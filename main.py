"""
main.py
-------
Ο κεντρικός βρόχος του bot.
Κάθε POLL_INTERVAL_SEC δευτερόλεπτα:
  1. Παίρνει θέσεις από tracked traders
  2. Βρίσκει αλλαγές με τον detector
  3. Εκτελεί orders με τον executor
  4. Στέλνει notification στο Telegram
"""

import asyncio
import logging
from leaderboard import BybitLeaderboard
from detector import PositionDetector
from executor import Executor
from config import POLL_INTERVAL_SEC, LEADERBOARD_PERIOD, MAX_TRADERS_TO_FOLLOW

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger(__name__)


class CopyBot:
    def __init__(self):
        self.leaderboard = BybitLeaderboard(
            period=LEADERBOARD_PERIOD,
            max_traders=MAX_TRADERS_TO_FOLLOW
        )
        self.detector  = PositionDetector()
        self.executor  = Executor()

        # Traders που ακολουθούμε: uid -> nickname
        self.followed: dict[str, str] = {}
        self.running = False

    # ── Follow / Unfollow ─────────────────────────────────────────────────────

    def follow(self, uid: str, nickname: str):
        self.followed[uid] = nickname
        self.detector.set_nickname(uid, nickname)
        logger.info(f"+ Following: {nickname} ({uid})")

    def unfollow(self, uid: str):
        nickname = self.followed.pop(uid, uid)
        self.detector.clear(uid)
        logger.info(f"- Unfollowed: {nickname}")

    # ── Κύριος βρόχος ─────────────────────────────────────────────────────────

    async def run(self):
        self.running = True
        logger.info("🤖 CopyBot ξεκίνησε")

        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Σφάλμα στο tick: {e}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _tick(self):
        if not self.followed:
            return

        # 1. Fetch θέσεις
        uids = list(self.followed.keys())
        positions_map = await self.leaderboard.get_all_positions(uids)

        # 2. Detect αλλαγές
        events = self.detector.detect_all(positions_map)

        # 3. Εκτέλεση + notification
        for event in events:
            logger.info(f"EVENT: {event.summary()}")
            result = self.executor.handle_event(event)
            if result:
                logger.info(str(result))

    async def stop(self):
        self.running = False
        await self.leaderboard.close()
        logger.info("🛑 CopyBot σταμάτησε")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    bot = CopyBot()

    # Παράδειγμα: κάνε follow χειροκίνητα (αργότερα θα γίνεται από Telegram)
    # bot.follow("123456789", "TopTrader1")

    # Ή: auto-follow top 3 από leaderboard
    traders = await bot.leaderboard.get_top_traders()
    for t in traders[:3]:
        bot.follow(t.uid, t.nickname)

    try:
        await bot.run()
    except KeyboardInterrupt:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
