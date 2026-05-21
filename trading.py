import hmac
import hashlib
import time
import json
import math
import httpx
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET,
    DEFAULT_LEVERAGE, DEFAULT_USDT, DEFAULT_SL_ATR_MULT
)

BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

_qty_step_cache: dict = {}
KNOWN_QTY_STEPS = {
    "BTCUSDT": 0.001, "ETHUSDT": 0.01, "SOLUSDT": 0.1,
    "BNBUSDT": 0.01,  "XRPUSDT": 1.0,  "DOGEUSDT": 1.0,
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
        "Content-Type":       "application/json",
        "X-BAPI-API-KEY":     BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN":        signature,
    }


async def _get(endpoint, params=None, signed=False):
    params    = params or {}
    query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    headers   = _make_headers(query_str) if signed else {"Content-Type": "application/json"}
    url       = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=30)
            data = resp.json()
            if data.get("retCode", 0) != 0:
                print(f"[Bybit GET Error] {endpoint} → code={data.get('retCode')} msg={data.get('retMsg')}")
            return data
        except httpx.TimeoutException:
            print(f"[Bybit GET TIMEOUT] {endpoint}")
            return None
        except Exception as e:
            print(f"[Bybit GET ERROR] {endpoint} — {type(e).__name__}: {e}")
            return None


async def _post(endpoint, params=None, signed=False):
    params   = params or {}
    body_str = json.dumps(params, separators=(',', ':'))
    headers  = _make_headers(body_str) if signed else {"Content-Type": "application/json"}
    url      = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, content=body_str, headers=headers, timeout=30)
            data = resp.json()
            if data.get("retCode", 0) != 0:
                print(f"[Bybit POST Error] {endpoint} → code={data.get('retCode')} msg={data.get('retMsg')}")
            return data
        except httpx.TimeoutException:
            print(f"[Bybit POST TIMEOUT] {endpoint}")
            return None
        except Exception as e:
            print(f"[Bybit POST ERROR] {endpoint} — {type(e).__name__}: {e}")
            return None


async def get_qty_step(symbol):
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


def round_qty(qty, step):
    qty_rounded = math.floor(qty / step) * step
    if step >= 1: return int(qty_rounded)
    decimals = len(str(step).rstrip('0').split('.')[-1])
    return round(qty_rounded, decimals)


def fix_symbol(symbol):
    symbol = symbol.upper().strip()
    if symbol.endswith("USDTUSDT"):
        symbol = symbol[:-4]
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    return symbol


async def get_price(symbol):
    symbol = fix_symbol(symbol)
    data = await _get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    try: return float(data["result"]["list"][0]["lastPrice"])
    except: return 0.0


async def set_leverage(symbol, leverage):
    data = await _post("/v5/position/set-leverage", {
        "category": "linear", "symbol": symbol,
        "buyLeverage": str(leverage), "sellLeverage": str(leverage),
    }, signed=True)
    return data and data.get("retCode") in [0, 110043]


async def get_atr(symbol: str, interval: str = "240", period: int = 14) -> float:
    """
    Υπολογίζει ATR από τα τελευταία κεριά (interval=240 → 4ωρο).
    Επιστρέφει ATR σε απόλυτη τιμή (π.χ. 450.0 για BTC).
    """
    data = await _get("/v5/market/kline", {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": str(period + 1),
    })
    try:
        candles = data["result"]["list"]
        # Bybit επιστρέφει: [startTime, open, high, low, close, volume, turnover]
        trs = []
        for i in range(len(candles) - 1):
            high  = float(candles[i][2])
            low   = float(candles[i][3])
            prev_close = float(candles[i + 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if not trs:
            return 0.0
        return sum(trs) / len(trs)
    except Exception as e:
        print(f"[ATR Error] {symbol}: {e}")
        return 0.0


async def place_order(symbol, side, usdt_amount, leverage, sl_price=None):
    """
    Ανοίγει θέση χωρίς TP.
    SL: αν δοθεί sl_price το χρησιμοποιεί, αλλιώς υπολογίζει 2x ATR από entry.
    """
    symbol = fix_symbol(symbol)
    current_price = await get_price(symbol)
    if current_price == 0:
        return {"success": False, "error": "Δεν βρέθηκε τιμή"}

    await set_leverage(symbol, leverage)
    position_value = usdt_amount * leverage
    step = await get_qty_step(symbol)
    qty  = round_qty(position_value / current_price, step)
    print(f"[Order] {symbol} {side} price={current_price} qty={qty}")

    if qty <= 0:
        return {"success": False, "error": f"Qty πολύ μικρό για {symbol}"}

    # ── SL Calculation ──
    if sl_price and float(sl_price) > 0:
        final_sl = round(float(sl_price), 4)
        # Έλεγχος λογικής
        if side == "Buy" and final_sl >= current_price:
            final_sl = None
        elif side == "Sell" and final_sl <= current_price:
            final_sl = None

    if not sl_price or not final_sl:
        # ATR-based SL
        atr = await get_atr(symbol, interval="240", period=14)
        if atr == 0:
            atr = current_price * 0.015  # fallback: 1.5% αν ATR αποτύχει
        sl_distance = atr * DEFAULT_SL_ATR_MULT
        if side == "Buy":
            final_sl = round(current_price - sl_distance, 4)
        else:
            final_sl = round(current_price + sl_distance, 4)

    if side == "Buy":
        sl_pct_actual = (current_price - final_sl) / current_price * 100
    else:
        sl_pct_actual = (final_sl - current_price) / current_price * 100

    pnl_sl = round(position_value * sl_pct_actual / 100, 2)

    data = await _post("/v5/order/create", {
        "category":      "linear",
        "symbol":        symbol,
        "side":          side,
        "orderType":     "Market",
        "qty":           str(qty),
        "stopLoss":      str(final_sl),
        "timeInForce":   "GoodTillCancel",
        "reduceOnly":    False,
        "closeOnTrigger": False,
        "slTriggerBy":   "LastPrice",
    }, signed=True)

    if data and data.get("retCode") == 0:
        return {
            "success":           True,
            "order_id":          data["result"]["orderId"],
            "symbol":            symbol,
            "side":              side,
            "entry_price":       current_price,
            "sl_price":          final_sl,
            "tp_price":          None,
            "qty":               qty,
            "leverage":          leverage,
            "usdt_amount":       usdt_amount,
            "expected_sl_loss":  pnl_sl,
        }
    else:
        return {"success": False,
                "error": data.get("retMsg", "Unknown") if data else "No response"}


async def update_position_tp_sl(symbol, tp_price=None, sl_price=None):
    """
    ✅ ΝΕΑ: Αλλάζει SL/TP υπάρχουσας θέσης στο Bybit.
    Χρησιμοποιείται:
    - Όταν έρθει νέο signal στην ίδια κατεύθυνση → νέο TP
    - Για auto-breakeven (μετακίνηση SL στο entry)
    """
    symbol = fix_symbol(symbol)
    params = {"category": "linear", "symbol": symbol,
              "tpslMode": "Full", "positionIdx": 0}

    if tp_price is not None:
        params["takeProfit"] = str(round(float(tp_price), 4))
        params["tpTriggerBy"] = "LastPrice"
    if sl_price is not None:
        params["stopLoss"] = str(round(float(sl_price), 4))
        params["slTriggerBy"] = "LastPrice"

    data = await _post("/v5/position/trading-stop", params, signed=True)

    if data and data.get("retCode") == 0:
        print(f"[Update SL/TP] {symbol} TP={tp_price} SL={sl_price} — OK")
        return True
    else:
        msg = data.get("retMsg", "Unknown") if data else "No response"
        print(f"[Update SL/TP] {symbol} FAILED: {msg}")
        return False


async def close_position_market(symbol, side, qty):
    symbol = fix_symbol(symbol)
    close_side = "Sell" if side == "Buy" else "Buy"
    data = await _post("/v5/order/create", {
        "category": "linear", "symbol": symbol,
        "side": close_side, "orderType": "Market",
        "qty": str(qty), "timeInForce": "GoodTillCancel",
        "reduceOnly": True,
    }, signed=True)
    if data and data.get("retCode") == 0:
        print(f"[Close OK] {symbol} {close_side}")
        return True
    print(f"[Close FAIL] {symbol} — {data}")
    return False


async def get_open_positions():
    data = await _get("/v5/position/list",
                      {"category": "linear", "settleCoin": "USDT"}, signed=True)
    try:
        return [p for p in data["result"]["list"] if float(p.get("size", 0)) > 0]
    except: return []


async def is_position_open(symbol):
    symbol = fix_symbol(symbol)
    data = await _get("/v5/position/list",
                      {"category": "linear", "symbol": symbol}, signed=True)
    try:
        for p in data["result"]["list"]:
            if float(p.get("size", 0)) > 0:
                return True
        return False
    except: return True


async def get_closed_pnl(limit=50):
    data = await _get("/v5/position/closed-pnl",
                      {"category": "linear", "limit": str(limit)}, signed=True)
    try: return data["result"]["list"]
    except: return []


async def get_wallet_balance():
    data = await _get("/v5/account/wallet-balance",
                      {"accountType": "UNIFIED", "coin": "USDT"}, signed=True)
    try: return float(data["result"]["list"][0]["coin"][0]["availableToWithdraw"])
    except: return 0.0


# ─── MESSAGE FORMATTERS ───────────────────────────────────

def format_trade_message(trade):
    side_emoji = "🟢 LONG" if trade["side"] == "Buy" else "🔴 SHORT"
    coin = trade["symbol"].replace("USDT", "")
    return (
        f"⚡️ <b>Νέο Trade!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\nΚατεύθυνση: <b>{side_emoji}</b>\n"
        f"Entry: <b>${trade['entry_price']:,.4f}</b>\n"
        f"Stop Loss: <b>${trade['sl_price']:,.4f}</b>\n"
        f"Exit: <b>Oscillator Signal (auto)</b>\n"
        f"Leverage: <b>{trade['leverage']}x</b> | Margin: <b>{trade['usdt_amount']} USDT</b>\n\n"
        f"⛔ Max Loss: <b>-{trade['expected_sl_loss']:.1f} USDT</b>"
    )


def format_rejected_message(symbol, side, score, reason):
    side_emoji = "🟢 LONG" if side.upper() in ["LONG", "BUY"] else "🔴 SHORT"
    coin = symbol.replace("USDT", "")
    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\nΚατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{score}/100</b>\nΛόγος: <i>{reason}</i>"
    )


def format_closed_trade_message(symbol, side, pnl, result):
    coin = symbol.replace("USDT", "")
    emoji = "✅" if result == "WIN" else "❌"
    side_txt = "LONG" if side == "Buy" else "SHORT"
    sign = "+" if pnl >= 0 else ""
    return (
        f"{emoji} <b>Trade Έκλεισε — {result}</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\nΚατεύθυνση: <b>{side_txt}</b>\n"
        f"PnL: <b>{sign}{pnl:.2f} USDT</b>"
    )
