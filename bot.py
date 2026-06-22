"""
crypto_bot — TradingView webhook -> Bybit market trades + Telegram.

Flow:
  TradingView alert (JSON)  ->  POST /webhook  ->  buy/sell only
                                              ->  market trade on Bybit (netted)
                                              ->  per-trader win-rate + Telegram

Run:  python bot.py
"""
import asyncio
import json
import logging
import time
from decimal import Decimal

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import leaderboard as lb
from executor import Executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

executor = Executor()

_exec_lock = asyncio.Lock()      # serialize order logic (keeps netting consistent)
_last_alert = {}                 # de-dup TradingView retries
_DEDUP_SECONDS = 3

bot = None                       # telegram Bot, set in main()
application = None


# ════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════
async def run_blocking(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


async def notify(text):
    """Send to Telegram (if configured) and always log."""
    log.info(text.replace("\n", " | "))
    if bot and config.TELEGRAM_CHAT_ID:
        try:
            await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID,
                                   text=text, parse_mode="HTML")
        except Exception as e:
            log.warning("telegram send failed: %s", e)


def desired_side(action):
    # buy/sell only — nothing else
    a = (action or "").strip().lower()
    if a in ("buy", "long"):
        return "long"
    if a in ("sell", "short"):
        return "short"
    return None


# ════════════════════════════════════════════════════════════════
#  Trade logic
# ════════════════════════════════════════════════════════════════
def _close_pnl(pos, exit_price):
    qty = float(pos["qty"])
    entry = float(pos["entry_price"])
    gross = (exit_price - entry) * qty if pos["side"] == "long" \
        else (entry - exit_price) * qty
    fees = (entry + exit_price) * qty * config.TAKER_FEE
    pnl = gross - fees
    notional = entry * qty
    pnl_pct = (pnl / notional * 100) if notional else 0.0
    return pnl, pnl_pct


async def _reconcile(symbol):
    """Net all traders on a symbol into a single Bybit position."""
    legs = lb.open_positions_for_symbol(symbol)
    target = Decimal("0")
    for leg in legs:
        q = Decimal(str(leg["qty"]))
        target += q if leg["side"] == "long" else -q

    sign = 1 if target >= 0 else -1
    target = await run_blocking(executor.round_qty, symbol, abs(target))
    target = target * sign

    current = await run_blocking(executor.position_size, symbol)
    delta = target - current
    step = await run_blocking(executor.qty_step, symbol)

    if abs(delta) < step:
        return
    side = "Buy" if delta > 0 else "Sell"
    await run_blocking(executor.market, symbol, side, abs(delta))


async def process_signal(data: dict) -> dict:
    # auth
    if config.WEBHOOK_SECRET and data.get("secret") != config.WEBHOOK_SECRET:
        log.warning("rejected: bad secret")
        return {"status": "rejected", "reason": "bad secret"}

    name = data.get("strategy") or data.get("trader")
    side = desired_side(data.get("action"))

    cfg = config.TRADERS.get(name)
    if not cfg:
        return {"status": "ignored", "reason": f"unknown trader '{name}'"}
    if not cfg.get("enabled", False):
        return {"status": "ignored", "reason": f"'{name}' disabled"}
    if side is None:
        return {"status": "ignored", "reason": "action must be buy or sell"}

    symbol = data.get("symbol") or cfg["symbol"]

    # de-dup identical alerts (TradingView retries)
    key = (name, side, symbol)
    t = time.time()
    if key in _last_alert and t - _last_alert[key] < _DEDUP_SECONDS:
        return {"status": "ignored", "reason": "duplicate"}
    _last_alert[key] = t

    async with _exec_lock:
        try:
            price = await run_blocking(executor.last_price, symbol)
        except Exception as e:
            log.error("price fetch failed (%s): %s", symbol, e)
            return {"status": "error", "reason": "price fetch failed"}

        cur = lb.get_position(name)
        cur_side = cur["side"] if cur else "flat"

        # flip: close opposite position first, record the trade
        if cur_side != "flat" and cur_side != side:
            pnl, pnl_pct = _close_pnl(cur, price)
            win = 1 if pnl > 0 else 0
            lb.record_trade(name, symbol, cur_side, float(cur["qty"]),
                            float(cur["entry_price"]), price, pnl, pnl_pct,
                            win, cur.get("entry_time"))
            lb.set_position(name, symbol, "flat", 0, None, None)
            res = "✅ WIN" if win else "❌ LOSS"
            sign = "+" if pnl >= 0 else ""
            await notify(
                f"🔴 <b>{name}</b> closed {cur_side.upper()} {symbol}\n"
                f"PnL {sign}{pnl:.2f} USDT ({sign}{pnl_pct:.2f}%)  {res}")
            cur_side = "flat"

        # open new position from flat
        if cur_side == "flat":
            qty = await run_blocking(
                executor.round_qty, symbol, cfg["size_usdt"] / price)
            min_q = await run_blocking(executor.min_qty, symbol)
            if qty < min_q or qty <= 0:
                return {"status": "error",
                        "reason": f"size too small (qty={qty})"}
            lb.set_position(name, symbol, side, float(qty), price, lb.now_iso())
            await notify(
                f"🟢 <b>{name}</b> {side.upper()} {symbol} @ {price}\n"
                f"size {cfg['size_usdt']} USDT")

        # align Bybit position with the net of all traders
        try:
            await _reconcile(symbol)
        except Exception as e:
            log.error("reconcile failed (%s): %s", symbol, e)
            await notify(f"⚠️ Order error on {symbol}: {e}")
            return {"status": "error", "reason": "order failed", "detail": str(e)}

    return {"status": "ok", "trader": name, "action": side,
            "symbol": symbol, "price": price}


# ════════════════════════════════════════════════════════════════
#  HTTP endpoints
# ════════════════════════════════════════════════════════════════
async def webhook(request: web.Request):
    raw = await request.text()
    try:
        data = json.loads(raw)
    except Exception:
        return web.json_response({"status": "error", "reason": "invalid JSON"})
    result = await process_signal(data)
    return web.json_response(result)


async def health(request: web.Request):
    return web.json_response({"ok": True})


# ════════════════════════════════════════════════════════════════
#  Telegram commands
# ════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"🤖 crypto_bot online.\nYour chat id: <code>{chat_id}</code>\n"
        "Set it as TELEGRAM_CHAT_ID to receive trade alerts.\n\n"
        "/stats · /leaderboard · /traders · /balance",
        parse_mode="HTML")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lb.format_stats(), parse_mode="HTML")


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lb.format_leaderboard(), parse_mode="HTML")


async def cmd_traders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lb.format_traders(), parse_mode="HTML")


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    eq, avail = await run_blocking(executor.equity)
    if eq is None:
        await update.message.reply_text("⚠️ Could not fetch balance.")
    else:
        await update.message.reply_text(
            f"💰 Equity: <b>{eq:.2f}</b> USDT\nAvailable: {avail:.2f} USDT",
            parse_mode="HTML")


# ════════════════════════════════════════════════════════════════
#  Startup
# ════════════════════════════════════════════════════════════════
async def main():
    global bot, application
    lb.init_db()

    # Telegram (optional — runs if a token is set)
    if config.TELEGRAM_BOT_TOKEN:
        application = Application.builder().token(
            config.TELEGRAM_BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("stats", cmd_stats))
        application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
        application.add_handler(CommandHandler("traders", cmd_traders))
        application.add_handler(CommandHandler("balance", cmd_balance))
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        bot = application.bot
        log.info("telegram bot started")
    else:
        log.info("no TELEGRAM_BOT_TOKEN — running without Telegram")

    # configure symbols (one-way + leverage)
    for sym, scfg in config.SYMBOLS.items():
        lev = scfg.get("leverage", 1) if isinstance(scfg, dict) else scfg
        try:
            await run_blocking(executor.configure_symbol, sym, lev)
            log.info("configured %s @ %sx (testnet=%s)",
                     sym, lev, config.BYBIT_TESTNET)
        except Exception as e:
            log.error("configure %s failed: %s", sym, e)

    # HTTP server
    app = web.Application()
    app.router.add_post("/webhook", webhook)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("listening on :%d  (%d traders)", config.PORT, len(config.TRADERS))

    await notify("🤖 <b>crypto_bot started</b> — "
                 f"{'TESTNET' if config.BYBIT_TESTNET else 'LIVE'}, "
                 f"{len(config.TRADERS)} traders")

    # run forever
    try:
        await asyncio.Event().wait()
    finally:
        if application:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("shutting down")
