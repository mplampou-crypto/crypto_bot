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
    SUBSCRIPTION_DAYS, MAX_DAILY_TRADES, MAX_CONSECUTIVE_LOSSES
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
    place_order, get_wallet_balance, get_price,
    get_closed_pnl, is_position_open, get_open_positions,
    format_trade_message, format_rejected_message,
    format_closed_trade_message
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
        [KeyboardButton("📊 Stats"),     KeyboardButton("💰 Balance")],
        [KeyboardButton("📰 Sentiment"), KeyboardButton("📈 Open Trades")],
        [KeyboardButton("ℹ️ Help")],
    ]
    if is_admin:
        keys.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)


async def broadcast(application: Application, message: str):
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
                logger.warning(f"Broadcast failed {row['chat_id']}: {e}")
    finally:
        await conn.close()


async def get_open_symbols() -> list:
    trades = await get_open_trades()
    return [t["symbol"] for t in trades]


# ─── SYNC CLOSED TRADES ───────────────────────────────────

async def sync_closed_trades(application: Application, notify: bool = True) -> int:
    """
    Συγχρονίζει τα κλειστά trades από το Bybit με τη DB.
    Επιστρέφει πόσα trades έκλεισε.
    Καλείται από τον monitor ΚΑΙ από τη /sync εντολή.
    """
    closed_count = 0
    try:
        open_trades = await get_open_trades()
        if not open_trades:
            return 0

        # Παίρνουμε closed PnL από Bybit (τελευταία 50)
        closed_bybit = await get_closed_pnl(limit=50)

        # Map: symbol → list of closed pnl entries
        symbol_pnl_map = {}
        for c in closed_bybit:
            sym = c.get("symbol", "")
            if sym:
                if sym not in symbol_pnl_map:
                    symbol_pnl_map[sym] = []
                symbol_pnl_map[sym].append(float(c.get("closedPnl", 0)))

        for trade in open_trades:
            symbol = trade["symbol"]

            # Ελέγχουμε αν η θέση είναι ακόμα ανοιχτή στο Bybit
            still_open = await is_position_open(symbol)

            if not still_open:
                # Παίρνουμε PnL — αν υπάρχουν πολλά, παίρνουμε το πιο πρόσφατο
                pnl_list = symbol_pnl_map.get(symbol, [0.0])
                pnl_usdt = pnl_list[0] if pnl_list else 0.0
                result   = "WIN" if pnl_usdt >= 0 else "LOSS"

                await close_trade(trade["id"], result, round(pnl_usdt, 2))
                closed_count += 1

                if notify:
                    msg = format_closed_trade_message(
                        symbol, trade["side"], pnl_usdt, result)
                    await broadcast(application, msg)

                logger.info(
                    f"Trade synced & closed: {symbol} {result} "
                    f"PnL={pnl_usdt:.2f} USDT")

    except Exception as e:
        logger.error(f"Sync error: {e}")

    return closed_count


# ─── /start ───────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or "User"
    await create_user(chat_id, username)
    subscribed = await is_subscribed(chat_id)

    if subscribed:
        expiry     = await get_sub_expiry(chat_id)
        expiry_str = expiry.strftime("%d/%m/%Y") if expiry else "—"
        await update.message.reply_text(
            f"👋 Καλώς ήρθες, <b>@{username}</b>!\n\n"
            f"✅ Συνδρομή ενεργή έως: <b>{expiry_str}</b>",
            reply_markup=main_keyboard(chat_id == ADMIN_CHAT_ID),
            parse_mode=ParseMode.HTML
        )
    else:
        tp_profit = round(DEFAULT_USDT * DEFAULT_LEVERAGE * DEFAULT_TP_PCT / 100, 1)
        sl_loss   = round(DEFAULT_USDT * DEFAULT_LEVERAGE * DEFAULT_SL_PCT / 100, 1)
        await update.message.reply_text(
            f"👋 Καλώς ήρθες στο <b>CryptoSniper Bot</b>!\n\n"
            f"🤖 Auto trading στο Bybit\n"
            f"• {DEFAULT_USDT} USDT margin / {DEFAULT_LEVERAGE}x leverage\n"
            f"• Dynamic TP από S/R levels\n"
            f"• SL: -{DEFAULT_SL_PCT}%\n\n"
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
    code    = update.message.text.strip().replace(" ", "").replace("-", "")

    if not code.isdigit() or len(code) != PAYSAFE_CODE_LENGTH:
        await update.message.reply_text(
            f"❌ Ο κωδικός πρέπει να έχει ακριβώς "
            f"<b>{PAYSAFE_CODE_LENGTH} ψηφία</b>. Δοκίμασε ξανά:",
            parse_mode=ParseMode.HTML
        )
        return

    context.user_data["awaiting_paysafe"] = False
    await set_subscription_pending(chat_id, code)

    user     = await get_user(chat_id)
    username = user["username"] if user else "Unknown"

    await context.bot.send_message(
        ADMIN_CHAT_ID,
        f"🔔 <b>Νέο αίτημα συνδρομής!</b>\n\n"
        f"👤 User: @{username}\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"💳 Paysafe: <code>{code}</code>\n\n"
        f"Επαλήθευσε στο paysafecard.com και μετά:\n"
        f"/approve {chat_id}\n"
        f"/reject {chat_id}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"APPROVE:{chat_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"REJECT:{chat_id}"),
        ]])
    )
    await update.message.reply_text(
        "⏳ <b>Κωδικός ελήφθη!</b>\n\nΘα ειδοποιηθείς μόλις γίνει επαλήθευση.",
        parse_mode=ParseMode.HTML
    )


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌ Δεν έχεις δικαίωμα!", show_alert=True)
        return

    parts        = query.data.split(":", 1)
    action       = parts[0]
    user_chat_id = int(parts[1])
    await query.answer()

    if action == "APPROVE":
        await approve_subscription(user_chat_id)
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(
            f"✅ Εγκρίθηκε! ID: <code>{user_chat_id}</code>",
            parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(
                user_chat_id,
                f"🎉 <b>Συνδρομή Ενεργοποιήθηκε!</b>\n\n"
                f"✅ {SUBSCRIPTION_DAYS} μέρες ενεργή!\n"
                f"Πάτα /start για να ξεκινήσεις.",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Could not notify {user_chat_id}: {e}")
    else:
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(
            f"❌ Απορρίφθηκε! ID: <code>{user_chat_id}</code>",
            parse_mode=ParseMode.HTML)
        try:
            await context.bot.send_message(
                user_chat_id,
                "❌ <b>Ο κωδικός απορρίφθηκε.</b>\n\n"
                "Βεβαιώσου ότι ο κωδικός είναι σωστός και δοκίμασε ξανά με /start.",
                parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Could not notify {user_chat_id}: {e}")


# ─── APPROVE / REJECT COMMANDS ────────────────────────────

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Χρήση: /approve 123456789"""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("Χρήση: /approve <chat_id>")
        return
    try:
        user_id = int(context.args[0])
        await approve_subscription(user_id)
        await update.message.reply_text(
            f"✅ Εγκρίθηκε ο χρήστης <code>{user_id}</code>",
            parse_mode=ParseMode.HTML)
        await context.bot.send_message(
            user_id,
            f"🎉 <b>Συνδρομή Ενεργοποιήθηκε!</b>\n\n"
            f"✅ {SUBSCRIPTION_DAYS} μέρες ενεργή!\n"
            f"Πάτα /start για να ξεκινήσεις.",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Χρήση: /reject 123456789"""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not context.args:
        await update.message.reply_text("Χρήση: /reject <chat_id>")
        return
    try:
        user_id = int(context.args[0])
        await update.message.reply_text(
            f"❌ Απορρίφθηκε ο χρήστης <code>{user_id}</code>",
            parse_mode=ParseMode.HTML)
        await context.bot.send_message(
            user_id,
            "❌ <b>Ο κωδικός απορρίφθηκε.</b>\n\n"
            "Βεβαιώσου ότι ο κωδικός είναι σωστός και δοκίμασε ξανά.",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ─── /sync COMMAND ────────────────────────────────────────

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sync — Κλείνει χειροκίνητα trades που έκλεισαν στο Bybit
    αλλά έμειναν ανοιχτά στη DB. Μόνο για admin.
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    await update.message.reply_text("⏳ Συγχρονίζω trades με Bybit...")

    closed = await sync_closed_trades(context.application, notify=True)

    if closed == 0:
        await update.message.reply_text(
            "✅ Όλα τα trades είναι ήδη συγχρονισμένα!\n"
            "Δεν βρέθηκαν κλειστά trades στο Bybit.")
    else:
        await update.message.reply_text(
            f"✅ Συγχρονίστηκαν <b>{closed} trades</b> από το Bybit!",
            parse_mode=ParseMode.HTML)


# ─── STATS ────────────────────────────────────────────────

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await is_subscribed(chat_id):
        await update.message.reply_text("❌ Χρειάζεσαι συνδρομή. Πάτα /start")
        return

    stats  = await get_stats()
    trades = await get_last_trades(20)

    # Header με γενικά stats
    msg = (
        f"📊 <b>Trading Stats</b>\n\n"
        f"✅ Wins: <b>{stats['wins']}</b>  "
        f"❌ Losses: <b>{stats['losses']}</b>\n"
        f"📈 Winrate: <b>{stats['winrate']}%</b>\n"
        f"💰 Total PnL: <b>"
        f"{'+' if stats['total_pnl'] >= 0 else ''}"
        f"{stats['total_pnl']} USDT</b>\n"
        f"📅 Σήμερα: <b>{stats['today_trades']} trades</b> | "
        f"<b>{'+' if stats['today_pnl'] >= 0 else ''}"
        f"{stats['today_pnl']} USDT</b>\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"<b>Τελευταία {len(trades)} Trades:</b>\n\n"
    )

    # Τελευταία 20 trades με +/- και χρώμα
    for t in trades:
        pnl    = t["pnl_usdt"] or 0
        sym    = t["symbol"].replace("USDT", "")
        side   = "LONG" if t["side"] == "Buy" else "SHORT"

        if t["result"] == "WIN":
            emoji  = "🟢"
            sign   = "+"
            result = "WIN"
        else:
            emoji  = "🔴"
            sign   = "" if pnl < 0 else "+"
            result = "LOSS"

        msg += (
            f"{emoji} <b>{sym}</b> {side} "
            f"<b>{sign}{pnl:.2f} USDT</b> — {result}\n"
        )

    # Αν δεν υπάρχουν trades ακόμα
    if not trades:
        msg += "<i>Δεν υπάρχουν κλειστά trades ακόμα.</i>\n"

    # Tip για sync αν υπάρχουν ανοιχτά
    open_trades = await get_open_trades()
    if open_trades and update.effective_chat.id == ADMIN_CHAT_ID:
        msg += f"\n<i>💡 {len(open_trades)} ανοιχτά trades. Γράψε /sync για συγχρονισμό.</i>"

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
            pnl = round((current - t["entry_price"]) / t["entry_price"]
                        * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        else:
            pnl = round((t["entry_price"] - current) / t["entry_price"]
                        * 100 * t["leverage"] * t["usdt_amount"] / 100, 2)
        msg += (
            f"{emoji} <b>{coin}</b>\n"
            f"  Entry: ${t['entry_price']:,.4f} → Now: ${current:,.4f}\n"
            f"  SL: ${t['sl_price']:,.4f} | TP: ${t['tp_price']:,.4f}\n"
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
        f"💰 <b>Bybit Wallet</b>\n\nΔιαθέσιμο: <b>{balance:,.2f} USDT</b>",
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
    price_target  = estimate_price_target(symbol, current_price, sentiment["score"], side)
    msg = format_sentiment_message(symbol, sentiment, price_target)
    await query.message.reply_text(
        msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ─── MONITOR CLOSED TRADES ────────────────────────────────

async def monitor_closed_trades(application: Application):
    """Κάθε 60 δευτερόλεπτα ελέγχει για κλειστά trades στο Bybit"""
    while True:
        await asyncio.sleep(60)
        await sync_closed_trades(application, notify=True)


# ─── SUBSCRIPTION EXPIRY ──────────────────────────────────

async def check_expiring_subs(application: Application):
    """Κάθε 12 ώρες ελέγχει συνδρομές"""
    while True:
        try:
            # Reminder 1 μέρα πριν τη λήξη
            expiring = await get_expiring_subs(days_ahead=1)
            for user in expiring:
                try:
                    await application.bot.send_message(
                        user["chat_id"],
                        "⚠️ <b>Η συνδρομή σου λήγει αύριο!</b>\n\n"
                        "Ανανέωσέ την με /start για να μην χάσεις πρόσβαση.",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

            # Απενεργοποίηση expired
            expired = await get_expired_subs()
            for user in expired:
                await deactivate_subscription(user["chat_id"])
                try:
                    await application.bot.send_message(
                        user["chat_id"],
                        "⏰ <b>Η συνδρομή σου έληξε!</b>\n\n"
                        "Πάτα /start για να ανανεώσεις.",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Sub expiry error: {e}")
        await asyncio.sleep(43200)


# ─── TRADINGVIEW WEBHOOK ──────────────────────────────────

async def handle_tradingview_webhook(symbol: str, side: str, score: int,
                                     application: Application,
                                     tp_price: float = None,
                                     sl_price: float = None):
    if score < MIN_SIGNAL_SCORE:
        reason = f"Score {score}/100 κάτω από {MIN_SIGNAL_SCORE}"
        await save_rejected_signal(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            format_rejected_message(symbol, side, score, reason),
            parse_mode=ParseMode.HTML)
        return

    open_symbols = await get_open_symbols()
    if symbol in open_symbols:
        reason = f"Υπάρχει ήδη ανοιχτό trade για {symbol}"
        await save_rejected_signal(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            f"⏸ <b>Signal παραλείφθηκε</b>\n\n"
            f"Pair: <b>{symbol.replace('USDT','')}/USDT</b>\n"
            f"Λόγος: <i>{reason}</i>",
            parse_mode=ParseMode.HTML)
        return

    if await get_today_trades_count() >= MAX_DAILY_TRADES:
        reason = f"Ημερήσιο όριο {MAX_DAILY_TRADES} trades"
        await save_rejected_signal(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            format_rejected_message(symbol, side, score, reason),
            parse_mode=ParseMode.HTML)
        return

    if await get_consecutive_losses() >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{MAX_CONSECUTIVE_LOSSES} consecutive losses — παύση"
        await save_rejected_signal(symbol, side, score, reason)
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            format_rejected_message(symbol, side, score, reason),
            parse_mode=ParseMode.HTML)
        return

    sentiment_data = await get_market_sentiment(symbol)
    side_bybit     = "Buy" if side.upper() == "LONG" else "Sell"

    if side_bybit == "Buy" and sentiment_data["score"] < -20:
        reason = f"Bearish sentiment ({sentiment_data['score']}) αντίθετο στο LONG"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    if side_bybit == "Sell" and sentiment_data["score"] > 20:
        reason = f"Bullish sentiment ({sentiment_data['score']}) αντίθετο στο SHORT"
        await save_rejected_signal(symbol, side, score, reason)
        msg = format_rejected_message(symbol, side, score, reason)
        await application.bot.send_message(ADMIN_CHAT_ID, msg, parse_mode=ParseMode.HTML)
        await broadcast(application, msg)
        return

    result = await place_order(
        symbol, side_bybit, DEFAULT_USDT, DEFAULT_LEVERAGE,
        DEFAULT_SL_PCT, DEFAULT_TP_PCT,
        tp_price=tp_price, sl_price=sl_price
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
        await application.bot.send_message(
            ADMIN_CHAT_ID,
            f"❌ <b>Trade failed</b>\n"
            f"Symbol: {symbol}\nSide: {side_bybit}\n"
            f"Error: <code>{result.get('error')}</code>",
            parse_mode=ParseMode.HTML)


# ─── ADMIN PANEL ──────────────────────────────────────────

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats    = await get_stats()
    balance  = await get_wallet_balance()
    pending  = await get_pending_subscriptions()
    open_t   = await get_open_trades()

    msg = (
        f"👑 <b>Admin Panel</b>\n\n"
        f"💰 Wallet: <b>{balance:,.2f} USDT</b>\n"
        f"📊 Trades: {stats['total']} ({stats['wins']}W / {stats['losses']}L)\n"
        f"📈 PnL: <b>"
        f"{'+' if stats['total_pnl']>=0 else ''}"
        f"{stats['total_pnl']} USDT</b>\n"
        f"📅 Σήμερα: {stats['today_trades']} trades\n"
        f"📌 Ανοιχτά: {len(open_t)}\n"
        f"⚠️ Consec. losses: {stats['consecutive_losses']}/{MAX_CONSECUTIVE_LOSSES}\n"
        f"⏳ Pending subs: {len(pending)}\n\n"
        f"<b>Εντολές:</b>\n"
        f"/approve &lt;id&gt; — Έγκριση\n"
        f"/reject &lt;id&gt; — Απόρριψη\n"
        f"/sync — Sync κλειστά trades"
    )
    await update.message.reply_text(
        msg, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Pending Subs", callback_data="admin_pending"),
            InlineKeyboardButton("📈 Positions",    callback_data="admin_positions"),
        ]])
    )


async def admin_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"💳 Code: <code>{sub['paysafe_code']}</code>\n\n"
            f"/approve {sub['chat_id']}\n"
            f"/reject {sub['chat_id']}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"APPROVE:{sub['chat_id']}"),
                InlineKeyboardButton("❌ Reject",  callback_data=f"REJECT:{sub['chat_id']}"),
            ]])
        )


async def admin_positions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await query.answer("❌", show_alert=True)
        return
    await query.answer()
    positions = await get_open_positions()
    if not positions:
        await query.message.reply_text("Δεν υπάρχουν ανοιχτές θέσεις στο Bybit.")
        return
    msg = "📈 <b>Bybit Positions:</b>\n\n"
    for p in positions:
        pnl  = float(p.get("unrealisedPnl", 0))
        msg += (
            f"• {p['symbol']} {p['side']}\n"
            f"  Size: {p['size']} | Entry: ${float(p['avgPrice']):,.4f}\n"
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
        await update.message.reply_text(
            f"ℹ️ <b>Βοήθεια</b>\n\n"
            f"/start — Αρχική\n"
            f"📊 Stats — Στατιστικά & τελευταία 20 trades\n"
            f"💰 Balance — Bybit υπόλοιπο\n"
            f"📰 Sentiment — Ανάλυση αγοράς\n"
            f"📈 Open Trades — Ανοιχτές θέσεις\n\n"
            f"<b>Settings:</b>\n"
            f"• Margin: {DEFAULT_USDT} USDT / {DEFAULT_LEVERAGE}x\n"
            f"• SL: -{DEFAULT_SL_PCT}% | TP: Dynamic\n"
            f"• Max {MAX_DAILY_TRADES} trades/μέρα",
            parse_mode=ParseMode.HTML
        )
    elif text == "👑 Admin Panel" and chat_id == ADMIN_CHAT_ID:
        await admin_panel(update, context)


# ─── AIOHTTP WEBHOOK ──────────────────────────────────────

from aiohttp import web


async def tradingview_handler(request: web.Request):
    try:
        data     = await request.json()
        symbol   = data.get("symbol", "BTCUSDT").upper()
        side     = data.get("side",   "LONG").upper()
        score    = int(float(data.get("score", 0)))
        tp_price = float(data.get("tp", 0)) or None
        sl_price = float(data.get("sl", 0)) or None
        app      = request.app["telegram_app"]
        asyncio.create_task(
            handle_tradingview_webhook(symbol, side, score, app, tp_price, sl_price))
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

    application.add_handler(CommandHandler("start",   start))
    application.add_handler(CommandHandler("stats",   stats_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject",  reject_command))
    application.add_handler(CommandHandler("sync",    sync_command))

    application.add_handler(CallbackQueryHandler(
        subscribe_callback,       pattern="^subscribe$"))
    application.add_handler(CallbackQueryHandler(
        approve_callback,         pattern="^(APPROVE|REJECT):"))
    application.add_handler(CallbackQueryHandler(
        sentiment_callback,       pattern="^sent_"))
    application.add_handler(CallbackQueryHandler(
        admin_pending_callback,   pattern="^admin_pending$"))
    application.add_handler(CallbackQueryHandler(
        admin_positions_callback, pattern="^admin_positions$"))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message))

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

    port   = int(__import__("os").environ.get("PORT", 8080))
    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"🚀 Bot running on port {port}")

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
