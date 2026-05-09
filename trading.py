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

    symbol = symbol.replace(".P", "")
    symbol = symbol.replace("BINANCE:", "")
    symbol = symbol.replace("BYBIT:", "")
    symbol = symbol.replace("/", "")
    symbol = symbol.replace("-", "")

    if symbol in VALID_SYMBOLS:
        return VALID_SYMBOLS[symbol]

    if symbol.endswith("USD") and not symbol.endswith("USDT"):
        symbol += "T"

    if symbol.endswith("USDTUSDT"):
        symbol = symbol.replace("USDTUSDT", "USDT")

    return symbol


# ─────────────────────────────────────────
# SIGNATURE
# ─────────────────────────────────────────

def _make_headers(sign_payload: str) -> dict:

    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"

    param_str = (
        timestamp
        + BYBIT_API_KEY
        + recv_window
        + sign_payload
    )

    signature = hmac.new(
        BYBIT_API_SECRET.encode(),
        param_str.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
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

    query_string = "&".join(
        f"{k}={v}"
        for k, v in sorted(params.items())
    )

    headers = (
        _make_headers(query_string)
        if signed
        else {"Content-Type": "application/json"}
    )

    url = f"{BASE_URL}{endpoint}"

    try:

        async with httpx.AsyncClient(timeout=20) as client:

            response = await client.get(
                url,
                params=params,
                headers=headers
            )

            print("GET STATUS:", response.status_code)
            print("GET RESPONSE:", response.text)

            return response.json()

    except Exception as e:

        import traceback

        print("========== GET ERROR ==========")
        print(str(e))
        traceback.print_exc()
        print("================================")

        return {
            "retCode": -1,
            "retMsg": str(e)
        }


# ─────────────────────────────────────────
# POST REQUEST
# ─────────────────────────────────────────

async def _post(
    endpoint: str,
    params: dict = None,
    signed: bool = False
):

    params = params or {}

    body = json.dumps(
        params,
        separators=(",", ":")
    )

    headers = (
        _make_headers(body)
        if signed
        else {"Content-Type": "application/json"}
    )

    url = f"{BASE_URL}{endpoint}"

    try:

        async with httpx.AsyncClient(timeout=20) as client:

            response = await client.post(
                url,
                content=body,
                headers=headers
            )

            print("POST STATUS:", response.status_code)
            print("POST RESPONSE:", response.text)

            return response.json()

    except Exception as e:

        import traceback

        print("========== POST ERROR ==========")
        print(str(e))
        traceback.print_exc()
        print("================================")

        return {
            "retCode": -1,
            "retMsg": str(e)
        }


# ─────────────────────────────────────────
# GET PRICE
# ─────────────────────────────────────────

async def get_price(symbol: str) -> float:

    symbol = normalize_symbol(symbol)

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

        print(f"Price error: {e}")
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
            "sellLeverage": str(leverage)
        },
        signed=True
    )

    print("SET LEVERAGE:", data)

    if data and data.get("retCode") in [0, 110043]:
        return True

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

    print(f"ORDER SYMBOL: {symbol}")

    current_price = await get_price(symbol)

    if current_price == 0:

        return {
            "success": False,
            "error": f"No price for {symbol}"
        }

    await set_leverage(symbol, leverage)

    position_value = usdt_amount * leverage

    qty = round(
        position_value / current_price,
        3
    )

    if qty <= 0:

        return {
            "success": False,
            "error": "Invalid qty"
        }

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
        current_price + (
            (tp_price - current_price)
            * BREAKEVEN_TRIGGER_PCT / 100
        ),
        4
    )

    # MARKET ORDER

    data = await _post(
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "takeProfit": str(tp_price),
            "stopLoss": str(sl_price),
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice"
        },
        signed=True
    )

    print("ORDER RESPONSE:", data)

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

    return {
        "success": False,
        "error": data.get("retMsg", "Unknown error")
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

    new_sl = round(entry_price, 4)

    data = await _post(
        "/v5/position/trading-stop",
        {
            "category": "linear",
            "symbol": symbol,
            "stopLoss": str(new_sl),
            "slTriggerBy": "LastPrice",
            "tpslMode": "Full"
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
            "tpslMode": "Full"
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
            data["result"]["list"][0]["coin"][0]["walletBalance"]
        )

    except Exception as e:

        print("BALANCE ERROR:", e)
        print(data)

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

    except Exception as e:

        print("POSITIONS ERROR:", e)
        print(data)

        return []


# ─────────────────────────────────────────
# MESSAGES
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
        f"SL: <b>${trade['sl_price']:,.4f}</b>\n"
        f"TP: <b>${trade['tp_price']:,.4f}</b>\n"
        f"Leverage: <b>{trade['leverage']}x</b>\n"
        f"Margin: <b>{trade['usdt_amount']} USDT</b>\n\n"
        f"💰 TP Profit: <b>+{trade['expected_tp_pnl']} USDT</b>\n"
        f"⛔ SL Loss: <b>-{trade['expected_sl_loss']} USDT</b>"
    )


def format_rejected_message(
    symbol: str,
    side: str,
    score: int,
    reason: str
) -> str:

    coin = normalize_symbol(symbol).replace("USDT", "")

    return (
        f"🚫 <b>Signal Rejected</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"Side: <b>{side}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Reason: <i>{reason}</i>"
    )


def format_breakeven_message(
    symbol: str,
    side: str,
    entry: float
) -> str:

    coin = normalize_symbol(symbol).replace("USDT", "")

    return (
        f"🔒 <b>Break Even Activated!</b>\n\n"
        f"Pair: <b>{coin}/USDT</b>\n"
        f"New SL: <b>${entry:,.4f}</b>"
    )
