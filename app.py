"""
Bybit webhook trading bot.

TradingView alert (JSON)  ->  /webhook  ->  market trade on Bybit
                                        ->  per-strategy tracking + dashboard

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import json
import logging
import os
import threading
import time
from decimal import Decimal

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

import database as db
from bybit_client import BybitClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

load_dotenv()

# ── config / env ────────────────────────────────────────────────
with open("config.yaml") as f:
    CONFIG = yaml.safe_load(f)

STRATEGIES = CONFIG.get("strategies", {})
SYMBOLS = CONFIG.get("symbols", {})
TAKER_FEE = float(CONFIG.get("taker_fee", 0.00055))

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

bybit = BybitClient(
    api_key=os.getenv("BYBIT_API_KEY", ""),
    api_secret=os.getenv("BYBIT_API_SECRET", ""),
    testnet=TESTNET,
)

# serialize all order logic so net position state stays consistent
_exec_lock = threading.Lock()
# simple de-dup against TradingView retries (same alert within N seconds)
_last_alert = {}
_DEDUP_SECONDS = 3

app = FastAPI(title="Bybit Webhook Bot")

with open(os.path.join("templates", "dashboard.html")) as f:
    DASHBOARD_HTML = f.read()


@app.on_event("startup")
def _startup():
    db.init_db()
    for sym, scfg in SYMBOLS.items():
        try:
            bybit.configure_symbol(sym, scfg.get("leverage", 1))
            log.info("configured %s (leverage %s, testnet=%s)",
                     sym, scfg.get("leverage", 1), TESTNET)
        except Exception as e:
            log.error("could not configure %s: %s", sym, e)
    log.info("ready — %d strategies loaded", len(STRATEGIES))


# ════════════════════════════════════════════════════════════════
#  Core trading logic
# ════════════════════════════════════════════════════════════════
def _desired_side(action):
    # only buy/sell — nothing else
    a = (action or "").strip().lower()
    if a in ("buy", "long"):
        return "long"
    if a in ("sell", "short"):
        return "short"
    return None


def _record_close(pos, exit_price):
    """Close an open per-strategy position and log the trade (net of fees)."""
    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    if qty <= 0 or entry <= 0:
        return
    gross = (exit_price - entry) * qty if pos["side"] == "long" \
        else (entry - exit_price) * qty
    fees = (entry + exit_price) * qty * TAKER_FEE
    pnl = gross - fees
    notional = entry * qty
    pnl_pct = (pnl / notional * 100) if notional else 0.0
    db.insert_trade(
        strategy=pos["strategy"], symbol=pos["symbol"], side=pos["side"],
        qty=qty, entry_price=entry, exit_price=exit_price,
        pnl=pnl, pnl_pct=pnl_pct, win=1 if pnl > 0 else 0,
        entry_time=pos.get("entry_time"),
    )
    log.info("CLOSE %s %s qty=%s pnl=%.2f", pos["strategy"], pos["side"],
             qty, pnl)


def _reconcile(symbol):
    """Net all strategy positions for a symbol -> one Bybit market order."""
    legs = db.get_open_positions_for_symbol(symbol)
    target = Decimal("0")
    for leg in legs:
        q = Decimal(str(leg["qty"]))
        target += q if leg["side"] == "long" else -q

    sign = 1 if target >= 0 else -1
    target = bybit.round_qty(symbol, abs(target)) * sign

    current = bybit.position_size(symbol)
    delta = target - current
    step = bybit.instrument(symbol)["qty_step"]

    if abs(delta) < step:
        return  # already aligned

    side = "Buy" if delta > 0 else "Sell"
    bybit.market(symbol, side, abs(delta))


def process_alert(data: dict) -> dict:
    # auth
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        log.warning("rejected alert: bad secret")
        return {"status": "rejected", "reason": "bad secret"}

    name = data.get("strategy")
    action = data.get("action")
    side = _desired_side(action)

    cfg = STRATEGIES.get(name)
    if not cfg:
        return {"status": "ignored", "reason": f"unknown strategy '{name}'"}
    if not cfg.get("enabled", False):
        return {"status": "ignored", "reason": f"'{name}' disabled"}
    if side is None:
        return {"status": "ignored", "reason": f"unknown action '{action}'"}

    symbol = data.get("symbol") or cfg["symbol"]

    # de-dup identical alerts from TradingView retries
    key = (name, side, symbol)
    now = time.time()
    if key in _last_alert and now - _last_alert[key] < _DEDUP_SECONDS:
        return {"status": "ignored", "reason": "duplicate"}
    _last_alert[key] = now

    with _exec_lock:
        try:
            price = bybit.last_price(symbol)
        except Exception as e:
            log.error("price fetch failed for %s: %s", symbol, e)
            return {"status": "error", "reason": "price fetch failed"}

        cur = db.get_strategy_position(name)
        cur_side = cur["side"] if cur else "flat"

        # if currently open and the signal changes the side -> close first
        if cur_side != "flat" and cur_side != side:
            _record_close(cur, price)
            db.set_strategy_position(name, symbol, "flat", 0, None, None)
            cur_side = "flat"

        # open a new position if going long/short from flat
        if side in ("long", "short") and cur_side == "flat":
            qty = bybit.round_qty(symbol, cfg["position_size_usdt"] / price)
            if qty < bybit.min_qty(symbol) or qty <= 0:
                return {"status": "error",
                        "reason": f"size too small (qty={qty})"}
            db.set_strategy_position(
                name, symbol, side, float(qty), price,
                db._now())
            log.info("OPEN %s %s qty=%s @ %.2f", name, side, qty, price)

        # bring Bybit position in line with the net of all strategies
        try:
            _reconcile(symbol)
        except Exception as e:
            log.error("reconcile failed for %s: %s", symbol, e)
            return {"status": "error", "reason": "order failed",
                    "detail": str(e)}

    return {"status": "ok", "strategy": name, "action": side,
            "symbol": symbol, "price": price}


# ════════════════════════════════════════════════════════════════
#  Routes
# ════════════════════════════════════════════════════════════════
@app.post("/webhook")
async def webhook(req: Request):
    raw = await req.body()
    try:
        data = json.loads(raw)
    except Exception:
        return JSONResponse({"status": "error", "reason": "invalid JSON"},
                            status_code=200)
    result = await run_in_threadpool(process_alert, data)
    # always 200 so TradingView doesn't spam retries
    return JSONResponse(result, status_code=200)


@app.get("/health")
def health():
    return {"ok": True}


def _build_stats():
    overall, per = db.get_stats()
    eq, avail = bybit.equity()

    strat_rows = []
    positions = {p["strategy"]: p for p in db.get_all_strategy_positions()}
    for name, cfg in STRATEGIES.items():
        s = per.get(name, {"trades": 0, "wins": 0, "losses": 0,
                           "win_rate": 0.0, "pnl": 0.0})
        pos = positions.get(name)
        strat_rows.append({
            "name": name,
            "enabled": cfg.get("enabled", False),
            "symbol": cfg.get("symbol"),
            "size_usdt": cfg.get("position_size_usdt"),
            "description": (cfg.get("description") or "").strip(),
            "backtest_win_rate": cfg.get("backtest_win_rate", "—"),
            "live": s,
            "open_side": pos["side"] if pos and pos["side"] != "flat" else None,
            "open_entry": pos["entry_price"] if pos and pos["side"] != "flat" else None,
        })

    return {
        "overall": overall,
        "equity": eq,
        "available": avail,
        "testnet": TESTNET,
        "strategies": strat_rows,
        "recent": db.get_recent_trades(25),
    }


@app.get("/api/stats")
def api_stats():
    return _build_stats()


@app.get("/", response_class=HTMLResponse)
def dashboard(token: str = ""):
    if DASHBOARD_TOKEN and token != DASHBOARD_TOKEN:
        return PlainTextResponse("Unauthorized", status_code=401)
    return HTMLResponse(DASHBOARD_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0",
                port=int(os.getenv("PORT", "8000")))
