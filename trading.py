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

BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

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
    symbol = symbol.replace(".P", "").replace("BINANCE:", "").replace("BYBIT:", "").replace("/", "").replace("-", "")

    if symbol in VALID_SYMBOLS:
        return VALID_SYMBOLS[symbol]

    if symbol.endswith("USD") and not symbol.endswith("USDT"):
        symbol += "T"

    if symbol.endswith("USDTUSDT"):
        symbol = symbol.replace("USDTUSDT", "USDT")

    return symbol


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


async def _get(endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    headers = _make_headers(query) if signed else {"Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{BASE_URL}{endpoint}", params=params, headers=headers, timeout=10)
            return r.json()

        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}


async def _post(endpoint: str, params: dict = None, signed: bool = False):
    params = params or {}
    body = json.dumps(params, separators=(",", ":"))

    headers = _make_headers(body) if signed else {"Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(f"{BASE_URL}{endpoint}", content=body, headers=headers, timeout=10)
            return r.json()
        except Exception as e:
            return {"retCode": -1, "retMsg": str(e)}


async def get_price(symbol: str) -> float:
    symbol = normalize_symbol(symbol)

    data = await _get("/v5/market/tickers", {"category": "linear", "symbol": symbol})

    try:
        return float(data["result"]["list"][0]["lastPrice"])
    except:
        return 0.0