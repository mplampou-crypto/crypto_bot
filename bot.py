import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

from config import (
    TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID,
    DEFAULT_LEVERAGE, DEFAULT_USDT,
    MIN_SIGNAL_SCORE, PAYSAFE_CODE_LENGTH, SUBSCRIPTION_PRICE,
    SUBSCRIPTION_DAYS, MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES,
    TRADING_PAIRS
)
from database import (
    init_db, get_user, create_user, is_subscribed, get_sub_expiry,
    set_subscription_pending, approve_subscription, deactivate_subscription,
    get_pending_subscriptions, save_trade, close_trade, update_trade_sl,
    get_last_trades, get_stats, save_rejected_signal,
    get_today_trades_count, get_consecutive_losses,
    get_expiring_subs, get_expired_subs, get_open_trades
)
from trading import (
    place_order, get_wallet_balance, get_price, fix_symbol,
    get_closed_pnl, is_position_open, get_open_positions,
    close_position_market, update_position_tp_sl,
    format_trade_message, format_rejected_message,
    format_closed_trade_message, format_signal_message,
    parse_webhook
)
from sentiment import get_market_sentiment, format_sentiment_message, estimate_price_target

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def main_keyboard(is_admin=False):
    keys = [
        [KeyboardButton("📊 Stats"),     KeyboardButton("💰 Balance")],
        [KeyboardButton("📰 Sentiment"), KeyboardButton("📈 Open Trades")],
        [KeyboardButton("ℹ️ Help")],
    ]
    if is_admin:
        keys.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)


async def broadcast(application, message):
    import asyncpg
    from config import DATABASE_URL
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT chat_id FROM users WHERE is_subscribed=TRUE")
        for row in rows:
            try:
                await application.bot.send_message(row["chat_id"], message, parse_mode=ParseMode.HTML)
            except:
                pass
    finally:
        await conn.close()


async def get_open_symbols():
    return [t["symbol"] for t in await get_open_trades()]


# ─── /start ───────────────────────────────────────────────

async def start(update, context):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or "User"
    await create_user(chat_id, username)
    if await is_subscribed(chat_id):
        expiry = await get_sub_expiry(chat_id)
        await update.message.reply_text(
            f"👋 <b>@{username}</b>!\n✅ Συνδρομή έως: <b>{expiry.strftime('%d/%m/%Y') if expiry else '—'}</b>",
            reply_markup=main_keyboard(chat_id == ADMIN_CHAT_ID),
            parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            f"👋 <b>CryptoSniper Bot</b>\n🤖 {DEFAULT_USDT} USDT / {DEFAULT_LEVERAGE}x\n💰 <b>{SUBSCRIPTION_PRICE}€/μήνα</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"💳 Εγγραφή {SUBSCRIPTION_PRICE}€", callback_data="subscribe")]]))


async def subscribe_callback(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        f"💳 Αγόρασε Paysafe {SUBSCRIPTION_PRICE}€ και στείλε τον {PAYSAFE_CODE_LENGTH}-ψήφιο κωδικό 👇",
        parse_mode=ParseMode.HTML)
    context.user_data["awaiting_paysafe"] = True


async def handle_paysafe(update, context):
    chat_id = update.effective_chat.id
    code = update.message.text.strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) != PAYSAFE_CODE_LENGTH:
        await update.message.reply_text(f"❌ Πρέπει {PAYSAFE_CODE_LENGTH} ψηφία.")
        return
    context.user_data["awaiting_paysafe"] = False
    await set_subscription_pending(chat_id, code)
    user = await get_user(chat_id)
    await context.bot.send_message(ADMIN_CHAT_ID,
        f"🔔 <b>Νέα συνδρομή!</b>\n👤 @{user['username'] if user else 'Unknown'} | <code>{chat_id}</code>\n"
        f"💳 <code>{code}</code>\n\n/approve {chat_id}  /reject {chat_id}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅", callback_data=f"APPROVE:{chat_id}"),
            InlineKeyboardButton("❌", callback_data=f"REJECT:{chat_id}")]]))
    await update.message.reply_text("⏳ Κωδικός ελήφθη!")


async def approve_callback(update, context):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    parts = query.data.split(":", 1)
    action, uid = parts[0], int(parts[1])
    await query.answer()
    if action == "APPROVE":
        await approve_subscription(uid)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"✅ {uid}")
        try:
            await context.bot.send_message(uid,
                f"🎉 <b>Συνδρομή ενεργή — {SUBSCRIPTION_DAYS} μέρες!</b>\n/start",
                parse_mode=ParseMode.HTML)
        except:
            pass
    else:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(f"❌ {uid}")
        try:
            await context.bot.send_message(uid, "❌ <b>Απορρίφθηκε.</b>", parse_mode=ParseMode.HTML)
        except:
            pass


# ─── ADMIN COMMANDS ───────────────────────────────────────

async def approve_command(update, context):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("/approve <id>")
        return
    try:
        uid = int(context.args[0])
        await approve_subscription(uid)
        await update.message.reply_text(f"✅ {uid}")
        await context.bot.send_message(uid,
            f"🎉 <b>Συνδρομή ενεργή — {SUBSCRIPTION_DAYS} μέρες!</b>",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def reject_command(update, context):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("/reject <id>")
        return
    try:
        uid = int(context.args[0])
        await update.message.reply_text(f"❌ {uid}")
        await context.bot.send_message(uid, "❌ <b>Απορρίφθηκε.</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def ip_command(update, context):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.ipify.org", timeout=10)
            await update.message.reply_text(
                f"🌐 Railway IP: <code>{resp.text}</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def debug_command(update, context):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    from config import BYBIT_API_KEY, BYBIT_API_SECRET
    await update.message.reply_text(
        f"🔍 <b>Debug:</b>\n\n"
        f"Key length: <b>{len(BYBIT_API_KEY)}</b>\n"
        f"Secret length: <b>{len(BYBIT_API_SECRET)}</b>\n"
        f"Has spaces: <b>{' ' in BYBIT_API_SECRET}</b>\n"
        f"Has newline: <b>{chr(10) in BYBIT_API_SECRET or chr(13) in BYBIT_API_SECRET}</b>",
        parse_mode=ParseMode.HTML)


async def sync_command(update, context):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    await update.message.reply_text("⏳ Sync...")
    closed = 0
    try:
        open_trades  = await get_open_trades()
        closed_bybit = await get_closed_pnl(limit=50)
        sym_pnl = {}
        for c in closed_bybit:
            s = c.get("symbol", "")
            if s and s not in sym_pnl:
                sym_pnl[s] = float(c.get("closedPnl", 0))
        for trade in open_trades:
            sym = fix_symbol(trade["symbol"])
            if not await is_position_open(sym):
                pnl    = sym_pnl.get(sym, 0.0)
                result = "WIN" if pnl >= 0 else "LOSS"
                await close_trade(trade["id"], result, round(pnl, 2))
                await broadcast(context.application,
                    format_closed_trade_message(sym, trade["side"], pnl, result))
                closed += 1
        await update.message.reply_text(f"✅ {closed} synced" if closed else "✅ Όλα OK")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


async def forceclose_command(update, context):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    open_trades = await get_open_trades()
    if not open_trades:
        await update.message.reply_text("📭 Κανένα ανοιχτό.")
        return
    if not context.args:
        msg = f"📌 <b>{len(open_trades)} ανοιχτά:</b>\n"
        for t in open_trades:
            msg += f"• {t['symbol']} {'L' if t['side']=='Buy' else 'S'} entry=${t['entry_price']:,.2f}\n"
        msg += "\n/forceclose <pnl>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return
    try:
        pnl    = float(context.args[0])
        result = "WIN" if pnl >= 0 else "LOSS"
        for t in open_trades:
            await close_trade(t["id"], result, pnl)
            await broadcast(context.application,
                format_closed_trade_message(t["symbol"], t["side"], pnl, result))
        await update.message.reply_text(f"✅ {len(open_trades)} closed ({'+' if pnl>=0 else ''}{pnl})")
    except ValueError:
        await update.message.reply_text("❌ /forceclose <αριθμός>")


# ─── STATS / OPEN / BALANCE / SENTIMENT ──────────────────

async def stats_command(update, context):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ /start")
        return
    stats  = await get_stats()
    trades = await get_last_trades(20)
    msg = (
        f"📊 <b>Stats</b>\n\n"
        f"✅ {stats['wins']}W  ❌ {stats['losses']}L  📈 {stats['winrate']}%\n"
        f"💰 PnL: <b>{'+' if stats['total_pnl']>=0 else ''}{stats['total_pnl']} USDT</b>\n"
        f"📅 Σήμερα: {stats['today_trades']}t | "
        f"{'+' if stats['today_pnl']>=0 else ''}{stats['today_pnl']} USDT\n\n"
        f"━━━━━━━━━━━━━━━━━\n<b>Τελευταία {len(trades)} Trades:</b>\n\n"
    )
    for t in trades:
        pnl = t["pnl_usdt"] or 0
        e   = "🟢" if t["result"] == "WIN" else "🔴"
        s   = "+" if pnl >= 0 else ""
        msg += f"{e} <b>{t['symbol'].replace('USDT','')}</b> {'L' if t['side']=='Buy' else 'S'} {s}{pnl:.2f} USDT\n"
    if not trades:
        msg += "<i>Δεν υπάρχουν κλειστά trades.</i>\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def open_trades_command(update, context):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌")
        return
    trades = await get_open_trades()
    if not trades:
        await update.message.reply_text("📭 Κανένα ανοιχτό.")
        return
    msg = "📈 <b>Ανοιχτές Θέσεις:</b>\n\n"
    for t in trades:
        cur = await get_price(t["symbol"])
        if t["side"] == "Buy":
            pnl = round((cur - t["entry_price"]) / t["entry_price"]
                        * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        else:
            pnl = round((t["entry_price"] - cur) / t["entry_price"]
                        * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        e = "🟢" if t["side"] == "Buy" else "🔴"
        msg += (
            f"{e} <b>{t['symbol'].replace('USDT','')}</b>\n"
            f"  Entry: ${t['entry_price']:,.4f} → Now: ${cur:,.4f}\n"
            f"  SL: ${t['sl_price']:,.4f}\n"
            f"  PnL: <b>{'+' if pnl>=0 else ''}{pnl} USDT</b>\n\n"
        )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def balance_command(update, context):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌")
        return
    b = await get_wallet_balance()
    await update.message.reply_text(f"💰 <b>Bybit:</b> {b:,.2f} USDT", parse_mode=ParseMode.HTML)


async def sentiment_command(update, context):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌")
        return
    await update.message.reply_text("📰 Διάλεξε:", parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("₿ BTC", callback_data="sent_BTCUSDT"),
             InlineKeyboardButton("Ξ ETH", callback_data="sent_ETHUSDT")],
            [InlineKeyboardButton("◎ SOL", callback_data="sent_SOLUSDT"),
             InlineKeyboardButton("✕ XRP", callback_data="sent_XRPUSDT")],
            [InlineKeyboardButton("🐕 DOGE", callback_data="sent_DOGEUSDT")]]))


async def sentiment_callback(update, context):
    query  = update.callback_query
    symbol = query.data.replace("sent_", "")
    await query.answer()
    await query.message.reply_text("⏳ Αναλύω...")
    sentiment = await get_market_sentiment(symbol)
    cur  = await get_price(symbol)
    side = "LONG" if sentiment["score"] >= 0 else "SHORT"
    pt   = estimate_price_target(symbol, cur, sentiment["score"], side)
    await query.message.reply_text(
        format_sentiment_message(symbol, sentiment, pt),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True)


# ─── MONITOR: κλειστά trades ──────────────────────────────

async def monitor_closed_trades(application):
    while True:
        await asyncio.sleep(60)
        try:
            open_trades  = await get_open_trades()
            if not open_trades:
                continue
            closed_bybit = await get_closed_pnl(limit=50)
            sym_pnl = {}
            for c in closed_bybit:
                s = c.get("symbol", "")
                if s and s not in sym_pnl:
                    sym_pnl[s] = float(c.get("closedPnl", 0))
            for trade in open_trades:
                sym = fix_symbol(trade["symbol"])
                if not await is_position_open(sym):
                    pnl    = sym_pnl.get(sym, 0.0)
                    result = "WIN" if pnl >= 0 else "LOSS"
                    await close_trade(trade["id"], result, round(pnl, 2))
                    await broadcast(application,
                        format_closed_trade_message(sym, trade["side"], pnl, result))
                    logger.info(f"Trade closed: {sym} {result} {pnl:.2f}")
        except Exception as e:
            logger.error(f"Monitor error: {e}")


async def check_expiring_subs(application):
    while True:
        try:
            for user in await get_expiring_subs(days_ahead=1):
                try:
                    await application.bot.send_message(user["chat_id"],
                        "⚠️ <b>Συνδρομή λήγει αύριο!</b> /start", parse_mode=ParseMode.HTML)
                except:
                    pass
            for user in await get_expired_subs():
                await deactivate_subscription(user["chat_id"])
                try:
                    await application.bot.send_message(user["chat_id"],
                        "⏰ <b>Συνδρομή έληξε!</b> /start", parse_mode=ParseMode.HTML)
                except:
                    pass
        except Exception as e:
            logger.error(f"Sub error: {e}")
        await asyncio.sleep(43200)


# ═══════════════════════════════════════════════════════════════════
# WEBHOOK HANDLER — διαβάζει το JSON του WS Pro indicator
# ═══════════════════════════════════════════════════════════════════

async def handle_tradingview_webhook(parsed: dict, application):
    """
    parsed = αποτέλεσμα parse_webhook() από trading.py
    Περιέχει: symbol, action, side, bybit_side, price,
              entry, sl, tp, tf, time, is_entry, is_exit
    """
    symbol     = parsed["symbol"]
    side       = parsed["side"]          # LONG / SHORT
    bybit_side = parsed["bybit_side"]    # Buy / Sell
    action     = parsed["action"]        # BUY / SELL / TP / STOP
    sl_price   = parsed["sl"]
    tp_price   = parsed["tp"]

    # ── Φιλτράρισμα — μόνο επιτρεπόμενα pairs ──
    if symbol not in TRADING_PAIRS:
        logger.info(f"Signal ignored — {symbol} not in TRADING_PAIRS")
        return

    # ── EXIT signals (TP / STOP) — κλείσιμο θέσης ──
    if parsed["is_exit"]:
        open_trades = await get_open_trades()
        for trade in open_trades:
            if fix_symbol(trade["symbol"]) == symbol and trade["side"] == bybit_side:
                ok = await close_position_market(symbol, trade["side"], trade["qty"])
                if ok:
                    cur     = await get_price(symbol)
                    pnl_est = 0.0
                    if cur > 0:
                        if trade["side"] == "Buy":
                            pnl_est = round(
                                (cur - trade["entry_price"]) / trade["entry_price"]
                                * 100 * trade["leverage"] * trade["usdt_amount"] / 100, 2)
                        else:
                            pnl_est = round(
                                (trade["entry_price"] - cur) / trade["entry_price"]
                                * 100 * trade["leverage"] * trade["usdt_amount"] / 100, 2)
                    result = "WIN" if pnl_est >= 0 else "LOSS"
                    await close_trade(trade["id"], result, pnl_est)
                    reason = "TP Hit ✅" if action == "TP" else "SL Hit ❌"
                    await broadcast(application,
                        format_closed_trade_message(symbol, trade["side"], pnl_est, result)
                        + f"\n\n<i>🔔 {reason} — WS Pro</i>")
                    logger.info(f"Exit signal: {symbol} {action} pnl={pnl_est:.2f}")
        return

    # ── ENTRY signals (BUY / SELL) ──
    if not parsed["is_entry"]:
        return

    open_trades = await get_open_trades()

    # Αν υπάρχει ήδη ανοιχτό trade στην ίδια κατεύθυνση → skip
    existing = next((t for t in open_trades
                     if fix_symbol(t["symbol"]) == symbol
                     and t["side"] == bybit_side), None)
    if existing:
        logger.info(f"Signal ignored — {symbol} {bybit_side} already open")
        return

    # Αν υπάρχει ανοιχτό trade στην ΑΝΤΙΘΕΤΗ κατεύθυνση → skip
    opposite = next((t for t in open_trades
                     if fix_symbol(t["symbol"]) == symbol
                     and t["side"] != bybit_side), None)
    if opposite:
        await save_rejected_signal(symbol, side, 90, "Αντίθετο trade ανοιχτό")
        await application.bot.send_message(ADMIN_CHAT_ID,
            f"⏸ <b>Skip</b> — {symbol} έχει αντίθετο trade", parse_mode=ParseMode.HTML)
        return

    # Daily / consecutive loss limits
    if await get_today_trades_count() >= MAX_DAILY_TRADES:
        await save_rejected_signal(symbol, side, 90, "Ημερήσιο όριο")
        return
    if await get_consecutive_losses() >= MAX_CONSECUTIVE_LOSSES:
        await save_rejected_signal(symbol, side, 90, "Consecutive losses")
        return

    # Sentiment filter
    sentiment_data = await get_market_sentiment(symbol)
    if bybit_side == "Buy" and sentiment_data["score"] < -20:
        await save_rejected_signal(symbol, side, 90, "Bearish sentiment")
        await broadcast(application,
            format_rejected_message(symbol, side, 90, "Bearish sentiment"))
        return
    if bybit_side == "Sell" and sentiment_data["score"] > 20:
        await save_rejected_signal(symbol, side, 90, "Bullish sentiment")
        await broadcast(application,
            format_rejected_message(symbol, side, 90, "Bullish sentiment"))
        return

    # Ενημέρωση admin για signal πριν ανοίξει η θέση
    await application.bot.send_message(ADMIN_CHAT_ID,
        format_signal_message(parsed), parse_mode=ParseMode.HTML)

    # Place Order — SL και TP από το Pine Script
    result = await place_order(
        symbol      = symbol,
        side        = bybit_side,
        usdt_amount = DEFAULT_USDT,
        leverage    = DEFAULT_LEVERAGE,
        sl_price    = sl_price,
        tp_price    = tp_price,
    )

    if result["success"]:
        await save_trade(
            symbol, bybit_side,
            result["entry_price"], result["sl_price"], result["tp_price"],
            DEFAULT_LEVERAGE, DEFAULT_USDT,
            result["qty"], result["order_id"],
            90, "WS_Pro"
        )
        await broadcast(application, format_trade_message(result))
        logger.info(f"Trade opened: {symbol} {bybit_side} entry={result['entry_price']}")
    else:
        await application.bot.send_message(ADMIN_CHAT_ID,
            f"❌ <b>Trade failed</b>\n{symbol} {bybit_side}\n"
            f"<code>{result.get('error')}</code>",
            parse_mode=ParseMode.HTML)


# ─── ADMIN PANEL ──────────────────────────────────────────

async def admin_panel(update, context):
    stats   = await get_stats()
    balance = await get_wallet_balance()
    pending = await get_pending_subscriptions()
    open_t  = await get_open_trades()
    msg = (
        f"👑 <b>Admin Panel</b>\n\n💰 {balance:,.2f} USDT\n"
        f"📊 {stats['total']}t ({stats['wins']}W/{stats['losses']}L) "
        f"PnL: {'+' if stats['total_pnl']>=0 else ''}{stats['total_pnl']}\n"
        f"📌 Ανοιχτά: {len(open_t)}\n"
        f"⏳ Pending: {len(pending)}\n\n"
        f"/approve /reject /sync /forceclose /ip /debug"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Pending",   callback_data="AP"),
            InlineKeyboardButton("📈 Positions", callback_data="BP")]]))


async def ap_callback(update, context):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    await query.answer()
    subs = await get_pending_subscriptions()
    if not subs:
        await query.message.reply_text("Κανένα pending.")
        return
    for sub in subs:
        await query.message.reply_text(
            f"👤 @{sub['username']} | <code>{sub['chat_id']}</code>\n"
            f"💳 <code>{sub['paysafe_code']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅", callback_data=f"APPROVE:{sub['chat_id']}"),
                InlineKeyboardButton("❌", callback_data=f"REJECT:{sub['chat_id']}")]]))


async def bp_callback(update, context):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    await query.answer()
    positions = await get_open_positions()
    if not positions:
        await query.message.reply_text("Κανένα position.")
        return
    msg = "📈 <b>Bybit:</b>\n\n"
    for p in positions:
        pnl = float(p.get("unrealisedPnl", 0))
        msg += (f"• {p['symbol']} {p['side']} size={p['size']} "
                f"PnL={'+' if pnl>=0 else ''}{pnl:.2f}\n")
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── MESSAGE HANDLER ──────────────────────────────────────

async def handle_message(update, context):
    chat_id = update.effective_chat.id
    text    = update.message.text.strip() if update.message.text else ""
    if context.user_data.get("awaiting_paysafe"):
        await handle_paysafe(update, context)
        return
    if text == "📊 Stats":
        await stats_command(update, context)
    elif text == "💰 Balance":
        await balance_command(update, context)
    elif text == "📰 Sentiment":
        await sentiment_command(update, context)
    elif text == "📈 Open Trades":
        await open_trades_command(update, context)
    elif text == "ℹ️ Help":
        await update.message.reply_text(
            f"ℹ️ <b>Help</b>\n\n/start /sync /forceclose /ip /debug\n"
            f"📊 Stats 💰 Balance 📰 Sentiment 📈 Open\n\n"
            f"<b>Features:</b>\n"
            f"• SL/TP από WS Pro indicator\n"
            f"• Auto-close σε TP/STOP signal\n"
            f"• Sentiment filter\n"
            f"• Daily/consecutive loss limit",
            parse_mode=ParseMode.HTML)
    elif text == "👑 Admin Panel" and chat_id == ADMIN_CHAT_ID:
        await admin_panel(update, context)


# ─── AIOHTTP WEBHOOK ──────────────────────────────────────

from aiohttp import web


async def tradingview_handler(request):
    """
    Δέχεται POST από TradingView webhook.
    Το body είναι το JSON που στέλνει ο WS Pro indicator.
    """
    try:
        raw = await request.json()
        logger.info(f"Webhook received: {raw}")

        app    = request.app["telegram_app"]
        parsed = parse_webhook(raw)

        # Άγνωστο action → ignore
        if parsed["action"] not in ["BUY", "SELL", "TP", "STOP"]:
            logger.warning(f"Unknown action: {parsed['action']}")
            return web.json_response({"status": "ignored", "reason": "unknown action"})

        asyncio.create_task(handle_tradingview_webhook(parsed, app))
        return web.json_response({"status": "ok", "action": parsed["action"]})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=400)


async def health_handler(request):
    return web.json_response({"status": "running"})


# ─── MAIN ─────────────────────────────────────────────────

async def main():
    await init_db()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start",      start))
    application.add_handler(CommandHandler("stats",      stats_command))
    application.add_handler(CommandHandler("balance",    balance_command))
    application.add_handler(CommandHandler("approve",    approve_command))
    application.add_handler(CommandHandler("reject",     reject_command))
    application.add_handler(CommandHandler("sync",       sync_command))
    application.add_handler(CommandHandler("forceclose", forceclose_command))
    application.add_handler(CommandHandler("ip",         ip_command))
    application.add_handler(CommandHandler("debug",      debug_command))

    application.add_handler(CallbackQueryHandler(subscribe_callback, pattern="^subscribe$"))
    application.add_handler(CallbackQueryHandler(approve_callback,   pattern="^(APPROVE|REJECT):"))
    application.add_handler(CallbackQueryHandler(sentiment_callback, pattern="^sent_"))
    application.add_handler(CallbackQueryHandler(ap_callback,        pattern="^AP$"))
    application.add_handler(CallbackQueryHandler(bp_callback,        pattern="^BP$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    aio_app = web.Application()
    aio_app["telegram_app"] = application
    aio_app.router.add_post("/webhook", tradingview_handler)
    aio_app.router.add_get("/health",   health_handler)
    aio_app.router.add_get("/",         health_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True)

    port   = int(__import__("os").environ.get("PORT", 8080))
    runner = web.AppRunner(aio_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🚀 Bot on port {port}")

    # Background tasks
    asyncio.create_task(monitor_closed_trades(application))
    asyncio.create_task(check_expiring_subs(application))

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
