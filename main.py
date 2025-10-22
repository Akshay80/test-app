# tg_bybit_futures_bot.py (Updated: Qty fix + Full SL risk)
import os
import re
from math import floor
from dotenv import load_dotenv
from telethon import TelegramClient, events
from pybit.unified_trading import HTTP

load_dotenv()

# ---------- CONFIG ----------
TG_API_ID = int(os.getenv("TG_API_ID"))
TG_API_HASH = os.getenv("TG_API_HASH")
TG_CHANNEL = os.getenv("TG_CHANNEL")  # numeric id like -1001234567890
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "false"
TRADE_PERCENT = float(os.getenv("TRADE_PERCENT", "0.05"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "20"))

BYBIT_ENDPOINT = "https://api-testnet.bybit.com" if USE_TESTNET else "https://api.bybit.com"

# ---------- Signal parser ----------
SYMBOL_RE = re.compile(r"#?([A-Z0-9]{2,20})\/USDT", re.IGNORECASE)
SIDE_RE = re.compile(r"\b(Long|Short)\b", re.IGNORECASE)
LEV_RE = re.compile(r"x(\d{1,3})")
ENTRY_RE = re.compile(r"Entry\s*[-:]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
TP_LINE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*\((\d+)%", re.IGNORECASE)

def parse_signal(text: str):
    text = text.replace("\r", "\n")
    symbol_m = SYMBOL_RE.search(text)
    symbol = symbol_m.group(1).upper() + "USDT" if symbol_m else None

    side_m = SIDE_RE.search(text)
    side = side_m.group(1).capitalize() if side_m else "Long"

    lev_m = LEV_RE.search(text)
    leverage = int(lev_m.group(1)) if lev_m else DEFAULT_LEVERAGE

    entry_m = ENTRY_RE.search(text)
    entry = float(entry_m.group(1).replace(',', '')) if entry_m else None

    tps = []
    allocations = []
    for m in TP_LINE_RE.finditer(text):
        price = float(m.group(1))
        pct = int(m.group(2))
        tps.append(price)
        allocations.append(pct / 100.0)

    if not tps:
        nums = re.findall(r"([0-9]+(?:\.[0-9]+)?)", text)
        for n in nums:
            f = float(n)
            if entry and abs(f - entry) < 1e-8:
                continue
            tps.append(f)

    if tps and (not allocations or sum(allocations) == 0):
        allocations = None

    return {
        "symbol": symbol,
        "side": side,
        "leverage": leverage,
        "entry": entry,
        "tps": tps,
        "allocations": allocations
    }

# ---------- Bybit client ----------
class BybitClient:
    def __init__(self, api_key, api_secret, testnet):
        self.s = HTTP(api_key=api_key, api_secret=api_secret, testnet=testnet)
        self.testnet = testnet

    def get_futures_balance(self):
        try:
            resp = self.s.get_wallet_balance(accountType="UNIFIED")
            if resp and 'result' in resp:
                coins = resp['result'].get('list', [])
                for account in coins:
                    for coin_data in account.get('coin', []):
                        if coin_data.get('coin') == 'USDT':
                            balance = float(coin_data.get('walletBalance', 0))
                            print(f"[BALANCE] Futures Wallet Balance: {balance} USDT")
                            return balance
            return None
        except Exception as e:
            print(f"[BALANCE] Error fetching balance: {e}")
            return None

    def set_leverage(self, symbol, leverage):
        try:
            return self.s.set_leverage(category="linear", symbol=symbol,
                                       buyLeverage=str(leverage), sellLeverage=str(leverage))
        except Exception as e:
            print("set_leverage error:", e)
            return None

    def place_market_order(self, symbol, side, qty):
        try:
            return self.s.place_order(category="linear", symbol=symbol, side=side,
                                      orderType="Market", qty=str(qty), timeInForce="IOC", isLeverage=1)
        except Exception as e:
            print("market order error:", e)
            raise

    def place_limit_reduce_only(self, symbol, side, qty, price):
        try:
            return self.s.place_order(category="linear", symbol=symbol, side=side,
                                      orderType="Limit", qty=str(qty), price=str(price),
                                      timeInForce="GTC", reduceOnly=True)
        except Exception as e:
            print("limit reduce-only error:", e)
            raise

    def place_stop_loss(self, symbol, side, stop_price):
        try:
            return self.s.set_trading_stop(category="linear", symbol=symbol,
                                           stopLoss=str(stop_price), positionIdx=0)
        except Exception as e:
            print("stop loss placement error:", e)
            raise

# ---------- Qty computation ----------
def compute_qty_from_notional(notional_usdt, entry_price):
    raw_qty = notional_usdt / entry_price
    qty = float(floor(raw_qty * 1000) / 1000.0)
    if qty <= 0:
        raise ValueError("Computed quantity <= 0. Increase capital or TRADE_PERCENT")
    return qty

def adjust_qty(symbol, qty):
    # Round down to nearest 0.001 for small coins, 1 for BTC/ETH
    if symbol in ["BTCUSDT","ETHUSDT"]:
        return floor(qty)
    else:
        return float(floor(qty * 1000) / 1000.0)

# ---------- Execute trade ----------
async def execute_trade(bybit: BybitClient, signal: dict):
    symbol = signal.get("symbol")
    side = signal.get("side")
    leverage = signal.get("leverage") or DEFAULT_LEVERAGE
    entry = signal.get("entry")
    tps = signal.get("tps") or []
    allocations = signal.get("allocations")

    if not symbol or not entry or not tps:
        print("[EXEC] Signal incomplete, skipping:", signal)
        return

    balance = bybit.get_futures_balance()
    if balance is None or balance <= 0:
        print("[ERROR] Could not fetch valid balance. Aborting trade.")
        return

    trade_size = balance * TRADE_PERCENT
    print(f"[EXEC] Current Balance: {balance} USDT")
    print(f"[EXEC] Trade Size: {trade_size} USDT ({TRADE_PERCENT*100}%)")
    print(f"[EXEC] Symbol: {symbol}, Side: {side}, Entry: {entry}, Leverage: {leverage}x")

    bybit.set_leverage(symbol, leverage)

    qty = compute_qty_from_notional(trade_size, entry)
    qty = adjust_qty(symbol, qty)
    if qty <= 0:
        print("[EXEC] Computed qty <= 0 after adjustment, skipping trade")
        return

    side_buy = "Buy" if side.lower() == "long" else "Sell"
    side_close = "Sell" if side_buy == "Buy" else "Buy"

    print(f"[EXEC] Placing market {side_buy} qty={qty} on {symbol}")
    try:
        resp = bybit.place_market_order(symbol, side_buy, qty)
        print("[EXEC] Market order response:", resp)
    except Exception as e:
        print("Market order failed:", e)
        return

    # TP allocations
    if allocations:
        allocs = allocations
        if len(allocs) < len(tps):
            remaining = 1.0 - sum(allocs)
            per = remaining / (len(tps) - len(allocs))
            allocs += [per] * (len(tps) - len(allocs))
    else:
        n = len(tps)
        allocs = [1.0 / n] * n

    # Place TP reduce-only orders
    for i, tp in enumerate(tps):
        frac = allocs[i] if i < len(allocs) else 1.0/len(tps)
        qty_tp = adjust_qty(symbol, qty * frac)
        if qty_tp <= 0:
            continue
        print(f"[EXEC] Place TP #{i+1} price={tp}, qty={qty_tp}, side_close={side_close}")
        try:
            bybit.place_limit_reduce_only(symbol, side_close, qty_tp, tp)
        except Exception as e:
            print("TP placement failed:", e)

    # Full SL risk
    if side.lower() == "long":
        sl_price = entry - trade_size / qty
    else:
        sl_price = entry + trade_size / qty

    print(f"[EXEC] Placing SL at stop_price={sl_price} (100% of allocated trade)")
    try:
        bybit.place_stop_loss(symbol, side_close, sl_price)
    except Exception as e:
        print("SL placement error:", e)

# ---------- Telegram listener ----------
async def start_telegram_listener():
    bybit = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, USE_TESTNET)
    client = TelegramClient('session_tg_bybit', TG_API_ID, TG_API_HASH)
    await client.start()

    try:
        channel_id = int(TG_CHANNEL) if TG_CHANNEL.lstrip('-').isdigit() else TG_CHANNEL
        channel_entity = await client.get_entity(channel_id)
        print(f"✅ Connected to channel: {channel_entity.title}")
    except Exception as e:
        print(f"❌ Could not fetch channel entity: {e}")
        return

    @client.on(events.NewMessage(chats=channel_entity))
    async def handler(event):
        try:
            raw = event.message.message or ""
            print("\n" + "="*60)
            print("NEW MESSAGE RECEIVED")
            print("="*60)
            print(raw)
            print("="*60)

            signal = parse_signal(raw)
            print("Parsed Signal:", signal)

            if signal.get("symbol") and signal.get("entry") and signal.get("tps"):
                await execute_trade(bybit, signal)
            else:
                print("[EXEC] Message did not match expected signal format, skipping.")
        except Exception as e:
            print("Error in handler:", e)

    print("="*60)
    print(f"🤖 BOT STARTED - Listening to: {channel_entity.title}")
    print(f"🔧 Testnet Mode: {USE_TESTNET}")
    print(f"💰 Trade Size: {TRADE_PERCENT*100}% of balance (full SL risk)")
    print("="*60)
    await client.run_until_disconnected()

if __name__ == "__main__":
    import asyncio
    asyncio.run(start_telegram_listener())
