import os
import re
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
from pybit.unified_trading import HTTP
from decimal import Decimal, ROUND_DOWN

# ---------------------------------
# Load ENV Variables
# ---------------------------------
load_dotenv()

TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_CHANNEL = int(os.getenv("TG_CHANNEL"))
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", None)

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"

TRADE_PERCENT = float(os.getenv("TRADE_PERCENT", 0.10))  # fraction of balance to trade (0.10 = 10%)
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", 20))
TRADE_CATEGORY = "linear"

# Safety / minimums (tweak if needed per symbol)
MIN_QTY = Decimal("0.000001")   # minimal quantity to try (some tokens require higher minimum)
QTY_DECIMALS = 6               # decimal precision to use for quantity (increase if needed)

# ---------------------------------
# Bybit API Initialization
# ---------------------------------
session = HTTP(
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    testnet=USE_TESTNET
)

# ---------------------------------
# Helper Functions
# ---------------------------------
def safe_decimal(v, quantize_decimals=6):
    """Return Decimal quantized down to quantize_decimals without rounding up."""
    d = Decimal(str(v))
    q = Decimal("1").scaleb(-quantize_decimals)  # 10^-quantize_decimals
    return d.quantize(q, rounding=ROUND_DOWN)


def get_balance():
    """Get current wallet balance (USDT equity from unified wallet)."""
    try:
        res = session.get_wallet_balance(accountType="UNIFIED")
        # find USDT or the first entry
        if "result" in res and "list" in res["result"] and len(res["result"]["list"]) > 0:
            # many accounts: sum or pick totalEquity
            total_equity = res["result"]["list"][0].get("totalEquity")
            return float(total_equity)
        else:
            print("❌ Unexpected balance response:", res)
            return 0.0
    except Exception as e:
        print("❌ Error fetching balance:", e)
        return 0.0


def set_cross_margin(symbol: str):
    """Attempt to set cross margin (if account allows). Fail gracefully."""
    try:
        # Note: some accounts do not allow switching to unified cross; handle error gracefully.
        session.switch_margin_mode(
            category=TRADE_CATEGORY,
            symbol=symbol,
            tradeMode=0  # 0 = cross, 1 = isolated (for some endpoints)
        )
        print(f"✅ Cross margin set for {symbol}")
    except Exception as e:
        # Log and continue — this often fails with ErrCode:100028 if unified forbidden
        print("⚠️ Failed to set cross margin (continuing):", e)


def set_leverage(symbol: str, leverage: int):
    """Attempt to set leverage. Fail gracefully if API rejects."""
    try:
        session.set_leverage(
            category=TRADE_CATEGORY,
            symbol=symbol,
            buyLeverage=leverage,
            sellLeverage=leverage
        )
        print(f"✅ Leverage set to {leverage}x for {symbol}")
    except Exception as e:
        # If leverage fails, log and continue; some markets or account types restrict leverage.
        print("⚠️ Failed to set leverage (continuing):", e)


def get_market_price(symbol: str):
    """Get current market price for symbol."""
    try:
        ticker = session.get_tickers(
            category=TRADE_CATEGORY,
            symbol=symbol
        )
        price = float(ticker["result"]["list"][0]["lastPrice"])
        return price
    except Exception as e:
        print("❌ Error fetching price for", symbol, ":", e)
        return None


def calculate_order_qty(balance: float, percent: float, price: float):
    """
    Calculate base token quantity for a trade.
    Uses Decimal with safe rounding down to avoid exceeding balance.
    """
    if price <= 0 or balance <= 0 or percent <= 0:
        return Decimal("0")
    trade_value = Decimal(str(balance)) * Decimal(str(percent))
    qty = trade_value / Decimal(str(price))
    qty = safe_decimal(qty, QTY_DECIMALS)
    if qty < MIN_QTY:
        # bump to minimal safe qty if calculation too small (this may still fail if exchange requires higher)
        qty = MIN_QTY
    return qty


def place_market_order(symbol: str, side: str, qty: Decimal):
    """Place a market order on Bybit. Returns response dict or None."""
    try:
        if qty <= 0:
            print(f"⚠️ Invalid quantity: {qty}")
            return None

        # Bybit unified API expects qty as string or number; using string ensures decimal formatting
        resp = session.place_order(
            category=TRADE_CATEGORY,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),       # use stringified Decimal for precision
            timeInForce="GTC",  # GTC is fine for market orders on some wrappers
            reduceOnly=False
        )
        print(f"✅ Market order placed: {side} {qty} {symbol}")
        print(f"   Response: {resp}")
        return resp
    except Exception as e:
        print("❌ Failed to place market order:", e)
        return None


def place_reduce_only_tp(symbol: str, take_profit_price: Decimal, side: str, qty: Decimal):
    """
    Place a reduce-only limit order to take profit.
    For a Sell short position (side='Sell') we place a Buy reduceOnly limit TP.
    For a Buy long position (side='Buy') we place a Sell reduceOnly limit TP.
    """
    try:
        tp_side = "Buy" if side.lower() == "sell" else "Sell"
        # Place limit reduce-only TP
        resp = session.place_order(
            category=TRADE_CATEGORY,
            symbol=symbol,
            side=tp_side,
            orderType="Limit",
            qty=str(qty),
            price=str(take_profit_price),
            timeInForce="GTC",
            reduceOnly=True
        )
        print(f"✅ TP order placed: {tp_side} {qty} {symbol} @ {take_profit_price} (reduceOnly)")
        print(f"   Response: {resp}")
        return resp
    except Exception as e:
        print("❌ Failed to place TP order:", e)
        return None


def parse_signal_message(msg: str):
    """
    Parse a telegram message and return dict with symbol, side, entry, tps list.
    Rules:
      - Skip messages that look like "Price / Profit" short summary.
      - Only accept structured messages that include "Entry -" and "Take-Profit:" and tp lines.
      - Symbol forms supported: #SYMBOL/USDT, SYMBOL-USDT, SYMBOLUSDT
    """
    # Quickly ignore messages like "✅ Price - 105.69\n🔝 Profit - 80%" to follow user's requirement
    if re.search(r"Price\s*-\s*\d+(\.\d+)?\s*\n.*Profit", msg, re.IGNORECASE):
        print("ℹ️ Ignoring compact price/profit message per user instruction.")
        return None

    # Find side: buy/long or sell/short
    if re.search(r"\b(long|buy)\b", msg, re.IGNORECASE):
        side = "Buy"
    elif re.search(r"\b(short|sell)\b", msg, re.IGNORECASE):
        side = "Sell"
    else:
        print("⚠️ No valid signal side found in message.")
        return None

    # Symbol detection (uppercase safe)
    sym_match = re.search(r"#?([A-Z0-9]+)[\-/]?USDT", msg.upper())
    if not sym_match:
        print("⚠️ No valid symbol found in message.")
        return None
    symbol = f"{sym_match.group(1).upper()}USDT"

    # Entry price
    entry_match = re.search(r"Entry\s*-\s*([0-9]*\.?[0-9]+)", msg, re.IGNORECASE)
    entry_price = float(entry_match.group(1)) if entry_match else None

    # Take-Profit lines: capture all numbers after the "Take-Profit" section
    tps = []
    if re.search(r"Take-?Profit", msg, re.IGNORECASE):
        # extract numbers on lines following 'Take-Profit' or in the message
        all_numbers = re.findall(r"([0-9]*\.?[0-9]+)", msg)
        # Heuristic: remove small integers like leverage (e.g., x20) — choose numbers close to prices
        # We'll collect numbers that reasonably look like price (e.g., >0.01)
        for n in all_numbers:
            try:
                val = float(n)
                # skip tiny numbers (leverage or percentages)
                if val > 0.01:
                    # store
                    tps.append(val)
            except:
                continue
    # Keep unique and sorted as they appear (we'll assume last TP is 100%)
    if not tps:
        print("⚠️ No TP prices parsed - continuing without TP (you asked 100% TP but none found).")
    return {
        "symbol": symbol,
        "side": side,
        "entry": entry_price,
        "tps": tps
    }


# ---------------------------------
# Telegram Client Setup
# ---------------------------------
client = TelegramClient("bybit_auto_trade", TG_API_ID, TG_API_HASH)


async def start_client():
    """Start Telegram client with session or bot token"""
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("✅ Connected with existing session file")
            return
        else:
            print("⚠️ Session file exists but not authorized")
    except Exception as e:
        print(f"⚠️ Could not use session file: {e}")

    if TG_BOT_TOKEN:
        try:
            await client.start(bot_token=TG_BOT_TOKEN)
            print("✅ Connected with Bot Token")
            return
        except Exception as e:
            print(f"❌ Bot token authentication failed: {e}")
            raise
    else:
        try:
            await client.start()
            print("✅ Connected with interactive login (Phone/Bot token)")
            return
        except EOFError:
            print("\n" + "=" * 60)
            print("❌ AUTHENTICATION ERROR")
            print("=" * 60)
            print("No session file found and no bot token provided.")
            print("\n📌 SOLUTION - Choose one:")
            print("\n1️⃣ LOCAL DEVELOPMENT (Recommended):")
            print("   - Run: python main.py")
            print("   - Enter phone number when prompted")
            print("   - Verify OTP")
            print("   - Session file will be created")
            print("\n2️⃣ RAILWAY DEPLOYMENT:")
            print("   - Create bot token via @BotFather on Telegram")
            print("   - Add TG_BOT_TOKEN to Railway env vars")
            print("   - Deploy")
            print("=" * 60 + "\n")
            raise


# ---------------------------------
# Message Handler
# ---------------------------------
@client.on(events.NewMessage(chats=TG_CHANNEL))
async def handler(event):
    """Handle incoming Telegram messages"""
    msg = event.raw_text
    print(f"\n{'=' * 60}")
    print(f"📩 New Message: {msg}")
    print('=' * 60)

    parsed = parse_signal_message(msg)
    if not parsed:
        print("ℹ️ Message ignored or not parsed as a valid trading signal.")
        return

    symbol = parsed["symbol"]
    side = parsed["side"]
    tps = parsed["tps"]

    print(f"🚀 Signal detected: {symbol} | Side: {side}")
    balance = get_balance()
    if balance <= 0:
        print(f"❌ Insufficient balance: {balance}")
        return
    print(f"💰 Current balance: {balance:.2f} USDT")

    price = get_market_price(symbol)
    if not price or price <= 0:
        print(f"❌ Could not fetch valid price for {symbol}")
        return
    print(f"📊 Current price: {price} USDT")

    # Set margin & leverage if possible (non-fatal)
    print(f"⚙️  Setting trading parameters...")
    set_cross_margin(symbol)
    set_leverage(symbol, DEFAULT_LEVERAGE)

    # Calculate order quantity
    qty = calculate_order_qty(balance, TRADE_PERCENT, price)
    print(f"📈 Trade details:")
    print(f"   - Quantity: {qty}")
    trade_amount = (qty * Decimal(str(price)))
    print(f"   - Trade amount: {trade_amount:.2f} USDT")
    print(f"   - Leverage: {DEFAULT_LEVERAGE}x")

    if qty <= 0:
        print(f"❌ Invalid quantity calculated: {qty}")
        return

    # Place market order
    print(f"\n🔄 Placing {side} market order...")
    market_resp = place_market_order(symbol, side, qty)
    if not market_resp:
        print("❌ Market order failed, aborting TP placement.")
        return

    # Attempt to determine filled quantity from response (fallback to requested qty)
    filled_qty = qty
    try:
        # Try common response paths to find executed qty
        if isinstance(market_resp, dict):
            r = market_resp.get("result") or market_resp.get("data") or market_resp
            # different wrappers return different structures; search keys
            if isinstance(r, dict):
                # possible keys: "filledQty", "execQty", "lastExecQty"
                for k in ("filledQty", "execQty", "lastExecQty", "filled_qty"):
                    if k in r:
                        filled_qty = Decimal(str(r[k]))
                        break
                # some wrappers set "list" with order info
                if "list" in r and isinstance(r["list"], list) and len(r["list"]) > 0:
                    item = r["list"][0]
                    for k in ("filledQty", "execQty", "lastExecQty", "filled_qty"):
                        if k in item:
                            filled_qty = Decimal(str(item[k]))
                            break
    except Exception as e:
        print("⚠️ Could not parse filled qty from market response, using requested qty. Error:", e)

    print(f"ℹ️ Using filled_qty (or requested qty) for TP order: {filled_qty}")

    # If TP prices parsed, use the last one (assumed 100% TP). Otherwise, skip TP placement.
    if tps:
        try:
            tp_price = Decimal(str(tps[-1]))  # last TP = 100% per user
            tp_price = tp_price.quantize(Decimal("1").scaleb(-8), rounding=ROUND_DOWN)  # 8 decimal price precision
            print(f"🚀 Placing TP at {tp_price} (100%)")
            tp_resp = place_reduce_only_tp(symbol, tp_price, side, filled_qty)
            if not tp_resp:
                print("⚠️ TP placement returned no response or failed. Inspect logs.")
        except Exception as e:
            print("❌ Error while placing TP:", e)
    else:
        print("ℹ️ No TP price parsed from message; not placing TP.")

    print('=' * 60 + "\n")


# ---------------------------------
# Main Function
# ---------------------------------
async def main():
    """Main bot function"""
    mode = "🧪 TESTNET" if USE_TESTNET else "💰 MAINNET"
    print(f"\n{'=' * 60}")
    print(f"🤖 Bybit Auto Trading Bot")
    print(f"   Mode: {mode}")
    print(f"   Leverage: {DEFAULT_LEVERAGE}x")
    print(f"   Trade Size: {TRADE_PERCENT * 100}% of balance")
    print(f"   Channel ID: {TG_CHANNEL}")
    print('=' * 60 + "\n")

    try:
        await start_client()
        print("👂 Listening for trading signals...\n")
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\n⛔ Bot stopped by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        raise


# ---------------------------------
# Entry Point
# ---------------------------------
if __name__ == "__main__":
    asyncio.run(main())
