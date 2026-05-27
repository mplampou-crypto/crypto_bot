"""
bot.py
------
Telegram bot interface.

Εντολές:
  /start        - Καλωσόρισμα
  /traders      - Δες top traders + κουμπί Follow
  /following    - Ποιους ακολουθείς τώρα
  /positions    - Ανοιχτές θέσεις σου
  /stats        - P&L στατιστικά
  /pause        - Παύση copy trading
  /resume       - Συνέχεια copy trading
  /stop         - Τερματισμός bot
"""

import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

from leaderboard import BybitLeaderboard, Trader
from detector import PositionDetector, TradeEvent
from executor import Executor
from database import Database
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    POLL_INTERVAL_SEC, LEADERBOARD_PERIOD, MAX_TRADERS_TO_FOLLOW
)

logger = logging.getLogger(__name__)


class CopyBotTelegram:

    def __init__(self):
        self.leaderboard = BybitLeaderboard(
            period=LEADERBOARD_PERIOD,
            max_traders=MAX_TRADERS_TO_FOLLOW
        )
        self.detector = PositionDetector()
        self.executor = Executor()
        self.db       = Database()

        # uid -> nickname
        self.followed: dict[str, str] = {}
        self.paused = False

        # Cache τελευταίων traders για το follow menu
        self._last_traders: list[Trader] = []

        # Φόρτωσε followed traders από DB (αν υπάρχουν από προηγούμενη εκτέλεση)
        for t in self.db.get_active_traders():
            self.followed[t["uid"]] = t["nickname"]
            self.detector.set_nickname(t["uid"], t["nickname"])

    # ── Βοηθητικές ───────────────────────────────────────────────────────────

    def _is_authorized(self, update: Update) -> bool:
        """Αποδέχεται μόνο μηνύματα από το δικό σου chat."""
        return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)

    async def _send(self, app: Application, text: str):
        """Στέλνει μήνυμα στο chat σου (για notifications από το polling loop)."""
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        balance = self.executor.get_balance()
        text = (
            "🤖 <b>CopyBot ξεκίνησε!</b>\n\n"
            f"💰 Balance: <b>{balance:.2f} USDT</b>\n"
            f"👥 Following: <b>{len(self.followed)}</b> traders\n\n"
            "Εντολές:\n"
            "/traders — δες top traders\n"
            "/following — ποιους ακολουθείς\n"
            "/positions — ανοιχτές θέσεις\n"
            "/stats — στατιστικά P&L\n"
            "/pause — παύση\n"
            "/resume — συνέχεια"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_traders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        await update.message.reply_text("⏳ Φορτώνω leaderboard...")

        traders = await self.leaderboard.get_top_traders()
        if not traders:
            await update.message.reply_text("❌ Δεν βρέθηκαν traders. Έλεγξε σύνδεση.")
            return

        self._last_traders = traders

        # Φτιάχνουμε inline keyboard — ένα κουμπί Follow/Unfollow ανά trader
        keyboard = []
        for t in traders:
            is_followed = t.uid in self.followed
            label = f"{'✅' if is_followed else '➕'} #{t.rank} {t.nickname} | ROI {t.roi:.1f}%"
            action = f"unfollow:{t.uid}" if is_followed else f"follow:{t.uid}"
            keyboard.append([InlineKeyboardButton(label, callback_data=action)])

        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🏆 <b>Top {len(traders)} Traders</b> (εβδομαδιαίο)\n"
            "Πάτα για follow/unfollow:",
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )

    async def cmd_following(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if not self.followed:
            await update.message.reply_text("👥 Δεν ακολουθείς κανέναν trader ακόμα.\n/traders για να επιλέξεις.")
            return

        lines = ["👥 <b>Traders που ακολουθείς:</b>\n"]
        keyboard = []
        for uid, nickname in self.followed.items():
            positions = self.detector.get_snapshot(uid)
            lines.append(f"• <b>{nickname}</b> — {len(positions)} ανοιχτές θέσεις")
            keyboard.append([InlineKeyboardButton(f"❌ Unfollow {nickname}", callback_data=f"unfollow:{uid}")])

        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )

    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        trades = self.db.get_open_trades()
        if not trades:
            await update.message.reply_text("📊 Δεν έχεις ανοιχτές θέσεις.")
            return

        lines = [f"📊 <b>Ανοιχτές θέσεις ({len(trades)}):</b>\n"]
        for t in trades:
            emoji = "🟢" if t["side"] == "Buy" else "🔴"
            lines.append(
                f"{emoji} <b>{t['symbol']}</b> {'LONG' if t['side']=='Buy' else 'SHORT'}\n"
                f"   Από: {t['trader_nickname']} | Size: {t['size']} | Entry: {t['entry_price']}"
            )

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        s = self.db.get_stats()
        pnl_emoji = "📈" if s["total_pnl"] >= 0 else "📉"
        text = (
            f"📊 <b>Στατιστικά</b>\n\n"
            f"Συνολικά trades: <b>{s['total_trades']}</b>\n"
            f"✅ Wins: <b>{s['wins']}</b> | ❌ Losses: <b>{s['losses']}</b>\n"
            f"🎯 Win rate: <b>{s['win_rate']}%</b>\n"
            f"{pnl_emoji} Total PnL: <b>{s['total_pnl']:+.2f} USDT</b>\n"
            f"📌 Avg PnL/trade: <b>{s['avg_pnl']:+.2f} USDT</b>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        self.paused = True
        await update.message.reply_text("⏸ Copy trading σε παύση.\n/resume για να συνεχίσεις.")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        self.paused = False
        await update.message.reply_text("▶️ Copy trading ενεργό ξανά!")

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        await update.message.reply_text("🛑 Τερματισμός bot...")
        await self.leaderboard.close()

    # ── Callback από κουμπιά (Follow / Unfollow) ──────────────────────────────

    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_authorized(update): return

        action, uid = query.data.split(":", 1)

        if action == "follow":
            trader = next((t for t in self._last_traders if t.uid == uid), None)
            if not trader:
                await query.edit_message_text("❌ Trader δεν βρέθηκε, ξαναπροσπάθησε με /traders")
                return
            self.followed[uid] = trader.nickname
            self.detector.set_nickname(uid, trader.nickname)
            self.db.add_trader(uid, trader.nickname)
            await query.edit_message_text(
                f"✅ Ακολουθείς τώρα τον <b>{trader.nickname}</b>!\n"
                f"ROI: {trader.roi:.1f}% | Win rate: {trader.win_rate:.1f}%\n\n"
                f"Το bot θα αντιγράφει αυτόματα τις θέσεις του.",
                parse_mode=ParseMode.HTML
            )

        elif action == "unfollow":
            nickname = self.followed.pop(uid, uid)
            self.detector.clear(uid)
            self.db.remove_trader(uid)
            await query.edit_message_text(
                f"❌ Σταμάτησες να ακολουθείς τον <b>{nickname}</b>.",
                parse_mode=ParseMode.HTML
            )

    # ── Polling loop (τρέχει παράλληλα) ──────────────────────────────────────

    async def _poll_loop(self, app: Application):
        """Κύριος βρόχος — τρέχει κάθε POLL_INTERVAL_SEC δευτερόλεπτα."""
        logger.info("🔄 Polling loop ξεκίνησε")

        while True:
            try:
                if not self.paused and self.followed:
                    await self._tick(app)
            except Exception as e:
                logger.error(f"Tick error: {e}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _tick(self, app: Application):
        uids = list(self.followed.keys())
        positions_map = await self.leaderboard.get_all_positions(uids)
        events = self.detector.detect_all(positions_map)

        for event in events:
            # Εκτέλεση order
            result = self.executor.handle_event(event)

            # Αποθήκευση στη DB
            p = event.position
            if result and result.success:
                from detector import EventType
                if event.event_type == EventType.OPEN:
                    self.db.open_trade(
                        trader_uid=event.trader_uid,
                        trader_nickname=event.trader_nickname,
                        symbol=p.symbol,
                        side=p.side,
                        size=result.size,
                        entry_price=p.entry_price,
                        order_id=result.order_id,
                    )
                elif event.event_type == EventType.CLOSE:
                    self.db.close_trade(
                        trader_uid=event.trader_uid,
                        symbol=p.symbol,
                        side=p.side,
                        exit_price=p.entry_price,
                        pnl=p.unrealised_pnl,
                    )

            # Telegram notification
            notif = event.summary()
            if result:
                if result.success:
                    notif += f"\n\n✅ <b>Order εκτελέστηκε</b> (id: {result.order_id})"
                else:
                    notif += f"\n\n❌ <b>Order απέτυχε:</b> {result.error}"

            await self._send(app, notif)

    # ── Εκκίνηση ─────────────────────────────────────────────────────────────

    def run(self):
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Καταχώρηση handlers
        app.add_handler(CommandHandler("start",     self.cmd_start))
        app.add_handler(CommandHandler("traders",   self.cmd_traders))
        app.add_handler(CommandHandler("following", self.cmd_following))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("stats",     self.cmd_stats))
        app.add_handler(CommandHandler("pause",     self.cmd_pause))
        app.add_handler(CommandHandler("resume",    self.cmd_resume))
        app.add_handler(CommandHandler("stop",      self.cmd_stop))
        app.add_handler(CallbackQueryHandler(self.on_button))

        # Εκκίνηση polling loop παράλληλα
        async def post_init(application: Application):
            asyncio.create_task(self._poll_loop(application))

        app.post_init = post_init

        logger.info("🤖 Telegram bot ξεκίνησε — περιμένει εντολές...")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    )
    bot = CopyBotTelegram()
    bot.run()
