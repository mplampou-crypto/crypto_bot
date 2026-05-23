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
    if step >= 1:
        return int(qty_rounded)
    decimals = len(str(step).rstrip('0').split('.')[-1])
    return round(qty_rounded, decimals)


def fix_symbol(symbol):
    symbol = symbol.upper().strip()
    if symbol.endswith("USDTUSDT"):
        symbol = symbol[:-4]
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    return symbol


# ═══════════════════════════════════════════════════════════════════
# PARSE WEBHOOK — διαβάζει το JSON από τον WS Pro indicator
# JSON format:
# {
#   "symbol":   "BTCUSDT",
#   "action":   "BUY" | "SELL" | "TP" | "STOP",
#   "side":     "LONG" | "SHORT",
#   "price":    74847.44,
#   "entry":    74847.44,
#   "sl":       74780.00,
#   "tp":       74947.00,
#   "tf":       "2",
#   "time":     "2026-05-23 11:32"
# }
# ═══════════════════════════════════════════════════════════════════

def parse_webhook(raw: dict) -> dict:
    """
    Μετατρέπει το JSON του WS Pro σε εσωτερικό format του bot.
    Επιστρέφει dict με:
      - symbol, action, side, price, entry, sl, tp, tf, time
      - bybit_side: "Buy" | "Sell"
      - is_entry: True αν είναι νέο trade (BUY/SELL)
      - is_exit:  True αν είναι κλείσιμο (TP/STOP)
    """
    symbol = fix_symbol(raw.get("symbol", ""))
    action = raw.get("action", "").upper()    # BUY / SELL / TP / STOP
    side   = raw.get("side",   "").upper()    # LONG / SHORT

    # Bybit χρησιμοποιεί Buy/Sell
    bybit_side = "Buy" if side == "LONG" else "Sell"

    price = float(raw.get("price", 0) or 0)
    entry = float(raw.get("entry", 0) or 0)
    sl    = float(raw.get("sl",    0) or 0) if raw.get("sl") not in [None, "null"] else None
    tp    = float(raw.get("tp",    0) or 0) if raw.get("tp") not in [None, "null"] else None
    tf    = raw.get("tf",   "")
    ts    = raw.get("time", "")

    is_entry = action in ["BUY", "SELL"]
    is_exit  = action in ["TP", "STOP"]

    return {
        "symbol":     symbol,
        "action":     action,
        "side":       side,
        "bybit_side": bybit_side,
        "price":      price,
        "entry":      entry,
        "sl":         sl,
        "tp":         tp,
        "tf":         tf,
        "time":       ts,
        "is_entry":   is_entry,
        "is_exit":    is_exit,
    }


async def get_price(symbol):
    symbol = fix_symbol(symbol)
    data = await _get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


async def set_leverage(symbol, leverage):
    data = await _post("/v5/position/set-leverage", {
        "category":    "linear",
        "symbol":      symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }, signed=True)
    return data and data.get("retCode") in [0, 110043]


async def get_atr(symbol: str, interval: str = "240", period: int = 14) -> float:
    data = await _get("/v5/market/kline", {
        "category": "linear",
        "symbol":   symbol,
        "interval": interval,
        "limit":    str(period + 1),
    })
    try:
        candles = data["result"]["list"]
        trs = []
        for i in range(len(candles) - 1):
            high       = float(candles[i][2])
            low        = float(candles[i][3])
            prev_close = float(candles[i + 1][4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if not trs:
            return 0.0
        return sum(trs) / len(trs)
    except Exception as e:
        print(f"[ATR Error] {symbol}: {e}")
        return 0.0


async def place_order(symbol, side, usdt_amount, leverage,
                      sl_price=None, tp_price=None):
    """
    Ανοίγει θέση με SL και TP.
    Προτεραιότητα: SL/TP από το Pine Script signal.
    Fallback: ATR-based SL + 1.5R TP αν δεν έρθουν από το signal.
    """
    symbol = fix_symbol(symbol)
    current_price = await get_price(symbol)
    if current_price == 0:
        return {"success": False, "error": "Δεν βρέθηκε τιμή"}

    await set_leverage(symbol, leverage)
    position_value = usdt_amount * leverage
    step = await get_qty_step(symbol)
    qty  = round_qty(position_value / current_price, step)
    print(f"[Order] {symbol} {side} price={current_price} qty={qty} sl={sl_price} tp={tp_price}")

    if qty <= 0:
        return {"success": False, "error": f"Qty πολύ μικρό για {symbol}"}

    # ── SL: από Pine Script ή ATR fallback ──
    final_sl = None
    if sl_price and float(sl_price) > 0:
        sl_candidate = float(sl_price)
        if side == "Buy" and sl_candidate < current_price:
            final_sl = round(sl_candidate, 4)
        elif side == "Sell" and sl_candidate > current_price:
            final_sl = round(sl_candidate, 4)

    if not final_sl:
        atr = await get_atr(symbol, interval="240", period=14)
        if atr == 0:
            atr = current_price * 0.015
        sl_distance = atr * DEFAULT_SL_ATR_MULT
        final_sl = round(
            current_price - sl_distance if side == "Buy"
            else current_price + sl_distance,
            4
        )

    # ── TP: από Pine Script ή 1.5R fallback ──
    final_tp = None
    if tp_price and float(tp_price) > 0:
        tp_candidate = float(tp_price)
        if side == "Buy" and tp_candidate > current_price:
            final_tp = round(tp_candidate, 4)
        elif side == "Sell" and tp_candidate < current_price:
            final_tp = round(tp_candidate, 4)

    if not final_tp:
        sl_distance_actual = abs(current_price - final_sl)
        final_tp = round(
            current_price + sl_distance_actual * 1.5 if side == "Buy"
            else current_price - sl_distance_actual * 1.5,
            4
        )

    # ── PnL εκτίμηση ──
    sl_pct = abs(current_price - final_sl) / current_price * 100
    pnl_sl = round(position_value * sl_pct / 100, 2)

    data = await _post("/v5/order/create", {
        "category":       "linear",
        "symbol":         symbol,
        "side":           side,
        "orderType":      "Market",
        "qty":            str(qty),
        "stopLoss":       str(final_sl),
        "takeProfit":     str(final_tp),
        "slTriggerBy":    "LastPrice",
        "tpTriggerBy":    "LastPrice",
        "timeInForce":    "GoodTillCancel",
        "reduceOnly":     False,
        "closeOnTrigger": False,
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
            "expected_sl_loss": pnl_sl,
        }
    else:
        return {
            "success": False,
            "error":   data.get("retMsg", "Unknown") if data else "No response"
        }


async def update_position_tp_sl(symbol, tp_price=None, sl_price=None):
    symbol = fix_symbol(symbol)
    params = {
        "category":   "linear",
        "symbol":     symbol,
        "tpslMode":   "Full",
        "positionIdx": 0,
    }
    if tp_price is not None:
        params["takeProfit"]  = str(round(float(tp_price), 4))
        params["tpTriggerBy"] = "LastPrice"
    if sl_price is not None:
        params["stopLoss"]    = str(round(float(sl_price), 4))
        params["slTriggerBy"] = "LastPrice"

    data = await _post("/v5/position/trading-stop", params, signed=True)
    if data and data.get("retCode") == 0:
        print(f"[Update SL/TP] {symbol} TP={tp_price} SL={sl_price} — OK")
        return True
    msg = data.get("retMsg", "Unknown") if data else "No response"
    print(f"[Update SL/TP] {symbol} FAILED: {msg}")
    return False


async def close_position_market(symbol, side, qty):
    symbol     = fix_symbol(symbol)
    close_side = "Sell" if side == "Buy" else "Buy"
    data = await _post("/v5/order/create", {
        "category":    "linear",
        "symbol":      symbol,
        "side":        close_side,
        "orderType":   "Market",
        "qty":         str(qty),
        "timeInForce": "GoodTillCancel",
        "reduceOnly":  True,
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
    except:
        return []


async def is_position_open(symbol):
    symbol = fix_symbol(symbol)
    data = await _get("/v5/position/list",
                      {"category": "linear", "symbol": symbol}, signed=True)
    try:
        for p in data["result"]["list"]:
            if float(p.get("size", 0)) > 0:
                return True
        return False
    except:
        return True


async def get_closed_pnl(limit=50):
    data = await _get("/v5/position/closed-pnl",
                      {"category": "linear", "limit": str(limit)}, signed=True)
    try:
        return data["result"]["list"]
    except:
        return []


async def get_wallet_balance():
    data = await _get("/v5/account/wallet-balance",
                      {"accountType": "UNIFIED", "coin": "USDT"}, signed=True)
    try:
        return float(data["result"]["list"][0]["coin"][0]["availableToWithdraw"])
    except:
        return 0.0


# ═══════════════════════════════════════════════════════════════════
# MESSAGE FORMATTERS
# ═══════════════════════════════════════════════════════════════════

def format_trade_message(trade):
    side_emoji = "🟢 LONG" if trade["side"] == "Buy" else "🔴 SHORT"
    coin = trade["symbol"].replace("USDT", "")
    return (
        f"⚡️ <b>Νέο Trade!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Entry: <b>${trade['entry_price']:,.4f}</b>\n"
        f"Stop Loss: <b>${trade['sl_price']:,.4f}</b>\n"
        f"Take Profit: <b>${trade['tp_price']:,.4f}</b>\n"
        f"Leverage: <b>{trade['leverage']}x</b> | "
        f"Margin: <b>{trade['usdt_amount']} USDT</b>\n\n"
        f"⛔ Max Loss: <b>-{trade['expected_sl_loss']:.1f} USDT</b>"
    )


def format_signal_message(parsed: dict):
    """Μήνυμα για νέο signal από WS Pro — πριν ανοίξει η θέση."""
    side_emoji = "🟢 LONG" if parsed["side"] == "LONG" else "🔴 SHORT"
    coin = parsed["symbol"].replace("USDT", "")
    sl_txt = f"${parsed['sl']:,.4f}" if parsed["sl"] else "ATR fallback"
    tp_txt = f"${parsed['tp']:,.4f}" if parsed["tp"] else "1.5R fallback"
    return (
        f"📡 <b>Signal — WS Pro</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Τιμή: <b>${parsed['price']:,.4f}</b>\n"
        f"SL: <b>{sl_txt}</b>\n"
        f"TP: <b>{tp_txt}</b>\n"
        f"TF: <b>{parsed['tf']}m</b> | "
        f"Ώρα: <b>{parsed['time']} UTC</b>"
    )


def format_rejected_message(symbol, side, score, reason):
    side_emoji = "🟢 LONG" if side.upper() in ["LONG", "BUY"] else "🔴 SHORT"
    coin = symbol.replace("USDT", "")
    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Λόγος: <i>{reason}</i>"
    )


def format_closed_trade_message(symbol, side, pnl, result):
    coin     = symbol.replace("USDT", "")
    emoji    = "✅" if result == "WIN" else "❌"
    side_txt = "LONG" if side == "Buy" else "SHORT"
    sign     = "+" if pnl >= 0 else ""
    return (
        f"{emoji} <b>Trade Έκλεισε — {result}</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_txt}</b>\n"
        f"PnL: <b>{sign}{pnl:.2f} USDT</b>"
    )
