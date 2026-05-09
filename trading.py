import hmac
import hashlib
import time
import json
import httpx

from config import (
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    BYBIT_TESTNET,
    DEFAULT_LEVERAGE,
    DEFAULT_USDT,
    DEFAULT_SL_PCT,
    DEFAULT_TP_PCT,
    BREAKEVEN_TRIGGER_PCT
)

# ─────────────────────────────────────────
# BASE URL
# ─────────────────────────────────────────

BASE_URL = (
    "https://api-testnet.bybit.com"
    if BYBIT_TESTNET
    else "https://api.bybit.com"
)

# ─────────────────────────────────────────
# SYMBOL FIXES
# ─────────────────────────────────────────

VALID_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
}


def normalize_symbol(symbol: str) -> str:

    symbol = symbol.upper()

    # TradingView cleanup
    symbol = symbol.replace(".P", "")
    symbol = symbol.replace("BINANCE:", "")
    symbol = symbol.replace("BYBIT:", "")
    symbol = symbol.replace("/", "")
    symbol = symbol.replace("-", "")

    # BTC → BTCUSDT
    if symbol in VALID_SYMBOLS:
        return VALID_SYMBOLS[symbol]

    # BTCUSD → BTCUSDT
    if symbol.endswith("USD") and not symbol.endswith("USDT"):
        symbol += "T"

    # BTCUSDT.P → BTCUSDT
    if symbol.endswith("USDTUSDT"):
        symbol = symbol.replace("USDTUSDT", "USDT")

    return symbol


# ─────────────────────────────────────────
# HEADERS / SIGNATURE
# ─────────────────────────────────────────

def _make_headers(sign_payload: str) -> dict:

    ts = str(int(time.time() * 1000))

    recv_window = "5000"

    full_str = (
        ts
        + BYBIT_API_KEY
        + recv_window
        + sign_payload
    )

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


# ─────────────────────────────────────────
# GET REQUEST
# ─────────────────────────────────────────

async def _get(
    endpoint: str,
    params: dict = None,
    signed: bool = False
):

    params = params or {}

    query_str = "&".join(
        f"{k}={v}"
        for k, v in sorted(params.items())
    )

    headers = (
        _make_headers(query_str)
        if signed
        else {
            "Content-Type": "application/json"
        }
    )

    url = f"{BASE_URL}{endpoint}"

    async with httpx.AsyncClient() as client:

        try:

            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=10
            )

            return resp.json()

        except Exception as e:

            print(f"Bybit GET error: {e}")

            return None


# ─────────────────────────────────────────
# POST REQUEST
# ─────────────────────────────────────────

async def _post(
    endpoint: str,
    params: dict = None,
    signed: bool = False
):

    params = params or {}

    body_str = json.dumps(
        params,
        separators=(',', ':')
    )

    headers = (
        _make_headers(body_str)
        if signed
        else {
            "Content-Type": "application/json"
        }
    )

    url = f"{BASE_URL}{endpoint}"

    async with httpx.AsyncClient() as client:

        try:

            resp = await client.post(
                url,
                content=body_str,
                headers=headers,
                timeout=10
            )

            return resp.json()

        except Exception as e:

            print(f"Bybit POST error: {e}")

            return None


# ─────────────────────────────────────────
# GET PRICE
# ─────────────────────────────────────────

async def get_price(symbol: str) -> float:

    symbol = normalize_symbol(symbol)

    print(f"PRICE REQUEST SYMBOL: {symbol}")

    data = await _get(
        "/v5/market/tickers",
        {
            "category": "linear",
            "symbol": symbol
        }
    )

    try:

        return float(
            data["result"]["list"][0]["lastPrice"]
        )

    except Exception as e:

        print(f"Price error for {symbol}: {e}")
        print(data)

        return 0.0


# ─────────────────────────────────────────
# SET LEVERAGE
# ─────────────────────────────────────────

async def set_leverage(
    symbol: str,
    leverage: int
) -> bool:

    symbol = normalize_symbol(symbol)

    data = await _post(
        "/v5/position/set-leverage",
        {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        },
        signed=True
    )

    if data and data.get("retCode") in [0, 110043]:

        return True

    print(f"Set leverage error: {data}")

    return False


# ─────────────────────────────────────────
# PLACE ORDER
# ─────────────────────────────────────────

async def place_order(
    symbol: str,
    side: str,
    usdt_amount: float,
    leverage: int,
    sl_pct: float,
    tp_pct: float
) -> dict:

    symbol = normalize_symbol(symbol)

    print(f"Trading symbol after cleanup: {symbol}")

    # GET PRICE

    current_price = await get_price(symbol)

    if current_price == 0:

        return {
            "success": False,
            "error": f"Δεν βρέθηκε τιμή για {symbol}"
        }

    # SET LEVERAGE

    await set_leverage(symbol, leverage)

    # POSITION SIZE

    position_value = usdt_amount * leverage

    qty = round(
        position_value / current_price,
        3
    )

    # LONG

    if side == "Buy":

        sl_price = round(
            current_price * (1 - sl_pct / 100),
            4
        )

        tp_price = round(
            current_price * (1 + tp_pct / 100),
            4
        )

        be_trigger = round(
            current_price + (
                (tp_price - current_price)
                * BREAKEVEN_TRIGGER_PCT / 100
            ),
            4
        )

    # SHORT

    else:

        sl_price = round(
            current_price * (1 + sl_pct / 100),
            4
        )

        tp_price = round(
            current_price * (1 - tp_pct / 100),
            4
        )

        be_trigger = round(
            current_price - (
                (current_price - tp_price)
                * BREAKEVEN_TRIGGER_PCT / 100
            ),
            4
        )

    # CREATE ORDER

    data = await _post(
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",

            "price": str(
                round(
                    current_price * (
                        0.9998
                        if side == "Buy"
                        else 1.0002
                    ),
                    4
                )
            ),

            "timeInForce": "PostOnly",

            "qty": str(qty),

            "stopLoss": str(sl_price),
            "takeProfit": str(tp_price),

            "reduceOnly": False,
            "closeOnTrigger": False,

            "slTriggerBy": "LastPrice",
            "tpTriggerBy": "LastPrice"
        },
        signed=True
    )

    print(data)

    # SUCCESS

    if data and data.get("retCode") == 0:

        pnl_tp = round(
            position_value * tp_pct / 100,
            2
        )

        pnl_sl = round(
            position_value * sl_pct / 100,
            2
        )

        return {
            "success": True,
            "order_id": data["result"]["orderId"],
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

    # FAILED

    else:

        print(f"Bybit order error: {data}")

        return {
            "success": False,
            "error": data.get("retMsg", "No response")
            if data else "No response"
        }


# ─────────────────────────────────────────
# MOVE TO BREAKEVEN
# ─────────────────────────────────────────

async def move_to_breakeven(
    symbol: str,
    side: str,
    entry_price: float,
    qty: float
) -> bool:

    symbol = normalize_symbol(symbol)

    new_sl = round(
        entry_price * (
            1.0005
            if side == "Buy"
            else 0.9995
        ),
        4
    )

    data = await _post(
        "/v5/position/trading-stop",
        {
            "category": "linear",
            "symbol": symbol,
            "stopLoss": str(new_sl),
            "slTriggerBy": "LastPrice",
            "tpslMode": "Full",
        },
        signed=True
    )

    return data and data.get("retCode") == 0


# ─────────────────────────────────────────
# SET STOP LOSS
# ─────────────────────────────────────────

async def set_stop_loss(
    symbol: str,
    new_sl: float
) -> bool:

    symbol = normalize_symbol(symbol)

    data = await _post(
        "/v5/position/trading-stop",
        {
            "category": "linear",
            "symbol": symbol,
            "stopLoss": str(new_sl),
            "slTriggerBy": "LastPrice",
            "tpslMode": "Full",
        },
        signed=True
    )

    return data and data.get("retCode") == 0


# ─────────────────────────────────────────
# TRAILING STOP
# ─────────────────────────────────────────

async def update_trailing_stop(
    symbol: str,
    side: str,
    current_price: float,
    entry_price: float,
    tp_price: float
):

    if side == "Buy":

        trail_dist = (
            tp_price - entry_price
        ) * 0.3

        new_sl = round(
            current_price - trail_dist,
            4
        )

        return (
            new_sl
            if new_sl > entry_price
            else None
        )

    else:

        trail_dist = (
            entry_price - tp_price
        ) * 0.3

        new_sl = round(
            current_price + trail_dist,
            4
        )

        return (
            new_sl
            if new_sl < entry_price
            else None
        )


# ─────────────────────────────────────────
# WALLET BALANCE
# ─────────────────────────────────────────

async def get_wallet_balance() -> float:

    data = await _get(
        "/v5/account/wallet-balance",
        {
            "accountType": "UNIFIED",
            "coin": "USDT"
        },
        signed=True
    )

    try:

        return float(
            data["result"]["list"][0]["coin"][0]["availableToWithdraw"]
        )

    except:

        return 0.0


# ─────────────────────────────────────────
# OPEN POSITIONS
# ─────────────────────────────────────────

async def get_open_positions() -> list:

    data = await _get(
        "/v5/position/list",
        {
            "category": "linear",
            "settleCoin": "USDT"
        },
        signed=True
    )

    try:

        return [
            p for p in data["result"]["list"]
            if float(p.get("size", 0)) > 0
        ]

    except:

        return []


# ─────────────────────────────────────────
# TRADE MESSAGE
# ─────────────────────────────────────────

def format_trade_message(trade: dict) -> str:

    side_emoji = (
        "🟢 LONG"
        if trade["side"] == "Buy"
        else "🔴 SHORT"
    )

    coin = trade["symbol"].replace("USDT", "")

    return (
        f"⚡️ <b>Νέο Trade!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Entry: <b>${trade['entry_price']:,.4f}</b>\n"
        f"Stop Loss: <b>${trade['sl_price']:,.4f}</b> (-{DEFAULT_SL_PCT}%)\n"
        f"Take Profit: <b>${trade['tp_price']:,.4f}</b> (+{DEFAULT_TP_PCT}%)\n"
        f"🔒 Break-Even at: <b>${trade['be_trigger']:,.4f}</b>\n"
        f"Leverage: <b>{trade['leverage']}x</b>\n"
        f"Margin: <b>{trade['usdt_amount']}€</b>\n\n"
        f"💰 TP: <b>+{trade['expected_tp_pnl']:.1f}€</b>  "
        f"⛔ SL: <b>-{trade['expected_sl_loss']:.1f}€</b>"
    )


# ─────────────────────────────────────────
# REJECTED MESSAGE
# ─────────────────────────────────────────

def format_rejected_message(
    symbol: str,
    side: str,
    score: int,
    reason: str
) -> str:

    side_emoji = (
        "🟢 LONG"
        if side.upper() in ["LONG", "BUY"]
        else "🔴 SHORT"
    )

    coin = normalize_symbol(symbol).replace("USDT", "")

    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Λόγος: <i>{reason}</i>"
    )


# ─────────────────────────────────────────
# BREAKEVEN MESSAGE
# ─────────────────────────────────────────

def format_breakeven_message(
    symbol: str,
    side: str,
    entry: float
) -> str:

    coin = normalize_symbol(symbol).replace("USDT", "")

    return (
        f"🔒 <b>Break Even Ενεργοποιήθηκε!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"SL μεταφέρθηκε στο entry: <b>${entry:,.4f}</b>\n"
        f"Αδύνατο να κλείσει με ζημιά! ✅"
    )


# ─────────────────────────────────────────
# PENDING TRADE MESSAGE
# ─────────────────────────────────────────

def format_pending_trade_message(
    pending: dict,
    sentiment: str = None
) -> str:

    side_emoji = (
        "🟢 LONG"
        if pending["side"] == "Buy"
        else "🔴 SHORT"
    )

    coin = normalize_symbol(
        pending["symbol"]
    ).replace("USDT", "")

    msg = (
        f"⏰ <b>Signal — Απαιτείται Έγκριση!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Κατεύθυνση: <b>{side_emoji}</b>\n"
        f"Score: <b>{pending['signal_score']}/100</b>\n"
    )

    if sentiment:

        msg += f"Sentiment: <b>{sentiment}</b>\n"

    if pending.get("price_target"):

        msg += (
            f"🎯 Target: "
            f"<b>${pending['price_target']:,.4f}</b>\n"
        )

    expected = round(
        DEFAULT_USDT
        * DEFAULT_LEVERAGE
        * DEFAULT_TP_PCT / 100,
        1
    )

    msg += (
        f"\nDefault: "
        f"{DEFAULT_USDT}€ / "
        f"{DEFAULT_LEVERAGE}x → "
        f"+{expected}€\n\n"
        f"Στείλε <code>leverage,ποσό</code>:"
    )

    return msg