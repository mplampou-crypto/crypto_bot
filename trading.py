import hmac
import hashlib
import time
import httpx
import asyncio
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET,
    DEFAULT_LEVERAGE, DEFAULT_USDT, DEFAULT_SL_PCT, DEFAULT_TP_PCT
)

BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"


def _sign(params: dict) -> str:
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    param_str = ts + BYBIT_API_KEY + recv_window
    # Sort params and build query string
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    param_str += sorted_params
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature, ts, recv_window


async def _request(method: str, endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}
    headers = {"Content-Type": "application/json"}

    if signed:
        sig, ts, recv_window = _sign(params)
        headers.update({
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": sig,
        })

    url = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient() as client:
        try:
            if method == "GET":
                resp = await client.get(url, params=params, headers=headers, timeout=10)
            else:
                resp = await client.post(url, json=params, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            print(f"Bybit API error: {e}")
            return None


async def get_price(symbol: str) -> float:
    """Παίρνει την τρέχουσα τιμή ενός pair"""
    data = await _request("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


async def set_leverage(symbol: str, leverage: int) -> bool:
    """Ορίζει leverage για ένα pair"""
    params = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    data = await _request("POST", "/v5/position/set-leverage", params, signed=True)
    if data and data.get("retCode") == 0:
        return True
    # retCode 110043 = leverage not changed (already set) → OK
    if data and data.get("retCode") == 110043:
        return True
    print(f"Set leverage error: {data}")
    return False


async def place_order(symbol: str, side: str, usdt_amount: float,
                      leverage: int, sl_pct: float, tp_pct: float) -> dict:
    """
    Ανοίγει μια θέση στο Bybit Perpetuals.
    side: 'Buy' ή 'Sell'
    Επιστρέφει: {success, order_id, entry_price, sl_price, tp_price, qty}
    """
    # 1. Παίρνουμε τρέχουσα τιμή
    current_price = await get_price(symbol)
    if current_price == 0:
        return {"success": False, "error": "Δεν βρέθηκε τιμή"}

    # 2. Set leverage
    await set_leverage(symbol, leverage)

    # 3. Υπολογισμός ποσότητας
    # usdt_amount = margin. Με leverage: position_value = usdt_amount * leverage
    position_value = usdt_amount * leverage
    qty = round(position_value / current_price, 3)

    # 4. Υπολογισμός SL / TP
    if side == "Buy":
        sl_price = round(current_price * (1 - sl_pct / 100), 4)
        tp_price = round(current_price * (1 + tp_pct / 100), 4)
    else:
        sl_price = round(current_price * (1 + sl_pct / 100), 4)
        tp_price = round(current_price * (1 - tp_pct / 100), 4)

    # 5. Place order
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "stopLoss": str(sl_price),
        "takeProfit": str(tp_price),
        "timeInForce": "GoodTillCancel",
        "reduceOnly": False,
        "closeOnTrigger": False,
        "slTriggerBy": "LastPrice",
        "tpTriggerBy": "LastPrice",
    }

    data = await _request("POST", "/v5/order/create", params, signed=True)

    if data and data.get("retCode") == 0:
        order_id = data["result"]["orderId"]
        # Υπολογισμός expected PnL
        # Με 100€, 25x leverage, 4% TP move:
        # PnL = position_value * tp_pct/100 = 100*25 * 0.04 = 100€ gross
        # Net after fees (~0.06%): ~96€ → αλλά με 100€ margin → +96€ profit
        # Με 2% SL: loss = 100*25*0.02 = 50€ gross
        pnl_tp  = round(position_value * tp_pct / 100, 2)
        pnl_sl  = round(position_value * sl_pct / 100, 2)

        return {
            "success": True,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "entry_price": current_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "qty": qty,
            "leverage": leverage,
            "usdt_amount": usdt_amount,
            "expected_tp_pnl": pnl_tp,
            "expected_sl_loss": pnl_sl,
        }
    else:
        error_msg = data.get("retMsg", "Unknown error") if data else "No response"
        return {"success": False, "error": error_msg}


async def close_position(symbol: str, side: str, qty: float) -> bool:
    """Κλείνει μια ανοιχτή θέση"""
    close_side = "Sell" if side == "Buy" else "Buy"
    params = {
        "category": "linear",
        "symbol": symbol,
        "side": close_side,
        "orderType": "Market",
        "qty": str(qty),
        "reduceOnly": True,
        "timeInForce": "GoodTillCancel",
    }
    data = await _request("POST", "/v5/order/create", params, signed=True)
    return data and data.get("retCode") == 0


async def get_open_positions() -> list:
    """Επιστρέφει όλες τις ανοιχτές θέσεις"""
    params = {"category": "linear", "settleCoin": "USDT"}
    data = await _request("GET", "/v5/position/list", params, signed=True)
    try:
        positions = data["result"]["list"]
        return [p for p in positions if float(p.get("size", 0)) > 0]
    except:
        return []


async def get_wallet_balance() -> float:
    """Επιστρέφει το διαθέσιμο USDT balance"""
    params = {"accountType": "UNIFIED", "coin": "USDT"}
    data = await _request("GET", "/v5/account/wallet-balance", params, signed=True)
    try:
        return float(data["result"]["list"][0]["coin"][0]["availableToWithdraw"])
    except:
        return 0.0


def format_trade_message(trade: dict) -> str:
    """Φτιάχνει μήνυμα για Telegram όταν ανοίγει trade"""
    side_emoji = "🟢 LONG" if trade["side"] == "Buy" else "🔴 SHORT"
    coin = trade["symbol"].replace("USDT", "")

    return (
        f"⚡️ <b>Νέο Trade Εκτελέστηκε!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Entry: <b>${trade['entry_price']:,.4f}</b>\n"
        f"Stop Loss: <b>${trade['sl_price']:,.4f}</b> (-{DEFAULT_SL_PCT}%)\n"
        f"Take Profit: <b>${trade['tp_price']:,.4f}</b> (+{DEFAULT_TP_PCT}%)\n"
        f"Leverage: <b>{trade['leverage']}x</b>\n"
        f"Margin: <b>{trade['usdt_amount']}€</b>\n\n"
        f"💰 Expected TP: <b>+{trade['expected_tp_pnl']:.1f}€</b>\n"
        f"⛔ Max Loss (SL): <b>-{trade['expected_sl_loss']:.1f}€</b>"
    )


def format_pending_trade_message(pending: dict, sentiment: dict = None) -> str:
    """Μήνυμα για manual approval (11:00-14:00)"""
    side_emoji = "🟢 LONG" if pending["side"] == "Buy" else "🔴 SHORT"
    coin = pending["symbol"].replace("USDT", "")

    msg = (
        f"⏰ <b>Νέο Signal — Απαιτείται Έγκριση!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{pending['signal_score']}/100</b>\n"
        f"Πηγή: {pending.get('source', 'TradingView')}\n"
    )

    if sentiment:
        msg += f"Sentiment: <b>{sentiment}</b>\n"

    if pending.get("price_target"):
        msg += f"🎯 Price Target: <b>${pending['price_target']:,.2f}</b>\n"

    msg += (
        f"\n<b>Ρύθμισε τις παραμέτρους:</b>\n"
        f"Default: 100€ margin, 25x leverage\n\n"
        f"Πάτα το κουμπί για να ορίσεις leverage & ποσό:"
    )
    return msg
