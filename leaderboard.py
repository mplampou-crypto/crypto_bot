"""
leaderboard.py
--------------
Κάνει fetch τους top traders από το Bybit Leaderboard
και τις ανοιχτές θέσεις τους.

Bybit δεν έχει official documented API για το leaderboard,
αλλά υπάρχουν δύο undocumented endpoints που δουλεύουν:

  POST https://api2.bybit.com/fapi/beehive/public/v1/common/rank/list
       → Λίστα top traders (UID, nickname, ROI, PnL)

  POST https://api2.bybit.com/fapi/beehive/public/v1/user/position
       → Ανοιχτές θέσεις συγκεκριμένου trader (public profile)
"""

import asyncio
import aiohttp
import json
import logging
import traceback
import ssl
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Bybit undocumented leaderboard endpoints ──────────────────────────────────
BASE_URL = "https://api2.bybit.com"
RANK_LIST_URL = f"{BASE_URL}/fapi/beehive/public/v1/common/rank/list"
POSITIONS_URL = f"{BASE_URL}/fapi/beehive/public/v1/user/position/query"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; CopyBot/1.0)",
    "Referer": "https://www.bybit.com/",
}

# period map: daily=1, weekly=2, monthly=3, all-time=4
PERIOD_MAP = {"daily": 1, "weekly": 2, "monthly": 3, "all": 4}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Trader:
    uid: str
    nickname: str
    roi: float          # Return on Investment %
    pnl: float          # Total PnL in USDT
    win_rate: float     # Win rate %
    followers: int
    rank: int

    def __repr__(self):
        return f"#{self.rank} {self.nickname} | ROI: {self.roi:.1f}% | PnL: {self.pnl:.0f} USDT"


@dataclass
class Position:
    uid: str
    symbol: str         # π.χ. "BTCUSDT"
    side: str           # "Buy" ή "Sell"
    size: float         # ποσότητα
    entry_price: float
    leverage: int
    unrealised_pnl: float
    created_time: datetime
    position_id: str = ""

    @property
    def direction(self) -> str:
        return "LONG" if self.side == "Buy" else "SHORT"

    def __repr__(self):
        return (
            f"{self.symbol} {self.direction} | "
            f"Size: {self.size} | Entry: {self.entry_price} | "
            f"x{self.leverage} | PnL: {self.unrealised_pnl:.2f}"
        )


# ── Main Leaderboard class ────────────────────────────────────────────────────

class BybitLeaderboard:
    """
    Fetches και cache-άρει το Bybit leaderboard.
    Χρησιμοποιεί aiohttp για async requests.
    """

    def __init__(self, period: str = "weekly", max_traders: int = 10):
        self.period = PERIOD_MAP.get(period, 2)
        self.max_traders = max_traders
        self._session: Optional[aiohttp.ClientSession] = None

        # Cache: uid -> list[Position]
        self._positions_cache: dict[str, list[Position]] = {}
        # Cache: list of top traders
        self._traders_cache: list[Trader] = []

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            # SSL context για Railway/Docker environments
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(
                headers=HEADERS,
                timeout=timeout,
                connector=connector
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Fetch top traders ─────────────────────────────────────────────────────

    async def get_top_traders(self, category: str = "linear") -> list[Trader]:
        """
        Επιστρέφει τους top traders από το leaderboard.
        category: "linear" (USDT perpetuals) | "inverse" | "option"
        """
        session = await self._get_session()
        payload = {
            "category": category,
            "period": self.period,
            "pageNo": 1,
            "pageSize": self.max_traders,
        }

        try:
            async with session.post(RANK_LIST_URL, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if data.get("retCode") != 0:
                logger.error(f"Leaderboard API error: {data.get('retMsg')}")
                return []

            traders = []
            for i, item in enumerate(data.get("result", {}).get("list", []), 1):
                traders.append(Trader(
                    uid=item.get("userId", ""),
                    nickname=item.get("nickName", f"Trader_{i}"),
                    roi=float(item.get("roi", 0)) * 100,
                    pnl=float(item.get("pnl", 0)),
                    win_rate=float(item.get("winRate", 0)) * 100,
                    followers=int(item.get("followers", 0)),
                    rank=i,
                ))

            self._traders_cache = traders
            logger.info(f"Fetched {len(traders)} traders from leaderboard")
            return traders

        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching leaderboard: {e}")
            return self._traders_cache  # επιστρέφουμε cached αν έχουμε

        except Exception as e:
            logger.error(f"Unexpected error fetching traders: {e!r}\n{traceback.format_exc()}")
            return []

    # ── Fetch positions για έναν trader ──────────────────────────────────────

    async def get_trader_positions(self, uid: str) -> list[Position]:
        """
        Επιστρέφει τις ανοιχτές θέσεις ενός trader.
        Μόνο traders με PUBLIC profile είναι ορατοί.
        """
        session = await self._get_session()
        payload = {"userId": uid, "category": "linear"}

        try:
            async with session.post(POSITIONS_URL, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if data.get("retCode") != 0:
                logger.warning(f"No positions for uid {uid}: {data.get('retMsg')}")
                return []

            positions = []
            for item in data.get("result", {}).get("list", []):
                size = float(item.get("size", 0))
                if size == 0:
                    continue  # skip κλειστές θέσεις

                positions.append(Position(
                    uid=uid,
                    symbol=item.get("symbol", ""),
                    side=item.get("side", "Buy"),
                    size=size,
                    entry_price=float(item.get("avgPrice", 0)),
                    leverage=int(item.get("leverage", 1)),
                    unrealised_pnl=float(item.get("unrealisedPnl", 0)),
                    created_time=datetime.fromtimestamp(
                        int(item.get("createdTime", 0)) / 1000
                    ),
                    position_id=item.get("positionIdx", ""),
                ))

            return positions

        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching positions for {uid}: {e}")
            return self._positions_cache.get(uid, [])

        except Exception as e:
            logger.error(f"Unexpected error for uid {uid}: {e!r}\n{traceback.format_exc()}")
            return []

    # ── Fetch positions για όλους τους tracked traders ────────────────────────

    async def get_all_positions(self, trader_uids: list[str]) -> dict[str, list[Position]]:
        """
        Κάνει concurrent fetch θέσεων για πολλούς traders.
        Επιστρέφει dict: uid -> list[Position]
        """
        tasks = [self.get_trader_positions(uid) for uid in trader_uids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        positions_map = {}
        for uid, result in zip(trader_uids, results):
            if isinstance(result, Exception):
                logger.error(f"Error for {uid}: {result}")
                positions_map[uid] = self._positions_cache.get(uid, [])
            else:
                positions_map[uid] = result
                self._positions_cache[uid] = result  # update cache

        return positions_map

    # ── Pretty print για debugging ────────────────────────────────────────────

    def print_traders(self, traders: list[Trader]):
        print("\n" + "═" * 60)
        print(f"  🏆 BYBIT LEADERBOARD TOP {len(traders)}")
        print("═" * 60)
        for t in traders:
            print(f"  {t}")
        print("═" * 60 + "\n")

    def print_positions(self, uid: str, positions: list[Position]):
        nickname = next(
            (t.nickname for t in self._traders_cache if t.uid == uid), uid
        )
        print(f"\n  📊 {nickname} — Ανοιχτές θέσεις: {len(positions)}")
        for p in positions:
            emoji = "🟢" if p.direction == "LONG" else "🔴"
            print(f"    {emoji} {p}")
