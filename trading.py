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

BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"


# ─────────────────────────────────────────
# SYMBOL NORMALIZATION
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
# HEADERS
# ─────────────────────────────────────────

def _make_headers(payload: str) -> dict:
    ts = str(int(time.time() * 1000))
    recv_window = "5000"

    full = ts + BYBIT_API_KEY.strip() + recv_window + payload

    signature = hmac.new(
        BYBIT_API_SECRET.strip().encode(),
        full.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": BYBIT_API_KEY.strip(),
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
    }


# ─────────────────────────────────────────
# GET REQUEST
# ─────────────────────────────────────────

async def _get(endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}

    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    headers = _make_headers(query) if signed else {"Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{BASE_URL}{endpoint}",
                params=params,
                headers=headers,
                timeout=10
            )
            return r.json()

        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}


# ─────────────────────────────────────────
# POST REQUEST
# ─────────────────────────────────────────

async def _post(endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}

    body = json.dumps(params, separators=(",", ":"))

    headers = _make_headers(body) if signed else {"Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{BASE_URL}{endpoint}",
                content=body,
                headers=headers,
                timeout=10
            )
            return r.json()

        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}


# ─────────────────────────────────────────
# PRICE
# ─────────────────────────────────────────

async def get_price(symbol: str) -> float:
    symbol = normalize_symbol(symbol)

    data = await _get(
        "/v5/market/tickers",
        {"category": "linear", "symbol": symbol}
    )

    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


# ─────────────────────────────────────────
# LEVERAGE
# ─────────────────────────────────────────

async def set_leverage(symbol: str, leverage: int) -> bool:
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

    return data and data.get("retCode") in [0, 110043]


# ─────────────────────────────────────────
# PLACE ORDER (MAIN)
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

    price = await get_price(symbol)

    if price == 0:
        return {"success": False, "error": "No price found"}

    await set_leverage(symbol, leverage)

    position_value = usdt_amount * leverage
    qty = round(position_value / price, 3)

    if side == "Buy":
        sl = price * (1 - sl_pct / 100)
        tp = price * (1 + tp_pct / 100)
    else:
        sl = price * (1 + sl_pct / 100)
        tp = price * (1 - tp_pct / 100)

    data = await _post(
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "stopLoss": str(round(sl, 4)),
            "takeProfit": str(round(tp, 4)),
        },
        signed=True
    )

    if data and data.get("retCode") == 0:
        return {
            "success": True,
            "order_id": data["result"]["orderId"],
            "symbol": symbol,
            "side": side,
            "entry_price": price,
            "sl_price": round(sl, 4),
            "tp_price": round(tp, 4),
            "qty": qty,
            "leverage": leverage,
            "usdt_amount": usdt_amount,
        }

    return {
        "success": False,
        "error": data.get("retMsg", "No response") if data else "No response"
    }