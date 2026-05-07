import asyncio
from email.mime import application
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
    PAYSAFE_CODE_LENGTH, SUBSCRIPTION_PRICE
)
from database import (
    init_db, get_user, create_user, is_subscribed,
    set_subscription_pending, approve_subscription,
    get_pending_subscriptions, save_trade, close_trade,
    get_last_trades, get_stats, save_pending_trade,
    get_pending_trade, delete_pending_trade
)
from trading import (
    place_order, get_wallet_balance, format_trade_message,
    format_pending_trade_message
)
from sentiment import get_market_sentiment, format_sentiment_message, estimate_price_target

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── HELPERS ──────────────────────────────────────────────

def get_eet_hour() -> int:
    """Επιστρέφει την ώρα σε EET (UTC+3)"""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).hour


def is_manual_window() -> bool:
    h = get_eet_hour()
    return MANUAL_HOUR_START <= h < MANUAL_HOUR_END


def main_keyboard(is_admin: bool = False):
    keys = [
        [KeyboardButton("📊 Stats"), KeyboardButton("💰 Balance")],
        [KeyboardButton("📰 Sentiment"), KeyboardButton("ℹ️ Help")],
    ]
    if is_admin:
        keys.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)


# ─── START ────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or "Unknown"

    await create_user(chat_id, username)
    user = await get_user(chat_id)
    subscribed = user and user["is_subscribed"]

    if subscribed:
        await update.message.reply_text(
            f"👋 Καλώς ήρθες πίσω, @{username}!\n"
            f"✅ Συνδρομή ενεργή — το bot τρέχει!\n\n"
            f"Χρησιμοποίησε τα κουμπιά παρακάτω:",
            reply_markup=main_keyboard(chat_id == ADMIN_CHAT_ID),
            parse_mode=ParseMode.HTML
        )
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💳 Εγγραφή — 25€", callback_data="subscribe")
        ]])
        await update.message.reply_text(
            f"👋 Καλώς ήρθες στο <b>CryptoSniper Bot</b>!\n\n"
            f"🤖 Αυτό το bot:\n"
            f"• Λαμβάνει signals από TradingView\n"
            f"• Κάνει auto trading στο Bybit\n"
            f"• Αναλύει crypto news & sentiment\n"
            f"• Σου στέλνει alerts για κάθε trade\n\n"
            f"💰 <b>Συνδρομή: {SUBSCRIPTION_PRICE}€/μήνα</b>\n\n"
            f"Πάτα το κουμπί για να ξεκινήσεις:",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )


# ─── SUBSCRIPTION ─────────────────────────────────────────

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        f"💳 <b>Διαδικασία Εγγραφής</b>\n\n"
        f"Για να ενεργοποιήσεις τη συνδρομή ({SUBSCRIPTION_PRICE}€):\n\n"
        f"1️⃣ Αγόρασε μια <b>Paysafe κάρτα {SUBSCRIPTION_PRICE}€</b>\n"
        f"2️⃣ Στείλε μου τον <b>{PAYSAFE_CODE_LENGTH}-ψήφιο κωδικό</b>\n"
        f"   (μόνο αριθμοί, χωρίς κενά)\n\n"
        f"✅ Μόλις γίνει επαλήθευση, η συνδρομή ενεργοποιείται αμέσως!\n\n"
        f"Στείλε τον κωδικό τώρα:",
        parse_mode=ParseMode.HTML
    )
    context.user_data["awaiting_paysafe"] = True


async def handle_paysafe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Δέχεται τον Paysafe κωδικό"""
    chat_id = update.effective_chat.id
    code = update.message.text.strip().replace(" ", "").replace("-", "")

    if not code.isdigit() or len(code) != PAYSAFE_CODE_LENGTH:
        await update.message.reply_text(
            f"❌ Ο κωδικός πρέπει να έχει <b>ακριβώς {PAYSAFE_CODE_LENGTH} ψηφία</b>.\n"
            f"Δοκίμασε ξανά:",
            parse_mode=ParseMode.HTML
        )
        return

    context.user_data["awaiting_paysafe"] = False
    await set_subscription_pending(chat_id, code)

    # Ειδοποίηση admin
    user = await get_user(chat_id)
    username = user["username"] if user else "Unknown"
    await context.bot.send_message(
        ADMIN_CHAT_ID,
        f"🔔 <b>Νέο αίτημα συνδρομής!</b>\n\n"
        f"User: @{username} (ID: {chat_id})\n"
        f"Paysafe Code: <code>{code}</code>\n\n"
        f"Πάτα Approve για να ενεργοποιήσεις:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{chat_id}"),
        ]])
    )

    await update.message.reply_text(
        "⏳ <b>Κωδικός ελήφθη!</b>\n\n"
        "Ο κωδικός σου στάλθηκε για επαλήθευση.\n"
        "Θα ειδοποιηθείς μόλις γίνει approve (συνήθως <b>εντός λίγων λεπτών</b>).",
        parse_mode=ParseMode.HTML
    )


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approve/reject subscription"""
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌ Δεν έχεις δικαίωμα!", show_alert=True)
        return

    data = query.data
    action, user_chat_id = data.split("_")[0], int(data.split("_")[1])

    if action == "approve":
        await approve_subscription(user_chat_id)
        await query.answer("✅ Εγκρίθηκε!")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(
            user_chat_id,
            "🎉 <b>Συνδρομή Ενεργοποιήθηκε!</b>\n\n"
            "✅ Καλώς ήρθες στο CryptoSniper Bot!\n"
            "Το bot θα ξεκινήσει αυτόματα να σου στέλνει signals.\n\n"
            "Χρησιμοποίησε /start για να δεις το μενού.",
            parse_mode=ParseMode.HTML
        )
    else:
        await query.answer("❌ Απορρίφθηκε!")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(
            user_chat_id,
            "❌ <b>Ο κωδικός απορρίφθηκε.</b>\n\n"
            "Βεβαιώσου ότι ο κωδικός είναι σωστός και στείλε ξανά /start.",
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

    # Header
    msg = (
        f"📊 <b>Trading Stats</b>\n\n"
        f"✅ Wins: <b>{stats['wins']}</b>\n"
        f"❌ Losses: <b>{stats['losses']}</b>\n"
        f"📈 Winrate: <b>{stats['winrate']}%</b>\n"
        f"💰 Total PnL: <b>{'+' if stats['total_pnl'] >= 0 else ''}{stats['total_pnl']}€</b>\n"
        f"📋 Total Trades: <b>{stats['total']}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Τελευταία 20 Trades:</b>\n"
    )

    # Last 20 trades
    for t in trades:
        emoji  = "🟢" if t["result"] == "WIN" else "🔴"
        pnl    = t["pnl_usdt"] or 0
        sign   = "+" if pnl >= 0 else ""
        symbol = t["symbol"].replace("USDT", "")
        side   = "L" if t["side"] == "Buy" else "S"
        msg += f"{emoji} {symbol} {side} | <b>{sign}{pnl:.1f}€</b>\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── BALANCE ──────────────────────────────────────────────

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή. Πάτα /start")
        return

    balance = await get_wallet_balance()
    await update.message.reply_text(
        f"💰 <b>Bybit Wallet Balance</b>\n\n"
        f"Διαθέσιμο USDT: <b>${balance:,.2f}</b>",
        parse_mode=ParseMode.HTML
    )


# ─── SENTIMENT ────────────────────────────────────────────

async def sentiment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή. Πάτα /start")
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
    msg = format_sentiment_message(symbol, sentiment)
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ─── WEBHOOK (TradingView Alerts) ─────────────────────────

async def handle_tradingview_webhook(symbol: str, side: str, score: int,
                                     application: Application):
    """
    Καλείται όταν έρθει alert από TradingView.
    Αν score >= MIN_SIGNAL_SCORE → εκτελεί trade ή ζητά manual approval.
    """
    if score < MIN_SIGNAL_SCORE:
        logger.info(f"Signal ignored: {symbol} {side} score={score} < {MIN_SIGNAL_SCORE}")
        return

    # Παίρνουμε sentiment
    sentiment_data = await get_market_sentiment(symbol)
    sentiment_label = sentiment_data["label"]
    current_price_est = 0  # θα πάρουμε από Bybit
    from trading import get_price
    current_price_est = await get_price(symbol)

    side_bybit = "Buy" if side.upper() == "LONG" else "Sell"
    price_target = estimate_price_target(
        symbol, current_price_est, sentiment_data["score"], side.upper()
    )

    # Αν είμαστε στο manual window (11:00-14:00) → ζητάμε approval
    if is_manual_window():
        pending_id = await save_pending_trade(
            symbol, side_bybit, score, "TradingView",
            sentiment_label, price_target
        )
        pending = await get_pending_trade(pending_id)
        msg = format_pending_trade_message(
            {"symbol": symbol, "side": side_bybit, "signal_score": score,
             "price_target": price_target},
            sentiment_label
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "⚙️ Ρύθμισε & Εκτέλεσε",
                callback_data=f"execute_{pending_id}"
            ),
            InlineKeyboardButton("❌ Παράλειψη", callback_data=f"skip_{pending_id}")
        ]])
        await application.bot.send_message(
            ADMIN_CHAT_ID, msg,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        # Fully automatic
        result = await place_order(
            symbol, side_bybit, DEFAULT_USDT,
            DEFAULT_LEVERAGE, DEFAULT_SL_PCT, DEFAULT_TP_PCT
        )
        if result["success"]:
            trade_id = await save_trade(
                symbol, side_bybit, result["entry_price"],
                result["sl_price"], result["tp_price"],
                DEFAULT_LEVERAGE, DEFAULT_USDT, score, "TradingView"
            )
            msg = format_trade_message(result)
            # Στείλε σε όλους τους subscribers
            await broadcast_to_subscribers(application, msg)
        else:
            await application.bot.send_message(
                ADMIN_CHAT_ID,
                f"❌ Trade failed: {result.get('error')}",
                parse_mode=ParseMode.HTML
            )


async def execute_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Όταν ο admin πατήσει 'Ρύθμισε & Εκτέλεσε'"""
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌ Δεν έχεις δικαίωμα!", show_alert=True)
        return

    pending_id = int(query.data.replace("execute_", ""))
    context.user_data["pending_trade_id"] = pending_id
    context.user_data["awaiting_trade_params"] = True
    await query.answer()
    await query.message.reply_text(
        "⚙️ <b>Ρύθμισε το Trade</b>\n\n"
        "Στείλε μου: <code>leverage,ποσό</code>\n"
        "Παράδειγμα: <code>25,100</code>\n\n"
        f"Default: <code>{DEFAULT_LEVERAGE},{DEFAULT_USDT}</code>",
        parse_mode=ParseMode.HTML
    )


async def skip_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    pending_id = int(query.data.replace("skip_", ""))
    await delete_pending_trade(pending_id)
    await query.answer("Trade παραλείφθηκε!")
    await query.edit_message_reply_markup(None)


async def handle_trade_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Δέχεται leverage,amount από admin"""
    text = update.message.text.strip()
    try:
        parts    = text.split(",")
        leverage = int(parts[0].strip())
        amount   = float(parts[1].strip())
    except:
        await update.message.reply_text("❌ Λάθος format. Στείλε: <code>25,100</code>", parse_mode=ParseMode.HTML)
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
            leverage, amount, pending["signal_score"], "Manual-TradingView"
        )
        await delete_pending_trade(pending_id)
        msg = format_trade_message(result)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        await broadcast_to_subscribers(context.application, msg)
    else:
        await update.message.reply_text(f"❌ Trade failed: {result.get('error')}")


# ─── BROADCAST ────────────────────────────────────────────

async def broadcast_to_subscribers(application: Application, message: str):
    """Στέλνει μήνυμα σε όλους τους subscribers"""
    import asyncpg
    from config import DATABASE_URL
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT chat_id FROM users WHERE is_subscribed=TRUE")
        for row in rows:
            try:
                await application.bot.send_message(
                    row["chat_id"], message,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Failed to send to {row['chat_id']}: {e}")
    finally:
        await conn.close()


# ─── GENERAL MESSAGE HANDLER ──────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    # Paysafe code input
    if context.user_data.get("awaiting_paysafe"):
        await handle_paysafe(update, context)
        return

    # Trade params input (admin only)
    if context.user_data.get("awaiting_trade_params") and chat_id == ADMIN_CHAT_ID:
        await handle_trade_params(update, context)
        return

    # Keyboard buttons
    if text == "📊 Stats":
        await stats_command(update, context)
    elif text == "💰 Balance":
        await balance_command(update, context)
    elif text == "📰 Sentiment":
        await sentiment_command(update, context)
    elif text == "ℹ️ Help":
        await update.message.reply_text(
            "ℹ️ <b>Βοήθεια</b>\n\n"
            "/start — Αρχική σελίδα\n"
            "📊 Stats — Στατιστικά trades\n"
            "💰 Balance — Υπόλοιπο Bybit\n"
            "📰 Sentiment — Ανάλυση αγοράς\n\n"
            "Το bot λαμβάνει signals από TradingView και κάνει auto trading στο Bybit.",
            parse_mode=ParseMode.HTML
        )
    elif text == "👑 Admin Panel" and chat_id == ADMIN_CHAT_ID:
        await admin_panel(update, context)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_subs = await get_pending_subscriptions()
    stats        = await get_stats()
    balance      = await get_wallet_balance()

    msg = (
        f"👑 <b>Admin Panel</b>\n\n"
        f"💰 Wallet: ${balance:,.2f} USDT\n"
        f"📊 Trades: {stats['total']} ({stats['wins']}W/{stats['losses']}L)\n"
        f"💵 PnL: {'+' if stats['total_pnl'] >= 0 else ''}{stats['total_pnl']}€\n"
        f"⏳ Pending subs: {len(pending_subs)}\n"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Pending Subs", callback_data="admin_pending_subs"),
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
        await query.message.reply_text("Δεν υπάρχουν pending subscriptions.")
        return
    for sub in subs:
        await query.message.reply_text(
            f"User: @{sub['username']} (ID: {sub['chat_id']})\n"
            f"Code: <code>{sub['paysafe_code']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{sub['chat_id']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{sub['chat_id']}"),
            ]])
        )


# ─── WEBHOOK SERVER (για TradingView) ─────────────────────

from aiohttp import web

async def tradingview_handler(request: web.Request):
    """
    Endpoint: POST /webhook
    TradingView στέλνει JSON:
    {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "score": 85
    }
    """
    try:
        data  = await request.json()
        symbol = data.get("symbol", "BTCUSDT").upper()
        side   = data.get("side", "LONG").upper()
        score  = int(data.get("score", 0))
        app    = request.app["telegram_app"]
        asyncio.create_task(
            handle_tradingview_webhook(symbol, side, score, app)
        )
        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.json_response({"status": "error", "msg": str(e)}, status=400)


# ─── MAIN ─────────────────────────────────────────────────

async def main():
    # Init database
    await init_db()

    # Build Telegram app
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("sentiment", sentiment_command))

    application.add_handler(CallbackQueryHandler(subscribe_callback,         pattern="^subscribe$"))
    application.add_handler(CallbackQueryHandler(approve_callback,           pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(sentiment_callback,         pattern="^sent_"))
    application.add_handler(CallbackQueryHandler(execute_pending_callback,   pattern="^execute_"))
    application.add_handler(CallbackQueryHandler(skip_pending_callback,      pattern="^skip_"))
    application.add_handler(CallbackQueryHandler(admin_pending_subs_callback,pattern="^admin_pending_subs$"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Aiohttp server για TradingView webhooks
    aio_app = web.Application()
    aio_app["telegram_app"] = application
    aio_app.router.add_post("/webhook", tradingview_handler)

    # Start everything
 await application.initialize()
 await application.start()

 runner = web.AppRunner(aio_app)
 await runner.setup()

 site = web.TCPSite(runner, "0.0.0.0", 8080)
 await site.start()

 logger.info("🚀 Bot started! Listening on port 8080...")
 try:
    await asyncio.Event().wait()
 finally:
    await application.stop()
    await application.shutdown()
    await runner.cleanup()
 
 if __name__ == "__main__":
    asyncio.run(main())
