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
    MIN_SIGNAL_SCORE, PAYSAFE_CODE_LENGTH, SUBSCRIPTION_PRICE,
    SUBSCRIPTION_DAYS, MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES,
    TRAILING_STOP_ACTIVE
)
from database import (
    init_db, get_user, create_user, is_subscribed, get_sub_expiry,
    set_subscription_pending, approve_subscription,
    get_pending_subscriptions, save_trade, close_trade, update_trade_sl,
    get_last_trades, get_stats, save_rejected_signal,
    get_today_trades_count, get_consecutive_losses,
    get_expiring_subs, get_open_trades
)
from trading import (
    place_order, get_wallet_balance, get_price, set_stop_loss,
    move_to_breakeven, update_trailing_stop,
    format_trade_message, format_rejected_message,
    format_breakeven_message
)
from sentiment import get_market_sentiment, format_sentiment_message, estimate_price_target

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ─── HELPERS ──────────────────────────────────────────────

def main_keyboard(is_admin: bool = False):
    keys = [
        [KeyboardButton("📊 Stats"), KeyboardButton("💰 Balance")],
        [KeyboardButton("📰 Sentiment"), KeyboardButton("📈 Open Trades")],
        [KeyboardButton("ℹ️ Help")],
    ]
    if is_admin:
        keys.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)


async def broadcast(application: Application, message: str):
    """Στέλνει μήνυμα σε όλους τους subscribers"""
    import asyncpg
    from config import DATABASE_URL
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            "SELECT chat_id FROM users WHERE is_subscribed=TRUE")
        for row in rows:
            try:
                await application.bot.send_message(
                    row["chat_id"], message, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"Broadcast failed for {row['chat_id']}: {e}")
    finally:
        await conn.close()


# ─── /start ───────────────────────────────────────────────

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
            f"✅ Συνδρομή ενεργή έως: <b>{expiry_str}</b>",
            reply_markup=main_keyboard(chat_id == ADMIN_CHAT_ID),
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"👋 Καλώς ήρθες στο <b>CryptoSniper Bot</b>!\n\n"
            f"🤖 Auto trading στο Bybit\n"
            f"• {DEFAULT_USDT} USDT margin / {DEFAULT_LEVERAGE}x leverage\n"
            f"• TP: +{DEFAULT_TP_PCT}% → +{round(DEFAULT_USDT*DEFAULT_LEVERAGE*DEFAULT_TP_PCT/100,1)} USDT\n"
            f"• SL: -{DEFAULT_SL_PCT}% → -{round(DEFAULT_USDT*DEFAULT_LEVERAGE*DEFAULT_SL_PCT/100,1)} USDT\n"
            f"• Break-even + Trailing stop αυτόματα\n"
            f"• News sentiment ανάλυση\n\n"
            f"💰 <b>Συνδρομή: {SUBSCRIPTION_PRICE}€/μήνα</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"💳 Εγγραφή — {SUBSCRIPTION_PRICE}€",
                    callback_data="subscribe")
            ]])
        )


# ─── SUBSCRIPTION ─────────────────────────────────────────

async def subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        f"💳 <b>Εγγραφή — {SUBSCRIPTION_PRICE}€/μήνα</b>\n\n"
        f"1️⃣ Αγόρασε <b>Paysafe {SUBSCRIPTION_PRICE}€</b> από περίπτερο\n"
        f"2️⃣ Στείλε μου τον <b>{PAYSAFE_CODE_LENGTH}-ψήφιο κωδικό</b>\n\n"
        f"Στείλε τον κωδικό τώρα 👇",
        parse_mode=ParseMode.HTML
    )
    context.user_data["awaiting_paysafe"] = True


async def handle_paysafe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    code = update.message.text.strip().replace(" ", "").replace("-", "")

    if not code.isdigit() or len(code) != PAYSAFE_CODE_LENGTH:
        await update.message.reply_text(
            f"❌ Ο κωδικός πρέπει να έχει ακριβώς "
            f"<b>{PAYSAFE_CODE_LENGTH} ψηφία</b>. Δοκίμασε ξανά:",
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
        f"User: @{username} (ID: <code>{chat_id}</code>)\n"
        f"Paysafe: <code>{code}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{chat_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{chat_id}"),
        ]])
    )
    await update.message.reply_text(
        "⏳ Κωδικός ελήφθη! Θα ειδοποιηθείς μόλις γίνει επαλήθευση."
    )


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌ Δεν έχεις δικαίωμα!", show_alert=True)
        return

    parts      = query.data.split("_")
    action     = parts[0]
    user_chat_id = int(parts[1])

    if action == "approve":
        await approve_subscription(user_chat_id)
        await query.answer("✅ Εγκρίθηκε!")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(
            user_chat_id,
            f"🎉 <b>Συνδρομή Ενεργοποιήθηκε!</b>\n\n"
            f"✅ {SUBSCRIPTION_DAYS} μέρες ενεργή συνδρομή!\n"
            f"Πάτα /start για το μενού.",
            parse_mode=ParseMode.HTML
        )
    else:
        await query.answer("❌ Απορρίφθηκε!")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(
            user_chat_id,
            "❌ <b>Ο κωδικός απορρίφθηκε.</b>\n\n"
            "Βεβαιώσου ότι ο κωδικός είναι σωστός και δοκίμασε ξανά.",
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
        f"💰 Total PnL: <b>"
        f"{'+' if stats['total_pnl'] >= 0 else ''}"
        f"{stats['total_pnl']} USDT</b>\n"
        f"📅 Σήμερα: <b>{stats['today_trades']} trades</b> | "
        f"<b>{'+' if stats['today_pnl'] >= 0 else ''}"
        f"{stats['today_pnl']} USDT</b>\n\n"
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
        msg += f"{emoji} {sym} {side} | <b>{sign}{pnl:.1f} USDT</b>\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── OPEN TRADES ──────────────────────────────────────────

async def open_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή.")
        return

    trades = await get_open_trades()
    if not trades:
        await update.message.reply_text(
            "📭 Δεν υπάρχουν ανοιχτές θέσεις αυτή τη στιγμή.")
        return

    msg = "📈 <b>Ανοιχτές Θέσεις:</b>\n\n"
    for t in trades:
        emoji   = "🟢" if t["side"] == "Buy" else "🔴"
        coin    = t["symbol"].replace("USDT", "")
        current = await get_price(t["symbol"])
        if t["side"] == "Buy":
            pnl = round(
                (current - t["entry_price"]) / t["entry_price"]
                * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        else:
            pnl = round(
                (t["entry_price"] - current) / t["entry_price"]
                * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        be_txt = " 🔒 BE" if t["is_breakeven"] else ""
        msg += (
            f"{emoji} <b>{coin}</b>{be_txt}\n"
            f"  Entry: ${t['entry_price']:,.4f} → Now: ${current:,.4f}\n"
            f"  SL: ${t['current_sl']:,.4f} | TP: ${t['tp_price']:,.4f}\n"
            f"  PnL: <b>{'+' if pnl>=0 else ''}{pnl} USDT</b>\n\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── BALANCE ──────────────────────────────────────────────

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή.")
        return
    balance = await get_wallet_balance()
    await update.message.reply_text(
        f"💰 <b>Bybit Wallet</b>\n\n"
        f"Διαθέσιμο: <b>{balance:,.2f} USDT</b>",
        parse_mode=ParseMode.HTML
    )


# ─── SENTIMENT ────────────────────────────────────────────

async def sentiment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_subscribed(update.effective_chat.id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή.")
        return
    await update.message.reply_text(
        "📰 <b>Market Sentiment</b>\n\nΔιάλεξε coin:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("₿ BTC",  callback_data="sent_BTCUSDT"),
             InlineKeyboardButton("Ξ ETH",  callback_data="sent_ETHUSDT")],
            [InlineKeyboardButton("◎ SOL",  callback_data="sent_SOLUSDT"),
             InlineKeyboardButton("✕ XRP",  callback_data="sent_XRPUSDT")],
            [InlineKeyboardButton("🐕 DOGE", callback_data="sent_DOGEUSDT")],
        ])
    )


async def sentiment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    symbol = query.data.replace("sent_", "")
    await query.answer()
    await query.message.reply_text("⏳ Αναλύω τα νέα...")

    sentiment     = await get_market_sentiment(symbol)
    current_price = await get_price(symbol)
    side          = "LONG" if sentiment["score"] >= 0 else "SHORT"
    price_target  = estimate_price_target(
        symbol, current_price, sentiment["score"], side)

    msg = format_sentiment_message(symbol, sentiment, price_target)
    await query.message.reply_text(
        msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ─── BREAK-EVEN MONITOR ───────────────────────────────────

async def monitor_breakeven(application: Application):
    """Κάθε 30 δευτερόλεπτα ελέγχει αν χρειάζεται break-even ή trailing"""
    while True:
        try:
            open_trades = await get_open_trades()
            for trade in open_trades:
                current_price = await get_price(trade["symbol"])
                if current_price == 0:
                    continue

                entry = trade["entry_price"]
                tp    = trade["tp_price"]

                if trade["is_breakeven"]:
                    # Trailing stop
                    if TRAILING_STOP_ACTIVE:
                        new_sl = await update_trailing_stop(
                            trade["symbol"], trade["side"],
                            current_price, entry, tp
                        )
                        if new_sl and new_sl != trade["current_sl"]:
                            ok = await set_stop_loss(trade["symbol"], new_sl)
                            if ok:
                                await update_trade_sl(
                                    trade["id"], new_sl, is_breakeven=True)
                    continue

                # Check break-even trigger (50% του δρόμου προς TP)
                if trade["side"] == "Buy":
                    be_trigger = entry + (tp - entry) * 0.5
                    triggered  = current_price >= be_trigger
                else:
                    be_trigger = entry - (entry - tp) * 0.5
                    triggered  = current_price <= be_trigger

                if triggered:
                    ok = await move_to_breakeven(
                        trade["symbol"], trade["side"],
                        entry, trade["qty"])
                    if ok:
                        await update_trade_sl(trade["id"], entry, is_breakeven=True)
                        msg = format_breakeven_message(
                            trade["symbol"], trade["side"], entry)
                        await broadcast(application, msg)
                        logger.info(f"Break-even: trade {trade['id']}")

        except Exception as e:
            logger.error(f"Monitor error: {e}")

        await asyncio.sleep(30)


# ─── SUBSCRIPTION EXPIRY REMINDER ─────────────────────────

async def check_expiring_subs(application: Application):
    """Κάθε 12 ώρες ελέγχει συνδρομές που λήγουν σε 3 μέρες"""
    while True:
        try:
            expiring = await get_expiring_subs(days_ahead=3)
            for user in expiring:
                expiry    = user["sub_expires_at"]
                days_left = (expiry - datetime.now(timezone.utc)).days
                try:
                    await application.bot.send_message(
                        user["chat_id"],
                        f"⚠️ <b>Η συνδρομή σου λήγει σε {days_left} μέρες!</b>\n\n"
                        f"Ανανέωσέ την με /start",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Sub expiry error: {e}")
        await asyncio.sleep(43200)


# ─── TRADINGVIEW WEBHOOK ──────────────────────────────────

async def handle_tradingview_webhook(symbol: str, side: str, score: int,
                                     application: Application):
    """Πλήρως αυτόματο — χωρίς manual approval"""

    # Έλεγχος score
    if score < MIN_SIGNAL_SCORE:
        reason = f"Score {score}/100 κάτω από το κατώφλι {MIN_SIGNAL_SCORE}"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    # Ημερήσιο όριο
    today_count = await get_today_trades_count()
    if today_count >= MAX_DAILY_TRADES:
        reason = f"Ημερήσιο όριο {MAX_DAILY_TRADES} trades επιτεύχθηκε"
        await save_rejected_signal(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            format_rejected_message(symbol, side, score, reason),
            parse_mode=ParseMode.HTML
        )
        return

    # Consecutive losses
    consec = await get_consecutive_losses()
    if consec >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{consec} consecutive losses — bot σε παύση"
        await save_rejected_signal(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            format_rejected_message(symbol, side, score, reason),
            parse_mode=ParseMode.HTML
        )
        return

    # Sentiment check
    sentiment_data = await get_market_sentiment(symbol)
    side_bybit     = "Buy" if side.upper() == "LONG" else "Sell"

    if side_bybit == "Buy" and sentiment_data["score"] < -20:
        reason = f"Bearish sentiment ({sentiment_data['score']}) — αντίθετο στο LONG"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    if side_bybit == "Sell" and sentiment_data["score"] > 20:
        reason = f"Bullish sentiment ({sentiment_data['score']}) — αντίθετο στο SHORT"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    # ✅ Εκτέλεση trade — πλήρως αυτόματο
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
        logger.info(f"Trade opened: {symbol} {side_bybit} score={score}")
    else:

        error_msg = result.get("error", "Unknown error")

        logger.error(f"TRADE FAILED FULL RESPONSE: {result}")

        await application.bot.send_message(
            ADMIN_CHAT_ID,
            f"❌ <b>Trade failed</b>\n\n"
            f"Symbol: <b>{symbol}</b>\n"
            f"Side: <b>{side_bybit}</b>\n"
            f"Error: <code>{error_msg}</code>\n\n"
            f"Full Response:\n<code>{str(result)}</code>",
            parse_mode=ParseMode.HTML
        )

# ─── ADMIN PANEL ──────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats   = await get_stats()
    balance = await get_wallet_balance()
    pending = await get_pending_subscriptions()

    msg = (
        f"👑 <b>Admin Panel</b>\n\n"
        f"💰 Wallet: <b>{balance:,.2f} USDT</b>\n"
        f"📊 Trades: {stats['total']} "
        f"({stats['wins']}W / {stats['losses']}L / {stats['breakevens']}BE)\n"
        f"📈 PnL: <b>"
        f"{'+' if stats['total_pnl']>=0 else ''}"
        f"{stats['total_pnl']} USDT</b>\n"
        f"📅 Σήμερα: {stats['today_trades']} trades\n"
        f"⚠️ Consecutive losses: "
        f"{stats['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}\n"
        f"⏳ Pending subs: {len(pending)}\n"
    )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📋 Pending Subs", callback_data="admin_pending_subs"),
            InlineKeyboardButton(
                "📈 Positions",    callback_data="admin_positions"),
        ]])
    )


async def admin_pending_subs_callback(update: Update,
                                       context: ContextTypes.DEFAULT_TYPE):
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
            f"👤 @{sub['username']} (ID: {sub['chat_id']})\n"
            f"Code: <code>{sub['paysafe_code']}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Approve", callback_data=f"approve_{sub['chat_id']}"),
                InlineKeyboardButton(
                    "❌ Reject",  callback_data=f"reject_{sub['chat_id']}"),
            ]])
        )


async def admin_positions_callback(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    await query.answer()
    from trading import get_open_positions
    positions = await get_open_positions()
    if not positions:
        await query.message.reply_text(
            "Δεν υπάρχουν ανοιχτές θέσεις στο Bybit.")
        return
    msg = "📈 <b>Bybit Positions:</b>\n\n"
    for p in positions:
        pnl = float(p.get("unrealisedPnl", 0))
        msg += (
            f"• {p['symbol']} {p['side']}\n"
            f"  Size: {p['size']} | "
            f"Entry: ${float(p['avgPrice']):,.4f}\n"
            f"  PnL: <b>{'+' if pnl>=0 else ''}{pnl:.2f} USDT</b>\n\n"
        )
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ─── GENERAL MESSAGE HANDLER ──────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        tp_profit = round(DEFAULT_USDT * DEFAULT_LEVERAGE * DEFAULT_TP_PCT / 100, 1)
        sl_loss   = round(DEFAULT_USDT * DEFAULT_LEVERAGE * DEFAULT_SL_PCT / 100, 1)
        await update.message.reply_text(
            f"ℹ️ <b>Βοήθεια</b>\n\n"
            f"/start — Αρχική σελίδα\n"
            f"📊 Stats — Στατιστικά & PnL\n"
            f"💰 Balance — Bybit υπόλοιπο\n"
            f"📰 Sentiment — Ανάλυση αγοράς\n"
            f"📈 Open Trades — Ανοιχτές θέσεις\n\n"
            f"<b>Trading settings:</b>\n"
            f"• Margin: {DEFAULT_USDT} USDT\n"
            f"• Leverage: {DEFAULT_LEVERAGE}x\n"
            f"• TP: +{DEFAULT_TP_PCT}% → +{tp_profit} USDT\n"
            f"• SL: -{DEFAULT_SL_PCT}% → -{sl_loss} USDT\n"
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
        asyncio.create_task(
            handle_tradingview_webhook(symbol, side, score, app))
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

    # Handlers
    application.add_handler(CommandHandler("start",   start))
    application.add_handler(CommandHandler("stats",   stats_command))
    application.add_handler(CommandHandler("balance", balance_command))

    application.add_handler(CallbackQueryHandler(
        subscribe_callback,          pattern="^subscribe$"))
    application.add_handler(CallbackQueryHandler(
        approve_callback,            pattern="^(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(
        sentiment_callback,          pattern="^sent_"))
    application.add_handler(CallbackQueryHandler(
        admin_pending_subs_callback, pattern="^admin_pending_subs$"))
    application.add_handler(CallbackQueryHandler(
        admin_positions_callback,    pattern="^admin_positions$"))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message))

    # Aiohttp για TradingView webhooks
    aio_app = web.Application()
    aio_app["telegram_app"] = application
    aio_app.router.add_post("/webhook", tradingview_handler)
    aio_app.router.add_get("/health",   health_handler)
    aio_app.router.add_get("/",         health_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

    port = int(__import__("os").environ.get("PORT", 8080))
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"🚀 Bot running on port {port}")

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
