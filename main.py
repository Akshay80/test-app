import os
import re
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
from pybit.unified_trading import HTTP

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

TRADE_PERCENT = float(os.getenv("TRADE_PERCENT", 0.10))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", 20))
TRADE_CATEGORY = "linear"

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
def get_balance():
    """Get current wallet balance"""
    try:
        res = session.get_wallet_balance(accountType="UNIFIED")
        balance = float(res["result"]["list"][0]["totalEquity"])
        return balance
    except Exception as e:
        print("❌ Error fetching balance:", e)
        return 0


def set_cross_margin(symbol: str):
    """Set cross margin mode for symbol"""
    try:
        session.switch_margin_mode(
            category=TRADE_CATEGORY,
            symbol=symbol,
            tradeMode=0
        )
        print(f"✅ Cross margin set for {symbol}")
    except Exception as e:
        print("⚠️ Failed to set cross margin:", e)


def set_leverage(symbol: str, leverage: int):
    """Set leverage for symbol"""
    try:
        session.set_leverage(
            category=TRADE_CATEGORY,
            symbol=symbol,
            buyLeverage=leverage,
            sellLeverage=leverage
        )
        print(f"✅ Leverage set to {leverage}x for {symbol}")
    except Exception as e:
        print("⚠️ Failed to set leverage:", e)


def get_market_price(symbol: str):
    """Get current market price for symbol"""
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
    """Calculate order quantity based on balance and percentage"""
    if price <= 0:
        return 0
    trade_value = balance * percent
    qty = trade_value / price
    return round(qty, 3)


def place_market_order(symbol: str, side: str, qty: float):
    """Place market order on Bybit"""
    try:
        if qty <= 0:
            print(f"⚠️ Invalid quantity: {qty}")
            return None

        resp = session.place_order(
            category=TRADE_CATEGORY,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty,
            timeInForce="IOC",
            reduceOnly=False
        )
        print(f"✅ Market order placed: {side} {qty} {symbol}")
        print(f"   Response: {resp}")
        return resp
    except Exception as e:
        print("❌ Failed to place order:", e)
        return None


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

    # Detect trade direction
    if re.search(r"\b(long|buy)\b", msg, re.IGNORECASE):
        side = "Buy"
    elif re.search(r"\b(short|sell)\b", msg, re.IGNORECASE):
        side = "Sell"
    else:
        print("⚠️ No valid signal side found (looking for: long/buy or short/sell)")
        return

    # Improved regex to detect symbols like #LIGHT/USDT, LIGHT-USDT, LIGHTUSDT
    match = re.search(r"#?([A-Z0-9]+)[-/]?USDT", msg.upper())
    if not match:
        print("⚠️ No valid symbol found (looking for: xxxUSDT format)")
        return

    symbol = f"{match.group(1).upper()}USDT"
    print(f"🚀 Signal detected: {symbol} | Side: {side}")

    # Get balance
    balance = get_balance()
    if balance <= 0:
        print(f"❌ Insufficient balance: {balance}")
        return

    print(f"💰 Current balance: {balance:.2f} USDT")

    # Get market price
    price = get_market_price(symbol)
    if not price or price <= 0:
        print(f"❌ Could not fetch valid price for {symbol}")
        return

    print(f"📊 Current price: {price} USDT")

    # Set margin and leverage
    print(f"⚙️  Setting trading parameters...")
    set_cross_margin(symbol)
    set_leverage(symbol, DEFAULT_LEVERAGE)

    # Calculate order quantity
    qty = calculate_order_qty(balance, TRADE_PERCENT, price)
    if qty <= 0:
        print(f"❌ Invalid quantity calculated: {qty}")
        return

    print(f"📈 Trade details:")
    print(f"   - Quantity: {qty}")
    print(f"   - Trade amount: {qty * price:.2f} USDT")
    print(f"   - Leverage: {DEFAULT_LEVERAGE}x")

    # Place the order
    print(f"\n🔄 Placing {side} order...")
    place_market_order(symbol, side, qty)
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
