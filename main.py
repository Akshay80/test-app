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

# Safety / minimums
MIN_QTY = Decimal("0.000001")
QTY_DECIMALS = 6

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
    d = Decimal(str(v))
    q = Decimal("1").scaleb(-quantize_decimals)
    return d.quantize(q, rounding=ROUND_DOWN)


def get_balance():
    try:
        res = session.get_wallet_balance(accountType="UNIFIED")
        if "result" in res and "list" in res["result"] and len(res["result"]["list"]) > 0:
            total_equity = res["result"]["list"][0].get("totalEquity")
            return float(total_equity)
        else:
            print("❌ Unexpected balance response:", res)
            return 0.0
    except Exception as e:
        print("❌ Error fetching balance:", e)
        return 0.0


def set_cross_margin(symbol: str):
    try:
        session.switch_margin_mode(
            category=TRADE_CATEGORY,
            symbol=symbol,
            tradeMode=0
        )
        print(f"✅ Cross margin set for {symbol}")
    except Exception as e:
        print("⚠️ Failed to set cross margin (continuing):", e)


def set_leverage(symbol: str, leverage: int):
    try:
        session.set_leverage(
            category=TRADE_CATEGORY,
            symbol=symbol,
            buyLeverage=leverage,
            sellLeverage=leverage
        )
        print(f"✅ Leverage set to {leverage}x for {symbol}")
    except Exception as e:
        print("⚠️ Failed to set leverage (continuing):", e)


def get_market_price(symbol: str):
    try:
        ticker = session.get_tickers(category=TRADE_CATEGORY, symbol=symbol)
        price = float(ticker["result"]["list"][0]["lastPrice"])
        return price
    except Exception as e:
        print("❌ Error fetching price for", symbol, ":", e)
        return None


def calculate_order_qty(balance: float, percent: float, price: float):
    if price <= 0 or balance <= 0 or percent <= 0:
        return Decimal("0")
    trade_value = Decimal(str(balance)) * Decimal(str(percent))
    qty = trade_value / Decimal(str(price))
    qty = safe_decimal(qty, QTY_DECIMALS)
    if qty < MIN_QTY:
        qty = MIN_QTY
    return qty


def place_market_order(symbol: str, side: str, qty: Decimal):
    try:
        if qty <= 0:
            print(f"⚠️ Invalid quantity: {qty}")
            return None

        resp = session.place_order(
            category=TRADE_CATEGORY,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="GTC",
            reduceOnly=False
        )
        print(f"✅ Market order placed: {side} {qty} {symbol}")
        print(f"   Response: {resp}")
        return resp
    except Exception as e:
        print("❌ Failed to place market order:", e)
        return None


def place_reduce_only_tp(symbol: str, take_profit_price: Decimal, side: str, qty: Decimal):
    try:
        tp_side = "Buy" if side.lower() == "sell" else "Sell"
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
        print(f"✅ TP order placed: {tp_side} {qty} {symbol} @ {take_profit_price}")
        print(f"   Response: {resp}")
        return resp
    except Exception as e:
        print("❌ Failed to place TP order:", e)
        return None


def parse_signal_message(msg: str):
    if re.search(r"Price\s*-\s*\d+(\.\d+)?\s*\n.*Profit", msg, re.IGNORECASE):
        print("ℹ️ Ignoring compact price/profit message.")
        return None

    if re.search(r"\b(long|buy)\b", msg, re.IGNORECASE):
        side = "Buy"
    elif re.search(r"\b(short|sell)\b", msg, re.IGNORECASE):
        side = "Sell"
    else:
        print("⚠️ No valid signal side found.")
        return None

    sym_match = re.search(r"#?([A-Z0-9]+)[\-/]?USDT", msg.upper())
    if not sym_match:
        print("⚠️ No valid symbol found.")
        return None
    symbol = f"{sym_match.group(1).upper()}USDT"

    entry_match = re.search(r"Entry\s*-\s*([0-9]*\.?[0-9]+)", msg, re.IGNORECASE)
    entry_price = float(entry_match.group(1)) if entry_match else None

    tps = []
    if re.search(r"Take-?Profit", msg, re.IGNORECASE):
        all_numbers = re.findall(r"([0-9]*\.?[0-9]+)", msg)
        for n in all_numbers:
            try:
                val = float(n)
                if val > 0.01:
                    tps.append(val)
            except:
                continue
    return {"symbol": symbol, "side": side, "entry": entry_price, "tps": tps}


# ---------------------------------
# Telegram Client Setup
# ---------------------------------
client = TelegramClient("bybit_auto_trade", TG_API_ID, TG_API_HASH)


async def start_client():
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("✅ Connected with existing session file")
            return
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
            print("✅ Connected with interactive login")
        except EOFError:
            print("❌ AUTHENTICATION ERROR — no session or token provided.")
            raise


# ---------------------------------
# Message Handler
# ---------------------------------
@client.on(events.NewMessage(chats=TG_CHANNEL))
async def handler(event):
    msg = event.raw_text
    print(f"\n{'=' * 60}\n📩 New Message:\n{msg}\n{'=' * 60}")

    parsed = parse_signal_message(msg)
    if not parsed:
        return

    symbol = parsed["symbol"]
    side = parsed["side"]
    tps = parsed["tps"]

    print(f"🚀 Signal detected: {symbol} | Side: {side}")
    balance = get_balance()
    if balance <= 0:
        print(f"❌ Insufficient balance: {balance}")
        return
    print(f"💰 Balance: {balance:.2f} USDT")

    price = get_market_price(symbol)
    if not price:
        print(f"❌ Could not fetch price for {symbol}")
        return
    print(f"📊 Price: {price} USDT")

    set_cross_margin(symbol)
    set_leverage(symbol, DEFAULT_LEVERAGE)

    qty = calculate_order_qty(balance, TRADE_PERCENT, price)
    print(f"📈 Quantity: {qty} | Value: {(qty * Decimal(str(price))):.2f} USDT")

    if qty <= 0:
        print("❌ Invalid quantity.")
        return

    print(f"🔄 Placing {side} Market Order...")
    market_resp = place_market_order(symbol, side, qty)
    if not market_resp:
        print("❌ Market order failed.")
        return

    filled_qty = qty
    print(f"ℹ️ Using filled qty: {filled_qty}")

    if tps:
        tp_price = Decimal(str(tps[-1])).quantize(Decimal("1").scaleb(-8), rounding=ROUND_DOWN)
        print(f"🚀 Placing TP at {tp_price} (100%)")
        place_reduce_only_tp(symbol, tp_price, side, filled_qty)
    else:
        print("ℹ️ No TP found, skipping.")

    print('=' * 60 + "\n")


# ---------------------------------
# Main Function
# ---------------------------------
async def main():
    mode = "🧪 TESTNET" if USE_TESTNET else "💰 MAINNET"
    print(f"\n{'=' * 60}\n🤖 Bybit Auto Trading Bot\nMode: {mode}\nLeverage: {DEFAULT_LEVERAGE}x\nTrade Size: {TRADE_PERCENT * 100}%\nChannel ID: {TG_CHANNEL}\n{'=' * 60}\n")

    try:
        await start_client()
        print("👂 Listening for signals...\n")
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\n⛔ Bot stopped by user.")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        raise


# ---------------------------------
# Entry Point
# ---------------------------------
if __name__ == "__main__":
    asyncio.run(main())
