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

# Cache qty steps ώστε να μην κάνουμε request κάθε φορά
_qty_step_cache: dict = {}

# Γνωστά steps για τα βασικά pairs (fallback)
KNOWN_QTY_STEPS = {
    "BTCUSDT":  0.001,
    "ETHUSDT":  0.01,
    "SOLUSDT":  0.1,
    "BNBUSDT":  0.01,
    "XRPUSDT":  1.0,
    "DOGEUSDT": 1.0,
}


# ─── SIGNATURE & REQUESTS ─────────────────────────────────

def _make_headers(sign_payload: str) -> dict:
    """Φτιάχνει headers με σωστή υπογραφή για Bybit v5 API"""
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    # Bybit v5: timestamp + api_key + recv_window + payload
    full_str = ts + BYBIT_API_KEY + recv_window + sign_payload
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        full_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type":      "application/json",
        "X-BAPI-API-KEY":    BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP":  ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN":       signature,
    }


async def _get(endpoint: str, params: dict = None, signed: bool = False):
    """GET request στο Bybit API"""
    params    = params or {}
    query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    headers   = _make_headers(query_str) if signed else {"Content-Type": "application/json"}
    url       = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"Bybit GET error: {e}")
            return None


async def _post(endpoint: str, params: dict = None, signed: bool = False):
    """
    POST request στο Bybit API.
    ΣΗΜ: Για POST υπογράφουμε το JSON body string — ΟΧΙ query params.
    """
    params   = params or {}
    body_str = json.dumps(params, separators=(',', ':'))
    headers  = _make_headers(body_str) if signed else {"Content-Type": "application/json"}
    url      = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            # content= για να στείλουμε ακριβώς το ίδιο string που υπογράψαμε
            resp = await client.post(url, content=body_str, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"Bybit POST error: {e}")
            return None


# ─── QTY STEP ─────────────────────────────────────────────

async def get_qty_step(symbol: str) -> float:
    """Παίρνει το minimum qty step για ένα symbol από το Bybit"""
    if symbol in _qty_step_cache:
        return _qty_step_cache[symbol]

    data = await _get("/v5/market/instruments-info",
                      {"category": "linear", "symbol": symbol})
    try:
        step = float(data["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        _qty_step_cache[symbol] = step
        return step
    except:
        # Fallback στα γνωστά steps
        step = KNOWN_QTY_STEPS.get(symbol, 0.001)
        _qty_step_cache[symbol] = step
        return step


def round_qty(qty: float, step: float) -> float:
    """
    Στρογγυλοποιεί το qty προς τα ΚΑΤΩ στο σωστό step.
    Π.χ. qty=1.041, step=0.01 → 1.04
         qty=0.0312, step=0.001 → 0.031
    """
    qty_rounded = math.floor(qty / step) * step
    if step >= 1:
        return int(qty_rounded)
    decimals = len(str(step).rstrip('0').split('.')[-1])
    return round(qty_rounded, decimals)


# ─── MARKET DATA ──────────────────────────────────────────

async def get_price(symbol: str) -> float:
    """Παίρνει την τρέχουσα τιμή ενός symbol"""
    data = await _get("/v5/market/tickers",
                      {"category": "linear", "symbol": symbol})
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


# ─── LEVERAGE ─────────────────────────────────────────────

async def set_leverage(symbol: str, leverage: int) -> bool:
    """Ορίζει leverage για ένα symbol"""
    data = await _post("/v5/position/set-leverage", {
        "category":    "linear",
        "symbol":      symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }, signed=True)
    # retCode 110043 = leverage already set → OK
    if data and data.get("retCode") in [0, 110043]:
        return True
    print(f"Set leverage error: {data}")
    return False


# ─── PLACE ORDER ──────────────────────────────────────────

async def place_order(symbol: str, side: str, usdt_amount: float,
                      leverage: int, sl_pct: float, tp_pct: float,
                      tp_price: float = None, sl_price: float = None) -> dict:
    """
    Ανοίγει trade στο Bybit Perpetuals.

    Αν δοθούν tp_price/sl_price (από indicator) → τα χρησιμοποιεί.
    Αλλιώς → υπολογίζει από sl_pct/tp_pct.

    Επιστρέφει dict με success/error και λεπτομέρειες trade.
    """
    # 1. Παίρνουμε τρέχουσα τιμή
    current_price = await get_price(symbol)
    if current_price == 0:
        return {"success": False, "error": "Δεν βρέθηκε τιμή"}

    # 2. Set leverage
    await set_leverage(symbol, leverage)

    # 3. Υπολογισμός qty με σωστό step
    position_value = usdt_amount * leverage
    raw_qty        = position_value / current_price
    step           = await get_qty_step(symbol)
    qty            = round_qty(raw_qty, step)

    print(f"[Order] {symbol} {side} | price={current_price} | "
          f"raw_qty={raw_qty:.6f} | step={step} | qty={qty}")

    if qty <= 0:
        return {
            "success": False,
            "error": f"Qty πολύ μικρό ({qty}) για {symbol}. "
                     f"Αύξησε το margin ή το leverage."
        }

    # 4. SL / TP — χρησιμοποίησε dynamic τιμές αν δόθηκαν
    if sl_price and tp_price and float(sl_price) > 0 and float(tp_price) > 0:
        final_sl = round(float(sl_price), 4)
        final_tp = round(float(tp_price), 4)

        # Βεβαιώσου ότι είναι σωστή κατεύθυνση
        if side == "Buy":
            if final_sl >= current_price or final_tp <= current_price:
                print(f"[Order] Dynamic SL/TP invalid for {side}, using fallback")
                final_sl = round(current_price * (1 - sl_pct / 100), 4)
                final_tp = round(current_price * (1 + tp_pct / 100), 4)
        else:
            if final_sl <= current_price or final_tp >= current_price:
                print(f"[Order] Dynamic SL/TP invalid for {side}, using fallback")
                final_sl = round(current_price * (1 + sl_pct / 100), 4)
                final_tp = round(current_price * (1 - tp_pct / 100), 4)
    else:
        # Fallback: fixed %
        if side == "Buy":
            final_sl = round(current_price * (1 - sl_pct / 100), 4)
            final_tp = round(current_price * (1 + tp_pct / 100), 4)
        else:
            final_sl = round(current_price * (1 + sl_pct / 100), 4)
            final_tp = round(current_price * (1 - tp_pct / 100), 4)

    # 5. PnL εκτίμηση βάσει πραγματικών τιμών
    if side == "Buy":
        tp_pct_actual = (final_tp - current_price) / current_price * 100
        sl_pct_actual = (current_price - final_sl) / current_price * 100
    else:
        tp_pct_actual = (current_price - final_tp) / current_price * 100
        sl_pct_actual = (final_sl - current_price) / current_price * 100

    pnl_tp = round(position_value * tp_pct_actual / 100, 2)
    pnl_sl = round(position_value * sl_pct_actual / 100, 2)

    # 6. Place order
    data = await _post("/v5/order/create", {
        "category":       "linear",
        "symbol":         symbol,
        "side":           side,
        "orderType":      "Market",
        "qty":            str(qty),
        "stopLoss":       str(final_sl),
        "takeProfit":     str(final_tp),
        "timeInForce":    "GoodTillCancel",
        "reduceOnly":     False,
        "closeOnTrigger": False,
        "slTriggerBy":    "LastPrice",
        "tpTriggerBy":    "LastPrice",
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
        error_msg = data.get("retMsg", "Unknown") if data else "No response"
        return {"success": False, "error": error_msg}


# ─── POSITION MANAGEMENT ──────────────────────────────────

async def get_open_positions() -> list:
    """Επιστρέφει όλες τις ανοιχτές θέσεις από το Bybit"""
    data = await _get("/v5/position/list",
                      {"category": "linear", "settleCoin": "USDT"}, signed=True)
    try:
        return [p for p in data["result"]["list"]
                if float(p.get("size", 0)) > 0]
    except:
        return []


async def is_position_open(symbol: str) -> bool:
    """Ελέγχει αν υπάρχει ανοιχτή θέση για ένα symbol στο Bybit"""
    data = await _get("/v5/position/list", {
        "category": "linear",
        "symbol":   symbol,
    }, signed=True)
    try:
        positions = data["result"]["list"]
        for p in positions:
            if float(p.get("size", 0)) > 0:
                return True
        return False
    except:
        return True  # assume open αν δεν μπορούμε να ελέγξουμε


async def get_closed_pnl(limit: int = 50) -> list:
    """
    Παίρνει τα πρόσφατα κλειστά trades από το Bybit με το PnL τους.
    Χρησιμοποιείται για να ενημερώσουμε τη database αυτόματα.
    """
    data = await _get("/v5/position/closed-pnl", {
        "category": "linear",
        "limit":    str(limit),
    }, signed=True)
    try:
        return data["result"]["list"]
    except:
        return []


# ─── ACCOUNT ──────────────────────────────────────────────

async def get_wallet_balance() -> float:
    """Επιστρέφει το διαθέσιμο USDT balance"""
    data = await _get("/v5/account/wallet-balance",
                      {"accountType": "UNIFIED", "coin": "USDT"}, signed=True)
    try:
        return float(data["result"]["list"][0]["coin"][0]["availableToWithdraw"])
    except:
        return 0.0


# ─── MESSAGE FORMATTERS ───────────────────────────────────

def format_trade_message(trade: dict) -> str:
    """Φτιάχνει μήνυμα Telegram για νέο trade"""
    side_emoji = "🟢 LONG" if trade["side"] == "Buy" else "🔴 SHORT"
    coin       = trade["symbol"].replace("USDT", "")
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
    """Φτιάχνει μήνυμα για rejected signal"""
    side_emoji = "🟢 LONG" if side.upper() in ["LONG", "BUY"] else "🔴 SHORT"
    coin       = symbol.replace("USDT", "")
    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Λόγος: <i>{reason}</i>"
    )


def format_closed_trade_message(symbol: str, side: str,
                                  pnl: float, result: str) -> str:
    """Φτιάχνει μήνυμα για κλειστό trade"""
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

