```python
# ─────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────

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

            print("========== BYBIT GET ==========")
            print("URL:", url)
            print("Params:", params)
            print("Status:", resp.status_code)
            print("Response:", resp.text)
            print("================================")

            return resp.json()

        except Exception as e:

            import traceback

            print("========== BYBIT GET ERROR ==========")
            print(str(e))
            traceback.print_exc()
            print("=====================================")

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

            print("========== BYBIT POST ==========")
            print("URL:", url)
            print("BODY:", body_str)
            print("Status:", resp.status_code)
            print("Response:", resp.text)
            print("================================")

            return resp.json()

        except Exception as e:

            import traceback

            print("========== BYBIT POST ERROR ==========")
            print(str(e))
            traceback.print_exc()
            print("======================================")

            return {
                "retCode": -1,
                "retMsg": str(e)
            }


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

    current_price = await get_price(symbol)

    if current_price == 0:

        return {
            "success": False,
            "error": f"Δεν βρέθηκε τιμή για {symbol}"
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
            "error": "Invalid quantity"
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

    data = await _post(
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",

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

    else:

        print(f"Bybit order error: {data}")

        return {
            "success": False,
            "error": data.get("retMsg", "No response")
            if data else "No response"
        }
```
