"""
test_leaderboard.py
-------------------
Τρέξε με: python test_leaderboard.py
"""

import asyncio
import logging
from leaderboard import BybitLeaderboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


async def main():
    board = BybitLeaderboard(period="weekly", max_traders=10)

    try:
        print("⏳ Fetching top traders...")
        traders = await board.get_top_traders()

        if not traders:
            print("❌ Δεν επιστράφηκαν traders. Έλεγξε σύνδεση ή το API endpoint.")
            return

        board.print_traders(traders)

        # Παίρνουμε τους top 3 και βλέπουμε θέσεις
        top3_uids = [t.uid for t in traders[:3]]
        print(f"⏳ Fetching positions για top 3 traders...")

        all_positions = await board.get_all_positions(top3_uids)

        for uid, positions in all_positions.items():
            board.print_positions(uid, positions)

        print("\n✅ Leaderboard module δουλεύει σωστά!")

    finally:
        await board.close()


if __name__ == "__main__":
    asyncio.run(main())
