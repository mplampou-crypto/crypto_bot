import hmac
import hashlib
import time
import httpx
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET,
    DEFAULT_LEVERAGE, DEFAULT_USDT, DEFAULT_SL_PCT, DEFAULT_TP_PCT,
    BREAKEVEN_TRIGGER_PCT
)

BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"


def _sign(params: dict):
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    param_str = ts + BYBIT_API_KEY + recv_window + sorted_params
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
    data = await _request("GET", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


async def set_leverage(symbol: str, leverage: int) -> bool:
    params = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    }
    data = await _request("POST", "/v5/position/set-leverage", params, signed=True)
    if data and data.get("retCode") in [0, 110043]:
        return True
    print(f"Set leverage error: {data}")
    return False


async def place_order(symbol: str, side: str, usdt_amount: float,
                      leverage: int, sl_pct: float, tp_pct: float) -> dict:
    current_price = await get_price(symbol)
    if current_price == 0:
        return {"success": False, "error": "Δεν βρέθηκε τιμή"}

    await set_leverage(symbol, leverage)

    position_value = usdt_amount * leverage
    qty = round(position_value / current_price, 3)

    if side == "Buy":
        sl_price = round(current_price * (1 - sl_pct / 100), 4)
        tp_price = round(current_price * (1 + tp_pct / 100), 4)
    else:
        sl_price = round(current_price * (1 + sl_pct / 100), 4)
        tp_price = round(current_price * (1 - tp_pct / 100), 4)

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
        pnl_tp   = round(position_value * tp_pct / 100, 2)
        pnl_sl   = round(position_value * sl_pct / 100, 2)
        # Break-even trigger price
        if side == "Buy":
            be_trigger = round(current_price + (tp_price - current_price) * BREAKEVEN_TRIGGER_PCT / 100, 4)
        else:
            be_trigger = round(current_price - (current_price - tp_price) * BREAKEVEN_TRIGGER_PCT / 100, 4)

        return {
            "success": True,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "entry_price": current_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "be_trigger": be_trigger,
            "qty": qty,
            "leverage": leverage,
            "usdt_amount": usdt_amount,
            "expected_tp_pnl": pnl_tp,
            "expected_sl_loss": pnl_sl,
        }
    else:
        error_msg = data.get("retMsg", "Unknown error") if data else "No response"
        return {"success": False, "error": error_msg}


async def move_to_breakeven(symbol: str, side: str, entry_price: float, qty: float) -> bool:
    """Μεταφέρει το SL στο entry price (break even)"""
    # Βάλε SL στο entry + μικρό buffer (0.05%) για να καλύψει fees
    if side == "Buy":
        new_sl = round(entry_price * 1.0005, 4)
    else:
        new_sl = round(entry_price * 0.9995, 4)

    params = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": str(new_sl),
        "slTriggerBy": "LastPrice",
        "tpslMode": "Full",
    }
    data = await _request("POST", "/v5/position/trading-stop", params, signed=True)
    return data and data.get("retCode") == 0


async def update_trailing_stop(symbol: str, side: str, current_price: float,
                                entry_price: float, tp_price: float) -> float | None:
    """
    Trailing stop: μετακινεί το SL ακολουθώντας την τιμή.
    Επιστρέφει νέο SL αν αλλάξει.
    """
    # Trail distance = 30% της TP απόστασης
    if side == "Buy":
        tp_distance = tp_price - entry_price
        trail_dist  = tp_distance * 0.3
        new_sl = round(current_price - trail_dist, 4)
        # Μόνο αν ανεβαίνει
        return new_sl if new_sl > entry_price else None
    else:
        tp_distance = entry_price - tp_price
        trail_dist  = tp_distance * 0.3
        new_sl = round(current_price + trail_dist, 4)
        return new_sl if new_sl < entry_price else None


async def set_stop_loss(symbol: str, new_sl: float) -> bool:
    params = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": str(new_sl),
        "slTriggerBy": "LastPrice",
        "tpslMode": "Full",
    }
    data = await _request("POST", "/v5/position/trading-stop", params, signed=True)
    return data and data.get("retCode") == 0


async def get_wallet_balance() -> float:
    params = {"accountType": "UNIFIED", "coin": "USDT"}
    data = await _request("GET", "/v5/account/wallet-balance", params, signed=True)
    try:
        return float(data["result"]["list"][0]["coin"][0]["availableToWithdraw"])
    except:
        return 0.0


async def get_open_positions() -> list:
    params = {"category": "linear", "settleCoin": "USDT"}
    data = await _request("GET", "/v5/position/list", params, signed=True)
    try:
        positions = data["result"]["list"]
        return [p for p in positions if float(p.get("size", 0)) > 0]
    except:
        return []


def format_trade_message(trade: dict) -> str:
    side_emoji = "🟢 LONG" if trade["side"] == "Buy" else "🔴 SHORT"
    coin = trade["symbol"].replace("USDT", "")
    return (
        f"⚡️ <b>Νέο Trade!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Entry: <b>${trade['entry_price']:,.4f}</b>\n"
        f"Stop Loss: <b>${trade['sl_price']:,.4f}</b> (-{DEFAULT_SL_PCT}%)\n"
        f"Take Profit: <b>${trade['tp_price']:,.4f}</b> (+{DEFAULT_TP_PCT}%)\n"
        f"Break-Even trigger: <b>${trade['be_trigger']:,.4f}</b>\n"
        f"Leverage: <b>{trade['leverage']}x</b>\n"
        f"Margin: <b>{trade['usdt_amount']}€</b>\n\n"
        f"💰 Expected TP: <b>+{trade['expected_tp_pnl']:.1f}€</b>\n"
        f"⛔ Max Loss: <b>-{trade['expected_sl_loss']:.1f}€</b>"
    )


def format_rejected_message(symbol: str, side: str, score: int, reason: str) -> str:
    side_emoji = "🟢 LONG" if side.upper() == "LONG" or side == "Buy" else "🔴 SHORT"
    coin = symbol.replace("USDT", "")
    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Λόγος: <i>{reason}</i>"
    )


def format_breakeven_message(symbol: str, side: str, entry: float) -> str:
    coin = symbol.replace("USDT", "")
    return (
        f"🔒 <b>Break Even Ενεργοποιήθηκε!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"SL μεταφέρθηκε στο entry: <b>${entry:,.4f}</b>\n"
        f"Το trade δεν μπορεί πλέον να κλείσει με ζημιά! ✅"
    )


def format_pending_trade_message(pending: dict, sentiment: str = None) -> str:
    side_emoji = "🟢 LONG" if pending["side"] == "Buy" else "🔴 SHORT"
    coin = pending["symbol"].replace("USDT", "")
    msg = (
        f"⏰ <b>Νέο Signal — Απαιτείται Έγκριση!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{pending['signal_score']}/100</b>\n"
    )
    if sentiment:
        msg += f"Sentiment: <b>{sentiment}</b>\n"
    if pending.get("price_target"):
        msg += f"🎯 Price Target: <b>${pending['price_target']:,.4f}</b>\n"
    msg += (
        f"\n<b>Default:</b> {DEFAULT_USDT}€ / {DEFAULT_LEVERAGE}x leverage\n"
        f"Expected TP: +{round(DEFAULT_USDT * DEFAULT_LEVERAGE * DEFAULT_TP_PCT / 100, 1)}€\n\n"
        f"Πάτα για να ορίσεις leverage & ποσό:"
    )
    return msg
