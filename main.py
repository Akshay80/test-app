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

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"

TRADE_PERCENT = float(os.getenv("TRADE_PERCENT", 0.10))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", 20))
TRADE_CATEGORY = "linear"

# ---------------------------------
# Bybit API Initialization - FIXED
# ---------------------------------
# Remove base_url parameter - use testnet parameter instead
session = HTTP(
    api_key=BYBIT_API_KEY, 
    api_secret=BYBIT_API_SECRET,
    testnet=USE_TESTNET  # ✅ Use testnet parameter instead of base_url
)

# ---------------------------------
# Bybit Helper Functions
# ---------------------------------
def get_balance():
    try:
        res = session.get_wallet_balance(accountType="UNIFIED")
        balance = float(res["result"]["list"][0]["totalEquity"])
        return balance
    except Exception as e:
        print("❌ Error fetching balance:", e)
        return 0


def set_cross_margin(symbol: str):
    try:
        session.switch_margin_mode(category=TRADE_CATEGORY, symbol=symbol, tradeMode=0)  # 0 = Cross
        print(f"✅ Cross margin set for {symbol}")
    except Exception as e:
        print("⚠️ Failed to set cross margin:", e)


def set_leverage(symbol: str, leverage: int):
    try:
        session.set_leverage(
            category=TRADE_CATEGORY,
            symbol=symbol,
            buyLeverage=leverage,
            sellLeverage=leverage
        )
        print(f"✅ Leverage set to {leverage}x")
    except Exception as e:
        print("⚠️ Failed to set leverage:", e)


def get_market_price(symbol: str):
    try:
        ticker = session.get_tickers(category=TRADE_CATEGORY, symbol=symbol)
        return float(ticker["result"]["list"][0]["lastPrice"])
    except Exception as e:
        print("❌ Error fetching price:", e)
        return None


def calculate_order_qty(symbol: str, balance: float, percent: float, price: float):
    trade_value = balance * percent
    qty = trade_value / price
    return round(qty, 3)


def place_market_order(symbol: str, side: str, qty: float):
    try:
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
        return resp
    except Exception as e:
        print("❌ Failed to place order:", e)
        return None


# ---------------------------------
# Telegram Client Setup
# ---------------------------------
client = TelegramClient("bybit_auto_trade", TG_API_ID, TG_API_HASH)

# ---------------------------------
# Message Handler
# ---------------------------------
@client.on(events.NewMessage(chats=TG_CHANNEL))
async def handler(event):
    msg = event.raw_text
    print(f"\n📩 New Message: {msg}")

    # Detect signal direction
    if re.search(r"\b(long|buy)\b", msg, re.IGNORECASE):
        side = "Buy"
    elif re.search(r"\b(short|sell)\b", msg, re.IGNORECASE):
        side = "Sell"
    else:
        print("⚠️ No valid signal side found.")
        return

    # Extract symbol dynamically (any token ending with USDT)
    match = re.search(r"\b([A-Z0-9]+USDT)\b", msg.upper())
    if not match:
        print("⚠️ No valid symbol found in message.")
        return

    symbol = match.group(1).upper()
    print(f"🚀 Signal detected → {symbol} | {side}")

    # Execute trade
    balance = get_balance()
    price = get_market_price(symbol)
    if not price:
        print("⚠️ Could not fetch price.")
        return

    set_cross_margin(symbol)
    set_leverage(symbol, DEFAULT_LEVERAGE)

    qty = calculate_order_qty(symbol, balance, TRADE_PERCENT, price)
    print(f"💰 Balance: {balance:.2f} | Qty: {qty} | Price: {price}")

    place_market_order(symbol, side, qty)

# ---------------------------------
# Run Bot
# ---------------------------------
async def main():
    print("🤖 Bybit Auto Trader Started (CROSS, TESTNET)") if USE_TESTNET else print("🤖 Bybit Auto Trader Started (CROSS, MAINNET)")
    await client.start()
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())