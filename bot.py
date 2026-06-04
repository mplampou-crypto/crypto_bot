"""
bot.py — CopyBot Telegram Interface
Εντολές:
  /start      - Καλωσόρισμα + status
  /traders    - Traders από config
  /following  - Ποιους ακολουθείς
  /positions  - Ανοιχτές θέσεις
  /stats      - P&L στατιστικά
  /blacklist  - Δες/προσθέτεις blacklisted coins
  /pause      - Παύση copy trading
  /resume     - Συνέχεια copy trading
"""

import asyncio
import json
import logging
from datetime import datetime, time as dt_time
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode

from leaderboard import BybitLeaderboard, Trader, load_traders_config
from detector import PositionDetector, TradeEvent
from executor import Executor
from database import Database
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    POLL_INTERVAL_SEC, LEADERBOARD_PERIOD, MAX_TRADERS_TO_FOLLOW
)

logger = logging.getLogger(__name__)
GREECE_TZ = pytz.timezone("Europe/Athens")

PLATFORM_EMOJI = {
    "bybit": "🟡",
    "binance": "🟠",
    "okx": "🔵",
    "hyperliquid": "🟣",
}


class CopyBotTelegram:

    def __init__(self):
        self.leaderboard = BybitLeaderboard(
            period=LEADERBOARD_PERIOD,
            max_traders=MAX_TRADERS_TO_FOLLOW
        )
        self.detector = PositionDetector()
        self.executor = Executor()
        self.db = Database()
        self.followed: dict[str, str] = {}
        self.paused = False
        self._last_traders: list[Trader] = []

        for t in self.db.get_active_traders():
            self.followed[t["uid"]] = t["nickname"]
            self.detector.set_nickname(t["uid"], t["nickname"])

    def _is_authorized(self, update: Update) -> bool:
        return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)

    async def _send(self, app: Application, text: str):
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )

    # ── /start ────────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        balance = self.executor.get_balance()
        config = load_traders_config()
        total_configured = sum(
            len(config.get(p, [])) for p in ["bybit", "binance", "okx", "hyperliquid"]
        )
        status = "⏸ Παύση" if self.paused else "▶️ Ενεργό"
        text = (
            "🤖 <b>CopyBot</b>\n\n"
            f"💰 Balance: <b>{balance:.2f} USDT</b>\n"
            f"👥 Following: <b>{len(self.followed)}</b> traders\n"
            f"📋 Στο config: <b>{total_configured}</b> traders\n"
            f"🔄 Status: {status}\n\n"
            "<b>Εντολές:</b>\n"
            "/traders — traders από config\n"
            "/following — ποιους ακολουθείς\n"
            "/positions — ανοιχτές θέσεις\n"
            "/stats — στατιστικά P&L\n"
            "/blacklist — blacklisted coins\n"
            "/pause — παύση\n"
            "/resume — συνέχεια"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # ── /traders ─────────────────────────────────────────────────────────────

    async def cmd_traders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        await update.message.reply_text("⏳ Φορτώνω traders από config...")

        traders = await self.leaderboard.get_top_traders()

        if not traders:
            await update.message.reply_text(
                "❌ <b>Δεν υπάρχουν traders στο config!</b>\n\n"
                "Πρόσθεσε UIDs στο <code>traders.json</code> στο GitHub:\n\n"
                "<pre>{\n"
                '  "bybit": ["123456789"],\n'
                '  "binance": ["portfolio_id"],\n'
                '  "okx": ["unique_code"],\n'
                '  "hyperliquid": ["0xABCD..."],\n'
                '  "blacklist": ["DOGEUSDT"]\n'
                "}</pre>",
                parse_mode=ParseMode.HTML
            )
            return

        self._last_traders = traders
        keyboard = []
        for t in traders:
            is_followed = t.uid in self.followed
            emoji = PLATFORM_EMOJI.get(t.platform, "⚪")
            label = f"{'✅' if is_followed else '➕'} {emoji} {t.nickname}"
            action = f"unfollow:{t.uid}" if is_followed else f"follow:{t.uid}"
            keyboard.append([InlineKeyboardButton(label, callback_data=action)])

        markup = InlineKeyboardMarkup(keyboard)

        platforms_summary = []
        config = load_traders_config()
        for p, emoji in PLATFORM_EMOJI.items():
            count = len(config.get(p, []))
            if count > 0:
                platforms_summary.append(f"{emoji} {p.capitalize()}: {count}")

        await update.message.reply_text(
            f"📋 <b>Traders στο config ({len(traders)}):</b>\n"
            + "\n".join(platforms_summary) + "\n\n"
            "Πάτα για follow/unfollow:",
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )

    # ── /following ────────────────────────────────────────────────────────────

    async def cmd_following(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        if not self.followed:
            await update.message.reply_text(
                "👥 Δεν ακολουθείς κανέναν trader.\n/traders για να επιλέξεις."
            )
            return

        lines = ["👥 <b>Traders που ακολουθείς:</b>\n"]
        keyboard = []
        for uid, nickname in self.followed.items():
            positions = self.detector.get_snapshot(uid)
            lines.append(f"• <b>{nickname}</b> — {len(positions)} ανοιχτές θέσεις")
            keyboard.append([InlineKeyboardButton(f"❌ Unfollow {nickname}", callback_data=f"unfollow:{uid}")])

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

    # ── /positions ────────────────────────────────────────────────────────────

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

    # ── /stats ────────────────────────────────────────────────────────────────

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        s = self.db.get_stats()
        pnl_emoji = "📈" if s["total_pnl"] >= 0 else "📉"
        balance = self.executor.get_balance()
        text = (
            f"📊 <b>Στατιστικά</b>\n\n"
            f"💰 Balance: <b>{balance:.2f} USDT</b>\n"
            f"Συνολικά trades: <b>{s['total_trades']}</b>\n"
            f"✅ Wins: <b>{s['wins']}</b> | ❌ Losses: <b>{s['losses']}</b>\n"
            f"🎯 Win rate: <b>{s['win_rate']}%</b>\n"
            f"{pnl_emoji} Total PnL: <b>{s['total_pnl']:+.2f} USDT</b>\n"
            f"📌 Avg PnL/trade: <b>{s['avg_pnl']:+.2f} USDT</b>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # ── /blacklist ────────────────────────────────────────────────────────────

    async def cmd_blacklist(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        config = load_traders_config()
        blacklist = config.get("blacklist", [])
        if blacklist:
            coins = "\n".join(f"• <code>{c}</code>" for c in blacklist)
            text = f"🚫 <b>Blacklisted coins ({len(blacklist)}):</b>\n\n{coins}\n\n<i>Επεξεργάσου το traders.json στο GitHub για αλλαγές.</i>"
        else:
            text = "🚫 <b>Blacklist</b>\n\nΔεν υπάρχουν blacklisted coins.\n\n<i>Πρόσθεσε coins στο traders.json → \"blacklist\"</i>"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # ── /pause & /resume ──────────────────────────────────────────────────────

    async def cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        self.paused = True
        await update.message.reply_text("⏸ Copy trading σε παύση.\n/resume για συνέχεια.")

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update): return
        self.paused = False
        await update.message.reply_text("▶️ Copy trading ενεργό ξανά!")

    # ── Follow / Unfollow buttons ─────────────────────────────────────────────

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
            emoji = PLATFORM_EMOJI.get(trader.platform, "⚪")
            await query.edit_message_text(
                f"✅ Ακολουθείς τώρα τον <b>{trader.nickname}</b>!\n"
                f"{emoji} Πλατφόρμα: <b>{trader.platform.capitalize()}</b>\n\n"
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

    # ── Daily P&L report ──────────────────────────────────────────────────────

    async def _daily_report(self, app: Application):
        """Στέλνει daily P&L report κάθε μέρα στις 08:00 ώρα Ελλάδας."""
        while True:
            try:
                now = datetime.now(GREECE_TZ)
                # Υπολόγισε πόσα δευτερόλεπτα μέχρι τις 08:00
                target = now.replace(hour=8, minute=0, second=0, microsecond=0)
                if now >= target:
                    # Αν πέρασαν τις 8, περίμενε μέχρι αύριο
                    from datetime import timedelta
                    target += timedelta(days=1)
                wait_secs = (target - now).total_seconds()
                await asyncio.sleep(wait_secs)

                # Στείλε report
                s = self.db.get_stats()
                balance = self.executor.get_balance()
                pnl_emoji = "📈" if s["total_pnl"] >= 0 else "📉"
                today = datetime.now(GREECE_TZ).strftime("%d/%m/%Y")

                text = (
                    f"📊 <b>Daily Report — {today}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Balance: <b>{balance:.2f} USDT</b>\n"
                    f"{pnl_emoji} Συνολικό PnL: <b>{s['total_pnl']:+.2f} USDT</b>\n"
                    f"📈 Trades: <b>{s['total_trades']}</b> "
                    f"({s['wins']} win / {s['losses']} loss)\n"
                    f"🎯 Win rate: <b>{s['win_rate']}%</b>\n"
                    f"👥 Following: <b>{len(self.followed)}</b> traders\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                )
                await self._send(app, text)
                logger.info("Daily report sent")

            except Exception as e:
                logger.error(f"Daily report error: {e}")
                await asyncio.sleep(3600)

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self, app: Application):
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
            result = self.executor.handle_event(event)
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

            notif = event.summary()
            if result:
                notif += f"\n\n✅ <b>Order εκτελέστηκε</b>" if result.success else f"\n\n❌ <b>Order απέτυχε:</b> {result.error}"

            await self._send(app, notif)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self):
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start",     self.cmd_start))
        app.add_handler(CommandHandler("traders",   self.cmd_traders))
        app.add_handler(CommandHandler("following", self.cmd_following))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("stats",     self.cmd_stats))
        app.add_handler(CommandHandler("blacklist", self.cmd_blacklist))
        app.add_handler(CommandHandler("pause",     self.cmd_pause))
        app.add_handler(CommandHandler("resume",    self.cmd_resume))
        app.add_handler(CallbackQueryHandler(self.on_button))

        async def post_init(application: Application):
            asyncio.create_task(self._poll_loop(application))
            asyncio.create_task(self._daily_report(application))

        app.post_init = post_init
        logger.info("🤖 CopyBot ξεκίνησε")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    )
    bot = CopyBotTelegram()
    bot.run()
