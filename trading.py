import hmac
import hashlib
import time
import json
import math
import httpx
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET,
    DEFAULT_LEVERAGE, DEFAULT_USDT, DEFAULT_SL_PCT, DEFAULT_TP_PCT
)

BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

_qty_step_cache: dict = {}

KNOWN_QTY_STEPS = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.01,
    "SOLUSDT":  0.1,
    "BNBUSDT":  0.01,
    "XRPUSDT":  1.0,
    "DOGEUSDT": 1.0,
}


def _make_headers(sign_payload: str) -> dict:
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    full_str = ts + BYBIT_API_KEY + recv_window + sign_payload
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        full_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
    }


async def _get(endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}
    query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    headers = _make_headers(query_str) if signed else {"Content-Type": "application/json"}
    url = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"Bybit GET error: {e}")
            return None


async def _post(endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}
    body_str = json.dumps(params, separators=(',', ':'))
    headers = _make_headers(body_str) if signed else {"Content-Type": "application/json"}
    url = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, content=body_str, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"Bybit POST error: {e}")
            return None


async def get_qty_step(symbol: str) -> float:
    if symbol in _qty_step_cache:
        return _qty_step_cache[symbol]
    data = await _get("/v5/market/instruments-info",
                      {"category": "linear", "symbol": symbol})
    try:
        step = float(data["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        _qty_step_cache[symbol] = step
        return step
    except:
        step = KNOWN_QTY_STEPS.get(symbol, 0.001)
        _qty_step_cache[symbol] = step
        return step


def round_qty(qty: float, step: float) -> float:
    qty_rounded = math.floor(qty / step) * step
    if step >= 1:
        return int(qty_rounded)
    decimals = len(str(step).rstrip('0').split('.')[-1])
    return round(qty_rounded, decimals)


async def get_price(symbol: str) -> float:
    data = await _get("/v5/market/tickers",
                      {"category": "linear", "symbol": symbol})
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


async def set_leverage(symbol: str, leverage: int) -> bool:
    data = await _post("/v5/position/set-leverage", {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }, signed=True)
    if data and data.get("retCode") in [0, 110043]:
        return True
    print(f"Set leverage error: {data}")
    return False


async def place_order(symbol: str, side: str, usdt_amount: float,
                      leverage: int, sl_pct: float, tp_pct: float,
                      tp_price: float = None, sl_price: float = None) -> dict:
    """
    Ανοίγει trade. Αν δοθούν tp_price/sl_price (από indicator),
    τα χρησιμοποιεί αντί για fixed %.
    """
    current_price = await get_price(symbol)
    if current_price == 0:
        return {"success": False, "error": "Δεν βρέθηκε τιμή"}

    await set_leverage(symbol, leverage)

    position_value = usdt_amount * leverage
    step = await get_qty_step(symbol)
    qty  = round_qty(position_value / current_price, step)

    print(f"[Order] {symbol} {side} | price={current_price} | qty={qty} | step={step}")

    if qty <= 0:
        return {"success": False,
                "error": f"Qty πολύ μικρό για {symbol}. Αύξησε το margin."}

    # Χρησιμοποίησε dynamic τιμές αν δόθηκαν, αλλιώς fixed %
    if sl_price and tp_price and sl_price > 0 and tp_price > 0:
        final_sl = round(float(sl_price), 4)
        final_tp = round(float(tp_price), 4)
        # Βεβαιώσου ότι είναι σωστή κατεύθυνση
        if side == "Buy" and (final_sl >= current_price or final_tp <= current_price):
            final_sl = round(current_price * (1 - sl_pct / 100), 4)
            final_tp = round(current_price * (1 + tp_pct / 100), 4)
        elif side == "Sell" and (final_sl <= current_price or final_tp >= current_price):
            final_sl = round(current_price * (1 + sl_pct / 100), 4)
            final_tp = round(current_price * (1 - tp_pct / 100), 4)
    else:
        if side == "Buy":
            final_sl = round(current_price * (1 - sl_pct / 100), 4)
            final_tp = round(current_price * (1 + tp_pct / 100), 4)
        else:
            final_sl = round(current_price * (1 + sl_pct / 100), 4)
            final_tp = round(current_price * (1 - tp_pct / 100), 4)

    # PnL εκτίμηση
    if side == "Buy":
        tp_pct_actual = (final_tp - current_price) / current_price * 100
        sl_pct_actual = (current_price - final_sl) / current_price * 100
    else:
        tp_pct_actual = (current_price - final_tp) / current_price * 100
        sl_pct_actual = (final_sl - current_price) / current_price * 100

    pnl_tp = round(position_value * tp_pct_actual / 100, 2)
    pnl_sl = round(position_value * sl_pct_actual / 100, 2)

    data = await _post("/v5/order/create", {
        "category":      "linear",
        "symbol":        symbol,
        "side":          side,
        "orderType":     "Market",
        "qty":           str(qty),
        "stopLoss":      str(final_sl),
        "takeProfit":    str(final_tp),
        "timeInForce":   "GoodTillCancel",
        "reduceOnly":    False,
        "closeOnTrigger": False,
        "slTriggerBy":   "LastPrice",
        "tpTriggerBy":   "LastPrice",
    }, signed=True)

    if data and data.get("retCode") == 0:
        return {
            "success":          True,
            "order_id":         data["result"]["orderId"],
            "symbol":           symbol,
            "side":             side,
            "entry_price":      current_price,
            "sl_price":         final_sl,
            "tp_price":         final_tp,
            "qty":              qty,
            "leverage":         leverage,
            "usdt_amount":      usdt_amount,
            "expected_tp_pnl":  pnl_tp,
            "expected_sl_loss": pnl_sl,
        }
    else:
        return {"success": False,
                "error": data.get("retMsg", "Unknown") if data else "No response"}


async def get_wallet_balance() -> float:
    data = await _get("/v5/account/wallet-balance",
                      {"accountType": "UNIFIED", "coin": "USDT"}, signed=True)
    try:
        return float(data["result"]["list"][0]["coin"][0]["availableToWithdraw"])
    except:
        return 0.0


async def get_open_positions() -> list:
    """Παίρνει ανοιχτές θέσεις από το Bybit"""
    data = await _get("/v5/position/list",
                      {"category": "linear", "settleCoin": "USDT"}, signed=True)
    try:
        return [p for p in data["result"]["list"]
                if float(p.get("size", 0)) > 0]
    except:
        return []


async def get_closed_pnl(limit: int = 20) -> list:
    """
    Παίρνει τα πρόσφατα κλειστά trades από το Bybit με το PnL τους.
    Χρησιμοποιείται για να ενημερώσουμε τη database.
    """
    data = await _get("/v5/position/closed-pnl", {
        "category": "linear",
        "limit": str(limit),
    }, signed=True)
    try:
        return data["result"]["list"]
    except:
        return []


async def is_position_open(symbol: str) -> bool:
    """Ελέγχει αν υπάρχει ανοιχτή θέση για ένα symbol στο Bybit"""
    data = await _get("/v5/position/list", {
        "category": "linear",
        "symbol": symbol,
    }, signed=True)
    try:
        positions = data["result"]["list"]
        for p in positions:
            if float(p.get("size", 0)) > 0:
                return True
        return False
    except:
        return True  # assume open αν δεν μπορούμε να ελέγξουμε


def format_trade_message(trade: dict) -> str:
    side_emoji = "🟢 LONG" if trade["side"] == "Buy" else "🔴 SHORT"
    coin = trade["symbol"].replace("USDT", "")
    return (
        f"⚡️ <b>Νέο Trade!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Entry: <b>${trade['entry_price']:,.4f}</b>\n"
        f"Stop Loss: <b>${trade['sl_price']:,.4f}</b>\n"
        f"Take Profit: <b>${trade['tp_price']:,.4f}</b>\n"
        f"Leverage: <b>{trade['leverage']}x</b>\n"
        f"Margin: <b>{trade['usdt_amount']} USDT</b>\n\n"
        f"💰 Expected TP: <b>+{trade['expected_tp_pnl']:.1f} USDT</b>\n"
        f"⛔ Max Loss: <b>-{trade['expected_sl_loss']:.1f} USDT</b>"
    )


def format_rejected_message(symbol: str, side: str,
                              score: int, reason: str) -> str:
    side_emoji = "🟢 LONG" if side.upper() in ["LONG", "BUY"] else "🔴 SHORT"
    coin = symbol.replace("USDT", "")
    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Λόγος: <i>{reason}</i>"
    )


def format_closed_trade_message(symbol: str, side: str,
                                  pnl: float, result: str) -> str:
    coin      = symbol.replace("USDT", "")
    emoji     = "✅" if result == "WIN" else "❌"
    side_txt  = "LONG" if side == "Buy" else "SHORT"
    sign      = "+" if pnl >= 0 else ""
    return (
        f"{emoji} <b>Trade Έκλεισε — {result}</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_txt}</b>\n"
        f"PnL: <b>{sign}{pnl:.2f} USDT</b>"
    )

