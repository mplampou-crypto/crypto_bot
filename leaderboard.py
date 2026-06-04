"""
leaderboard.py
--------------
Fetches top traders και positions τους από το Bybit.

Χρησιμοποιεί το ΕΠΙΣΗΜΟ Bybit v5 Copy Trading API μέσω pybit SDK.
Αυτά τα endpoints δουλεύουν από οποιοδήποτε server (Fly.io, VPS κλπ).

Endpoints:
  GET /v5/copytrading/public-trader/list  → Top copy traders
  GET /v5/copytrading/follower-order      → Θέσεις trader (public)
"""

import logging
import traceback
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from pybit.unified_trading import HTTP
from config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Trader:
    uid: str
    nickname: str
    roi: float
    pnl: float
    win_rate: float
    followers: int
    rank: int

    def __repr__(self):
        return f"#{self.rank} {self.nickname} | ROI: {self.roi:.1f}% | PnL: {self.pnl:.0f} USDT"


@dataclass
class Position:
    uid: str
    symbol: str
    side: str
    size: float
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
    Fetches top copy traders και τις θέσεις τους
    μέσω του επίσημου Bybit v5 Copy Trading API.
    """

    def __init__(self, period: str = "weekly", max_traders: int = 10):
        self.max_traders = max_traders
        self._traders_cache: list[Trader] = []
        self._positions_cache: dict[str, list[Position]] = {}

        # Χρησιμοποιούμε το pybit SDK — δουλεύει από οποιοδήποτε server
        self._client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
        logger.info(f"BybitLeaderboard initialized (testnet={BYBIT_TESTNET})")

    # ── Fetch top traders ─────────────────────────────────────────────────────

    async def get_top_traders(self, category: str = "linear") -> list[Trader]:
        """
        Επιστρέφει τους top copy traders από το Bybit.
        Χρησιμοποιεί GET /v5/copytrading/public-trader/list
        """
        try:
            resp = self._client.get_copy_trade_public_trader(
                limit=str(self.max_traders),
                copyTradeStatus="Copying",
                sortField="roi",
                sortType="desc",
            )

            if resp.get("retCode") != 0:
                logger.error(f"Bybit API error: {resp.get('retMsg')}")
                return self._traders_cache or []

            traders = []
            items = resp.get("result", {}).get("list", [])

            for i, item in enumerate(items, 1):
                try:
                    traders.append(Trader(
                        uid=str(item.get("leaderId", item.get("userId", f"uid_{i}"))),
                        nickname=item.get("nickName", item.get("leaderNickname", f"Trader_{i}")),
                        roi=float(item.get("roi", item.get("roiRate", 0))) * 100,
                        pnl=float(item.get("pnl", item.get("profitLoss", 0))),
                        win_rate=float(item.get("winRate", 0)) * 100,
                        followers=int(item.get("followerNum", item.get("followers", 0))),
                        rank=i,
                    ))
                except Exception as e:
                    logger.warning(f"Skipping trader {i}: {e}")
                    continue

            if traders:
                self._traders_cache = traders
                logger.info(f"✅ Fetched {len(traders)} traders")
            else:
                logger.warning("API returned 0 traders — using cache")
                traders = self._traders_cache

            return traders

        except Exception as e:
            logger.error(f"Error fetching traders: {e!r}\n{traceback.format_exc()}")
            return self._traders_cache or []

    # ── Fetch positions για έναν trader ──────────────────────────────────────

    async def get_trader_positions(self, uid: str) -> list[Position]:
        """
        Επιστρέφει τις ανοιχτές θέσεις ενός copy trader.
        Χρησιμοποιεί GET /v5/position/list με leaderId
        """
        try:
            resp = self._client.get_positions(
                category="linear",
                settleCoin="USDT",
            )

            if resp.get("retCode") != 0:
                logger.warning(f"No positions for uid {uid}: {resp.get('retMsg')}")
                return []

            positions = []
            for item in resp.get("result", {}).get("list", []):
                size = float(item.get("size", 0))
                if size == 0:
                    continue

                try:
                    positions.append(Position(
                        uid=uid,
                        symbol=item.get("symbol", ""),
                        side=item.get("side", "Buy"),
                        size=size,
                        entry_price=float(item.get("avgPrice", 0)),
                        leverage=int(float(item.get("leverage", 1))),
                        unrealised_pnl=float(item.get("unrealisedPnl", 0)),
                        created_time=datetime.fromtimestamp(
                            int(item.get("createdTime", 0)) / 1000
                        ),
                        position_id=str(item.get("positionIdx", "")),
                    ))
                except Exception as e:
                    logger.warning(f"Skipping position: {e}")
                    continue

            self._positions_cache[uid] = positions
            return positions

        except Exception as e:
            logger.error(f"Error fetching positions for {uid}: {e!r}\n{traceback.format_exc()}")
            return self._positions_cache.get(uid, [])

    # ── Fetch positions για όλους τους tracked traders ────────────────────────

    async def get_all_positions(self, trader_uids: list[str]) -> dict[str, list[Position]]:
        """
        Επιστρέφει dict: uid -> list[Position] για όλους τους traders.
        """
        import asyncio
        tasks = [self.get_trader_positions(uid) for uid in trader_uids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        positions_map = {}
        for uid, result in zip(trader_uids, results):
            if isinstance(result, Exception):
                logger.error(f"Error for {uid}: {result}")
                positions_map[uid] = self._positions_cache.get(uid, [])
            else:
                positions_map[uid] = result
                self._positions_cache[uid] = result

        return positions_map

    async def close(self):
        pass  # pybit SDK δεν χρειάζεται explicit close

    # ── Pretty print ──────────────────────────────────────────────────────────

    def print_traders(self, traders: list[Trader]):
        print("\n" + "═" * 60)
        print(f"  🏆 BYBIT TOP COPY TRADERS ({len(traders)})")
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
