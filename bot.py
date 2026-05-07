import asyncio
import logging
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
    DEFAULT_LEVERAGE, DEFAULT_USDT, DEFAULT_SL_PCT, DEFAULT_TP_PCT,
    MIN_SIGNAL_SCORE, MANUAL_HOUR_START, MANUAL_HOUR_END,
    PAYSAFE_CODE_LENGTH, SUBSCRIPTION_PRICE, SUBSCRIPTION_DAYS,
    MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES, TRAILING_STOP_ACTIVE
)
from database import (
    init_db, get_user, create_user, is_subscribed, get_sub_expiry,
    set_subscription_pending, approve_subscription,
    get_pending_subscriptions, save_trade, close_trade, update_trade_sl,
    get_last_trades, get_stats, save_pending_trade, get_pending_trade,
    delete_pending_trade, save_rejected_signal, get_today_trades_count,
    get_consecutive_losses, get_expiring_subs, get_open_trades
)
from trading import (
    place_order, get_wallet_balance, get_price, set_stop_loss,
    move_to_breakeven, update_trailing_stop,
    format_trade_message, format_rejected_message,
    format_breakeven_message, format_pending_trade_message
)
from sentiment import get_market_sentiment, format_sentiment_message, estimate_price_target

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── HELPERS ──────────────────────────────────────────────

def get_eet_hour() -> int:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).hour


def is_manual_window() -> bool:
    h = get_eet_hour()
    return MANUAL_HOUR_START <= h < MANUAL_HOUR_END


def main_keyboard(is_admin: bool = False):
    keys = [
        [KeyboardButton("📊 Stats"), KeyboardButton("💰 Balance")],
        [KeyboardButton("📰 Sentiment"), KeyboardButton("📈 Open Trades")],
        [KeyboardButton("ℹ️ Help")],
    ]
    if is_admin:
        keys.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)


async def broadcast(application: Application, message: str, parse_mode=ParseMode.HTML):
    """Στέλνει μήνυμα σε όλους τους subscribers"""
    import asyncpg
    from config import DATABASE_URL
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT chat_id FROM users WHERE is_subscribed=TRUE")
        for row in rows:
            try:
                await application.bot.send_message(row["chat_id"], message, parse_mode=parse_mode)
            except Exception as e:
                logger.warning(f"Failed to send to {row['chat_id']}: {e}")
    finally:
        await conn.close()


# ─── START ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or "User"
    await create_user(chat_id, username)
    subscribed = await is_subscribed(chat_id)

    if subscribed:
        expiry = await get_sub_expiry(chat_id)
        expiry_str = expiry.strftime("%d/%m/%Y") if expiry else "—"
        await update.message.reply_text(
            f"👋 Καλώς ήρθες, <b>@{username}</b>!\n\n"
            f"✅ Συνδρομή ενεργή έως: <b>{expiry_str}</b>\n"
            f"Χρησιμοποίησε τα κουμπιά παρακάτω:",
            reply_markup=main_keyboard(chat_id == ADMIN_CHAT_ID),
            parse_mode=ParseMode.HTML
        )
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💳 Εγγραφή — {SUBSCRIPTION_PRICE}€/μήνα", callback_data="subscribe")
        ]])
        await update.message.reply_text(
            f"👋 Καλώς ήρθες στο <b>CryptoSniper Bot</b>!\n\n"
            f"🤖 Τι κάνει το bot:\n"
            f"• Auto trading στο Bybit (50x leverage)\n"
            f"• Signals από TradingView + AI score\n"
            f"• News sentiment ανάλυση\n"
            f"• Break-even & trailing stop προστασία\n"
            f"• Stats & PnL tracking\n\n"
            f"💰 <b>Συνδρομή: {SUBSCRIPTION_PRICE}€/μήνα</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# ─── SUBSCRIPTION ─────────────────────────────────────────

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        f"💳 <b>Εγγραφή — {SUBSCRIPTION_PRICE}€/μήνα</b>\n\n"
        f"1️⃣ Αγόρασε <b>Paysafe {SUBSCRIPTION_PRICE}€</b> από περίπτερο\n"
        f"2️⃣ Στείλε μου τον <b>{PAYSAFE_CODE_LENGTH}-ψήφιο κωδικό</b>\n\n"
        f"Στείλε τον κωδικό τώρα:",
        parse_mode=ParseMode.HTML
    )
    context.user_data["awaiting_paysafe"] = True


async def handle_paysafe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    code = update.message.text.strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) != PAYSAFE_CODE_LENGTH:
        await update.message.reply_text(
            f"❌ Ο κωδικός πρέπει να έχει <b>ακριβώς {PAYSAFE_CODE_LENGTH} ψηφία</b>.\nΔοκίμασε ξανά:",
            parse_mode=ParseMode.HTML
        )
        return
    context.user_data["awaiting_paysafe"] = False
    await set_subscription_pending(chat_id, code)
    user = await get_user(chat_id)
    username = user["username"] if user else "Unknown"
    await context.bot.send_message(
        ADMIN_CHAT_ID,
        f"🔔 <b>Νέο αίτημα συνδρομής!</b>\n\n"
        f"User: @{username} (ID: {chat_id})\n"
        f"Paysafe: <code>{code}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{chat_id}"),
        ]])
    )
    await update.message.reply_text(
        "⏳ Κωδικός ελήφθη! Θα ειδοποιηθείς μόλις γίνει επαλήθευση.",
        parse_mode=ParseMode.HTML
    )


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌ Δεν έχεις δικαίωμα!", show_alert=True)
        return
    data = query.data
    action     = data.split("_")[0]
    user_chat_id = int(data.split("_")[1])
    if action == "approve":
        await approve_subscription(user_chat_id)
        await query.answer("✅ Εγκρίθηκε!")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(
            user_chat_id,
            f"🎉 <b>Συνδρομή Ενεργοποιήθηκε!</b>\n\n"
            f"✅ {SUBSCRIPTION_DAYS} μέρες ενεργή συνδρομή!\n"
            f"Πάτα /start για να δεις το μενού.",
            parse_mode=ParseMode.HTML
        )
    else:
        await query.answer("❌ Απορρίφθηκε!")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(
            user_chat_id,
            "❌ <b>Ο κωδικός απορρίφθηκε.</b>\n\nΒεβαιώσου ότι ο κωδικός είναι σωστός.",
            parse_mode=ParseMode.HTML
        )


# ─── STATS ────────────────────────────────────────────────

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await is_subscribed(chat_id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή. Πάτα /start")
        return
    stats  = await get_stats()
    trades = await get_last_trades(20)

    msg = (
        f"📊 <b>Trading Stats</b>\n\n"
        f"✅ Wins: <b>{stats['wins']}</b>\n"
        f"❌ Losses: <b>{stats['losses']}</b>\n"
        f"🔒 Break-Even: <b>{stats['breakevens']}</b>\n"
        f"📈 Winrate: <b>{stats['winrate']}%</b>\n"
        f"💰 Total PnL: <b>{'+' if stats['total_pnl'] >= 0 else ''}{stats['total_pnl']}€</b>\n"
        f"📅 Σήμερα: <b>{stats['today_trades']} trades</b> / <b>{'+' if stats['today_pnl'] >= 0 else ''}{stats['today_pnl']}€</b>\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"<b>Τελευταία 20 Trades:</b>\n"
    )
    for t in trades:
        if t["result"] == "WIN":
            emoji = "🟢"
        elif t["result"] == "BREAKEVEN":
            emoji = "🔒"
        else:
            emoji = "🔴"
        pnl  = t["pnl_usdt"] or 0
        sign = "+" if pnl >= 0 else ""
        sym  = t["symbol"].replace("USDT", "")
        side = "L" if t["side"] == "Buy" else "S"
        msg += f"{emoji} {sym} {side} | <b>{sign}{pnl:.1f}€</b>\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── OPEN TRADES ──────────────────────────────────────────

async def open_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή.")
        return
    trades = await get_open_trades()
    if not trades:
        await update.message.reply_text("📭 Δεν υπάρχουν ανοιχτές θέσεις αυτή τη στιγμή.")
        return
    msg = "📈 <b>Ανοιχτές Θέσεις:</b>\n\n"
    for t in trades:
        side_emoji = "🟢" if t["side"] == "Buy" else "🔴"
        coin = t["symbol"].replace("USDT", "")
        current = await get_price(t["symbol"])
        if t["side"] == "Buy":
            pnl_est = round((current - t["entry_price"]) / t["entry_price"] * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        else:
            pnl_est = round((t["entry_price"] - current) / t["entry_price"] * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        be_txt = " 🔒 BE" if t["is_breakeven"] else ""
        msg += (
            f"{side_emoji} <b>{coin}</b>{be_txt}\n"
            f"  Entry: ${t['entry_price']:,.4f} | Now: ${current:,.4f}\n"
            f"  SL: ${t['current_sl']:,.4f} | TP: ${t['tp_price']:,.4f}\n"
            f"  Est. PnL: <b>{'+' if pnl_est >= 0 else ''}{pnl_est}€</b>\n\n"
        )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── BALANCE ──────────────────────────────────────────────

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή.")
        return
    balance = await get_wallet_balance()
    await update.message.reply_text(
        f"💰 <b>Bybit Wallet</b>\n\nΔιαθέσιμο: <b>${balance:,.2f} USDT</b>",
        parse_mode=ParseMode.HTML
    )


# ─── SENTIMENT ────────────────────────────────────────────

async def sentiment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("₿ BTC",  callback_data="sent_BTCUSDT"),
         InlineKeyboardButton("Ξ ETH",  callback_data="sent_ETHUSDT")],
        [InlineKeyboardButton("◎ SOL",  callback_data="sent_SOLUSDT"),
         InlineKeyboardButton("✕ XRP",  callback_data="sent_XRPUSDT")],
        [InlineKeyboardButton("🐕 DOGE", callback_data="sent_DOGEUSDT")],
    ])
    await update.message.reply_text(
        "📰 <b>Market Sentiment</b>\n\nΔιάλεξε coin:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def sentiment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    symbol = query.data.replace("sent_", "")
    await query.answer()
    await query.message.reply_text("⏳ Αναλύω τα νέα...")
    sentiment = await get_market_sentiment(symbol)
    current_price = await get_price(symbol)
    side = "LONG" if sentiment["score"] >= 0 else "SHORT"
    price_target = estimate_price_target(symbol, current_price, sentiment["score"], side)
    msg = format_sentiment_message(symbol, sentiment, price_target)
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ─── BREAK-EVEN MONITOR ───────────────────────────────────

async def monitor_breakeven(application: Application):
    """Τρέχει κάθε 30 δευτερόλεπτα και ελέγχει αν πρέπει να μετακινηθεί το SL"""
    while True:
        try:
            open_trades = await get_open_trades()
            for trade in open_trades:
                if trade["is_breakeven"]:
                    # Trailing stop για trades σε break-even
                    if TRAILING_STOP_ACTIVE:
                        current_price = await get_price(trade["symbol"])
                        new_sl = await update_trailing_stop(
                            trade["symbol"], trade["side"],
                            current_price, trade["entry_price"], trade["tp_price"]
                        )
                        if new_sl and new_sl != trade["current_sl"]:
                            ok = await set_stop_loss(trade["symbol"], new_sl)
                            if ok:
                                await update_trade_sl(trade["id"], new_sl, is_breakeven=True)
                    continue

                current_price = await get_price(trade["symbol"])
                entry = trade["entry_price"]
                tp    = trade["tp_price"]

                # Check αν φτάσαμε το break-even trigger (50% του δρόμου προς TP)
                if trade["side"] == "Buy":
                    be_trigger = entry + (tp - entry) * 0.5
                    triggered  = current_price >= be_trigger
                else:
                    be_trigger = entry - (entry - tp) * 0.5
                    triggered  = current_price <= be_trigger

                if triggered:
                    ok = await move_to_breakeven(trade["symbol"], trade["side"], entry, trade["qty"])
                    if ok:
                        await update_trade_sl(trade["id"], entry, is_breakeven=True)
                        msg = format_breakeven_message(trade["symbol"], trade["side"], entry)
                        await broadcast(application, msg)
                        logger.info(f"Break-even activated for trade {trade['id']}")
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        await asyncio.sleep(30)


# ─── SUBSCRIPTION EXPIRY REMINDER ─────────────────────────

async def check_expiring_subs(application: Application):
    """Κάθε 12 ώρες ελέγχει αν κάποιες συνδρομές λήγουν σύντομα"""
    while True:
        try:
            expiring = await get_expiring_subs(days_ahead=3)
            for user in expiring:
                expiry = user["sub_expires_at"]
                days_left = (expiry - datetime.now(timezone.utc)).days
                try:
                    await application.bot.send_message(
                        user["chat_id"],
                        f"⚠️ <b>Η συνδρομή σου λήγει σε {days_left} μέρες!</b>\n\n"
                        f"Ανανέωσέ την με /start → Εγγραφή",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
        except Exception as e:
            logger.error(f"Sub expiry check error: {e}")
        await asyncio.sleep(43200)  # 12 ώρες


# ─── TRADINGVIEW WEBHOOK ──────────────────────────────────

async def handle_tradingview_webhook(symbol: str, side: str, score: int,
                                     application: Application):
    if score < MIN_SIGNAL_SCORE:
        reason = f"Score {score} < {MIN_SIGNAL_SCORE}"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    # Daily limits check
    today_count = await get_today_trades_count()
    if today_count >= MAX_DAILY_TRADES:
        reason = f"Ημερήσιο όριο trades ({MAX_DAILY_TRADES}) επιτεύχθηκε"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        return

    consec_losses = await get_consecutive_losses()
    if consec_losses >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{consec_losses} consecutive losses — bot σε παύση"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        return

    # Sentiment check
    sentiment_data = await get_market_sentiment(symbol)
    current_price  = await get_price(symbol)
    side_bybit     = "Buy" if side.upper() == "LONG" else "Sell"
    price_target   = estimate_price_target(symbol, current_price, sentiment_data["score"], side.upper())

    # Αν sentiment είναι αντίθετο → reject
    if side_bybit == "Buy" and sentiment_data["score"] < -20:
        reason = f"Bearish sentiment ({sentiment_data['score']}) αντίθετο στο LONG signal"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return
    if side_bybit == "Sell" and sentiment_data["score"] > 20:
        reason = f"Bullish sentiment ({sentiment_data['score']}) αντίθετο στο SHORT signal"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    # Manual window (11-14) → ζητάμε approval
    if is_manual_window():
        pending_id = await save_pending_trade(
            symbol, side_bybit, score, "TradingView",
            sentiment_data["label"], price_target
        )
        pending = await get_pending_trade(pending_id)
        msg = format_pending_trade_message(
            {"symbol": symbol, "side": side_bybit,
             "signal_score": score, "price_target": price_target},
            sentiment_data["label"]
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ Ρύθμισε & Εκτέλεσε", callback_data=f"execute_{pending_id}"),
            InlineKeyboardButton("❌ Παράλειψη", callback_data=f"skip_{pending_id}")
        ]])
        await application.bot.send_message(
            ADMIN_CHAT_ID, msg,
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    else:
        # Fully automatic
        result = await place_order(
            symbol, side_bybit, DEFAULT_USDT,
            DEFAULT_LEVERAGE, DEFAULT_SL_PCT, DEFAULT_TP_PCT
        )
        if result["success"]:
            await save_trade(
                symbol, side_bybit,
                result["entry_price"], result["sl_price"], result["tp_price"],
                DEFAULT_LEVERAGE, DEFAULT_USDT, result["qty"],
                result["order_id"], score, "TradingView"
            )
            msg = format_trade_message(result)
            await broadcast(application, msg)
        else:
            await application.bot.send_message(
                ADMIN_CHAT_ID,
                f"❌ Trade failed: {result.get('error')}",
                parse_mode=ParseMode.HTML
            )


# ─── PENDING TRADE CALLBACKS ──────────────────────────────

async def execute_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    pending_id = int(query.data.replace("execute_", ""))
    context.user_data["pending_trade_id"] = pending_id
    context.user_data["awaiting_trade_params"] = True
    await query.answer()
    await query.message.reply_text(
        f"⚙️ Στείλε: <code>leverage,ποσό</code>\n"
        f"Παράδειγμα: <code>{DEFAULT_LEVERAGE},{DEFAULT_USDT}</code>",
        parse_mode=ParseMode.HTML
    )


async def skip_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pending_id = int(query.data.replace("skip_", ""))
    pending = await get_pending_trade(pending_id)
    if pending:
        await save_rejected_signal(
            pending["symbol"], pending["side"],
            pending["signal_score"], "Manual skip από admin"
        )
    await delete_pending_trade(pending_id)
    await query.answer("Παραλείφθηκε!")
    await query.edit_message_reply_markup(None)


async def handle_trade_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        parts    = text.split(",")
        leverage = int(parts[0].strip())
        amount   = float(parts[1].strip())
    except:
        await update.message.reply_text("❌ Format: <code>50,100</code>", parse_mode=ParseMode.HTML)
        return

    pending_id = context.user_data.get("pending_trade_id")
    pending    = await get_pending_trade(pending_id)
    if not pending:
        await update.message.reply_text("❌ Trade δεν βρέθηκε.")
        return

    context.user_data["awaiting_trade_params"] = False
    result = await place_order(
        pending["symbol"], pending["side"],
        amount, leverage, DEFAULT_SL_PCT, DEFAULT_TP_PCT
    )
    if result["success"]:
        await save_trade(
            pending["symbol"], pending["side"],
            result["entry_price"], result["sl_price"], result["tp_price"],
            leverage, amount, result["qty"],
            result["order_id"], pending["signal_score"], "Manual-TV"
        )
        await delete_pending_trade(pending_id)
        msg = format_trade_message(result)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        await broadcast(context.application, msg)
    else:
        await update.message.reply_text(f"❌ Trade failed: {result.get('error')}")


# ─── ADMIN PANEL ──────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats   = await get_stats()
    balance = await get_wallet_balance()
    pending = await get_pending_subscriptions()
    msg = (
        f"👑 <b>Admin Panel</b>\n\n"
        f"💰 Wallet: <b>${balance:,.2f} USDT</b>\n"
        f"📊 Trades: {stats['total']} ({stats['wins']}W / {stats['losses']}L / {stats['breakevens']}BE)\n"
        f"📈 PnL: <b>{'+' if stats['total_pnl'] >= 0 else ''}{stats['total_pnl']}€</b>\n"
        f"📅 Σήμερα: {stats['today_trades']} trades / {'+' if stats['today_pnl'] >= 0 else ''}{stats['today_pnl']}€\n"
        f"⚠️ Consecutive losses: {stats['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}\n"
        f"⏳ Pending subs: {len(pending)}\n"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Pending Subs", callback_data="admin_pending_subs"),
        InlineKeyboardButton("📈 Open Positions", callback_data="admin_positions"),
    ]])
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def admin_pending_subs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    await query.answer()
    subs = await get_pending_subscriptions()
    if not subs:
        await query.message.reply_text("Δεν υπάρχουν pending subs.")
        return
    for sub in subs:
        await query.message.reply_text(
            f"👤 @{sub['username']} (ID: {sub['chat_id']})\nCode: <code>{sub['paysafe_code']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{sub['chat_id']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{sub['chat_id']}"),
            ]])
        )


async def admin_positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    await query.answer()
    from trading import get_open_positions
    positions = await get_open_positions()
    if not positions:
        await query.message.reply_text("Δεν υπάρχουν ανοιχτές θέσεις στο Bybit.")
        return
    msg = "📈 <b>Bybit Positions:</b>\n\n"
    for p in positions:
        msg += (
            f"• {p['symbol']} {p['side']}\n"
            f"  Size: {p['size']} | Entry: ${float(p['avgPrice']):,.4f}\n"
            f"  PnL: ${float(p.get('unrealisedPnl', 0)):,.2f}\n\n"
        )
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── GENERAL MESSAGE HANDLER ──────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = update.message.text.strip() if update.message.text else ""

    if context.user_data.get("awaiting_paysafe"):
        await handle_paysafe(update, context)
        return

    if context.user_data.get("awaiting_trade_params") and chat_id == ADMIN_CHAT_ID:
        await handle_trade_params(update, context)
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
            "ℹ️ <b>Βοήθεια</b>\n\n"
            "/start — Αρχική\n"
            "📊 Stats — Στατιστικά & PnL\n"
            "💰 Balance — Bybit υπόλοιπο\n"
            "📰 Sentiment — Ανάλυση αγοράς\n"
            "📈 Open Trades — Ανοιχτές θέσεις\n\n"
            "Το bot κάνει auto trading με:\n"
            f"• {DEFAULT_LEVERAGE}x leverage / {DEFAULT_USDT}€ margin\n"
            f"• TP: +{DEFAULT_TP_PCT}% (+{round(DEFAULT_USDT*DEFAULT_LEVERAGE*DEFAULT_TP_PCT/100,0):.0f}€)\n"
            f"• SL: -{DEFAULT_SL_PCT}% (-{round(DEFAULT_USDT*DEFAULT_LEVERAGE*DEFAULT_SL_PCT/100,0):.0f}€)\n"
            f"• Break-even στο 50% του TP\n"
            f"• Max {MAX_DAILY_TRADES} trades/μέρα",
            parse_mode=ParseMode.HTML
        )
    elif text == "👑 Admin Panel" and chat_id == ADMIN_CHAT_ID:
        await admin_panel(update, context)


# ─── AIOHTTP WEBHOOK SERVER ───────────────────────────────

from aiohttp import web

async def tradingview_handler(request: web.Request):
    try:
        data   = await request.json()
        symbol = data.get("symbol", "BTCUSDT").upper()
        side   = data.get("side", "LONG").upper()
        score  = int(data.get("score", 0))
        app    = request.app["telegram_app"]
        asyncio.create_task(handle_tradingview_webhook(symbol, side, score, app))
        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=400)


async def health_handler(request: web.Request):
    return web.json_response({"status": "running"})


# ─── MAIN ─────────────────────────────────────────────────

async def main():
    await init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("balance", balance_command))

    application.add_handler(CallbackQueryHandler(subscribe_callback,          pattern="^subscribe$"))
    application.add_handler(CallbackQueryHandler(approve_callback,            pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(sentiment_callback,          pattern="^sent_"))
    application.add_handler(CallbackQueryHandler(execute_pending_callback,    pattern="^execute_"))
    application.add_handler(CallbackQueryHandler(skip_pending_callback,       pattern="^skip_"))
    application.add_handler(CallbackQueryHandler(admin_pending_subs_callback, pattern="^admin_pending_subs$"))
    application.add_handler(CallbackQueryHandler(admin_positions_callback,    pattern="^admin_positions$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Aiohttp για TradingView webhooks
    aio_app = web.Application()
    aio_app["telegram_app"] = application
    aio_app.router.add_post("/webhook", tradingview_handler)
    aio_app.router.add_get("/health",   health_handler)
    aio_app.router.add_get("/",         health_handler)

    await application.initialize()
    await application.start()

    # ─── FIX: Polling ΧΩΡΙΣ conflict με aiohttp ───
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

    runner = web.AppRunner(aio_app)
    await runner.setup()
    port = int(__import__("os").environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"🚀 Bot running on port {port}!")

    # Background tasks
    asyncio.create_task(monitor_breakeven(application))
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
