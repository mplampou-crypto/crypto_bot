"""
leaderboard.py
--------------
Multi-platform position fetcher.

Υποστηριζόμενες πλατφόρμες:
  - Bybit   → pybit SDK (επίσημο API)
  - Binance → public leaderboard API
  - OKX     → public copy trading API
  - Hyperliquid → on-chain public API (δουλεύει πάντα από servers)
"""

import logging
import traceback
import aiohttp
import json
import ssl
from dataclasses import dataclass
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
    platform: str = "bybit"

    def __repr__(self):
        return f"#{self.rank} [{self.platform.upper()}] {self.nickname} | ROI: {self.roi:.1f}%"


@dataclass
class Position:
    uid: str
    symbol: str
    side: str          # "Buy" ή "Sell"
    size: float
    entry_price: float
    leverage: int
    unrealised_pnl: float
    created_time: datetime
    position_id: str = ""
    platform: str = "bybit"

    @property
    def direction(self) -> str:
        return "LONG" if self.side == "Buy" else "SHORT"

    def __repr__(self):
        return (
            f"[{self.platform.upper()}] {self.symbol} {self.direction} | "
            f"Size: {self.size} | Entry: {self.entry_price} | "
            f"x{self.leverage} | PnL: {self.unrealised_pnl:.2f}"
        )


# ── Traders config loader ─────────────────────────────────────────────────────

def load_traders_config() -> dict:
    """Φορτώνει το traders.json από το repo."""
    try:
        with open("traders.json", "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load traders.json: {e}")
        return {"bybit": [], "binance": [], "okx": [], "hyperliquid": [], "blacklist": []}


# ── HTTP session helper ───────────────────────────────────────────────────────

def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── Platform fetchers ─────────────────────────────────────────────────────────

class BybitFetcher:
    def __init__(self):
        self._client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )

    async def get_positions(self, uid: str) -> list[Position]:
        try:
            resp = self._client.get_positions(category="linear", settleCoin="USDT")
            if resp.get("retCode") != 0:
                return []
            positions = []
            for item in resp.get("result", {}).get("list", []):
                size = float(item.get("size", 0))
                if size == 0:
                    continue
                positions.append(Position(
                    uid=uid,
                    symbol=item.get("symbol", ""),
                    side=item.get("side", "Buy"),
                    size=size,
                    entry_price=float(item.get("avgPrice", 0)),
                    leverage=int(float(item.get("leverage", 1))),
                    unrealised_pnl=float(item.get("unrealisedPnl", 0)),
                    created_time=datetime.fromtimestamp(int(item.get("createdTime", 0)) / 1000),
                    position_id=str(item.get("positionIdx", "")),
                    platform="bybit",
                ))
            return positions
        except Exception as e:
            logger.error(f"[Bybit] Error for {uid}: {e!r}")
            return []


class BinanceFetcher:
    BASE = "https://www.binance.com"

    async def get_positions(self, uid: str) -> list[Position]:
        """Fetches public positions of a Binance copy trader."""
        url = f"{self.BASE}/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position"
        payload = {"portfolioId": uid, "pageNumber": 1, "pageSize": 20}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; CopyBot/1.0)",
            "Referer": "https://www.binance.com/en/copy-trading",
        }
        try:
            connector = aiohttp.TCPConnector(ssl=_make_ssl_ctx())
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()

            positions = []
            for item in data.get("data", {}).get("list", []):
                size = float(item.get("positionAmount", 0))
                if size == 0:
                    continue
                side = "Buy" if item.get("positionSide", "LONG") in ("LONG", "BUY") else "Sell"
                positions.append(Position(
                    uid=uid,
                    symbol=item.get("symbol", "").replace("USDT", "USDT"),
                    side=side,
                    size=abs(size),
                    entry_price=float(item.get("entryPrice", 0)),
                    leverage=int(item.get("leverage", 1)),
                    unrealised_pnl=float(item.get("unrealizedProfit", 0)),
                    created_time=datetime.now(),
                    platform="binance",
                ))
            return positions
        except Exception as e:
            logger.error(f"[Binance] Error for {uid}: {e!r}")
            return []


class OKXFetcher:
    BASE = "https://www.okx.com"

    async def get_positions(self, uid: str) -> list[Position]:
        """Fetches public positions of an OKX copy trader."""
        url = f"{self.BASE}/api/v5/copytrading/public-position"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; CopyBot/1.0)",
        }
        try:
            connector = aiohttp.TCPConnector(ssl=_make_ssl_ctx())
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.get(
                    url,
                    params={"uniqueCode": uid},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()

            positions = []
            for item in data.get("data", [{}])[0].get("copyTradingPositions", []):
                size = float(item.get("pos", 0))
                if size == 0:
                    continue
                side = "Buy" if item.get("posSide", "long") == "long" else "Sell"
                positions.append(Position(
                    uid=uid,
                    symbol=item.get("instId", "").replace("-SWAP", "").replace("-", ""),
                    side=side,
                    size=abs(size),
                    entry_price=float(item.get("avgPx", 0)),
                    leverage=int(float(item.get("lever", 1))),
                    unrealised_pnl=float(item.get("upl", 0)),
                    created_time=datetime.fromtimestamp(int(item.get("cTime", 0)) / 1000),
                    platform="okx",
                ))
            return positions
        except Exception as e:
            logger.error(f"[OKX] Error for {uid}: {e!r}")
            return []


class HyperliquidFetcher:
    BASE = "https://api.hyperliquid.xyz"

    async def get_positions(self, address: str) -> list[Position]:
        """
        Fetches open positions of a Hyperliquid wallet address.
        100% on-chain public API — δουλεύει από οποιοδήποτε server.
        """
        url = f"{self.BASE}/info"
        payload = {"type": "clearinghouseState", "user": address}
        headers = {"Content-Type": "application/json"}
        try:
            connector = aiohttp.TCPConnector(ssl=_make_ssl_ctx())
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json()

            positions = []
            for item in data.get("assetPositions", []):
                pos = item.get("position", {})
                size = float(pos.get("szi", 0))
                if size == 0:
                    continue
                side = "Buy" if size > 0 else "Sell"
                leverage_info = pos.get("leverage", {})
                lev = int(leverage_info.get("value", 1)) if isinstance(leverage_info, dict) else 1
                positions.append(Position(
                    uid=address,
                    symbol=pos.get("coin", "") + "USDT",
                    side=side,
                    size=abs(size),
                    entry_price=float(pos.get("entryPx", 0)),
                    leverage=lev,
                    unrealised_pnl=float(pos.get("unrealizedPnl", 0)),
                    created_time=datetime.now(),
                    platform="hyperliquid",
                ))
            return positions
        except Exception as e:
            logger.error(f"[Hyperliquid] Error for {address}: {e!r}")
            return []


# ── Main multi-platform leaderboard ──────────────────────────────────────────

class BybitLeaderboard:
    """
    Multi-platform position tracker.
    Φορτώνει traders από traders.json και fetches θέσεις από όλες τις πλατφόρμες.
    """

    def __init__(self, period: str = "weekly", max_traders: int = 10):
        self._traders_cache: list[Trader] = []
        self._positions_cache: dict[str, list[Position]] = {}
        self._bybit = BybitFetcher()
        self._binance = BinanceFetcher()
        self._okx = OKXFetcher()
        self._hyperliquid = HyperliquidFetcher()
        logger.info("Multi-platform leaderboard initialized")

    async def get_top_traders(self, category: str = "linear") -> list[Trader]:
        """
        Επιστρέφει traders από το traders.json ως λίστα Trader objects.
        """
        config = load_traders_config()
        traders = []
        rank = 1

        platforms = {
            "bybit": "Bybit",
            "binance": "Binance",
            "okx": "OKX",
            "hyperliquid": "Hyperliquid",
        }

        for platform_key, platform_name in platforms.items():
            for uid in config.get(platform_key, []):
                traders.append(Trader(
                    uid=uid,
                    nickname=f"{platform_name}:{uid[:8]}...",
                    roi=0.0,
                    pnl=0.0,
                    win_rate=0.0,
                    followers=0,
                    rank=rank,
                    platform=platform_key,
                ))
                rank += 1

        if traders:
            self._traders_cache = traders
            logger.info(f"Loaded {len(traders)} traders from config")
        else:
            logger.warning("traders.json is empty — add trader UIDs to start copying")

        return traders

    async def get_trader_positions(self, uid: str) -> list[Position]:
        """Fetches positions για έναν trader από την αντίστοιχη πλατφόρμα."""
        # Βρες platform από cache
        trader = next((t for t in self._traders_cache if t.uid == uid), None)
        platform = trader.platform if trader else "bybit"

        # Φόρτωσε blacklist
        config = load_traders_config()
        blacklist = set(config.get("blacklist", []))

        try:
            if platform == "bybit":
                positions = await self._bybit.get_positions(uid)
            elif platform == "binance":
                positions = await self._binance.get_positions(uid)
            elif platform == "okx":
                positions = await self._okx.get_positions(uid)
            elif platform == "hyperliquid":
                positions = await self._hyperliquid.get_positions(uid)
            else:
                positions = []

            # Φιλτράρισμα blacklisted symbols
            filtered = [p for p in positions if p.symbol not in blacklist]
            if len(filtered) < len(positions):
                skipped = len(positions) - len(filtered)
                logger.info(f"Blacklist: skipped {skipped} positions for {uid}")

            self._positions_cache[uid] = filtered
            return filtered

        except Exception as e:
            logger.error(f"Error fetching positions for {uid}: {e!r}\n{traceback.format_exc()}")
            return self._positions_cache.get(uid, [])

    async def get_all_positions(self, trader_uids: list[str]) -> dict[str, list[Position]]:
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
        return positions_map

    async def close(self):
        pass

    def print_traders(self, traders: list[Trader]):
        print("\n" + "═" * 60)
        print(f"  🏆 TRACKED TRADERS ({len(traders)})")
        print("═" * 60)
        for t in traders:
            print(f"  {t}")
        print("═" * 60 + "\n")

    def print_positions(self, uid: str, positions: list[Position]):
        nickname = next((t.nickname for t in self._traders_cache if t.uid == uid), uid)
        print(f"\n  📊 {nickname} — Ανοιχτές θέσεις: {len(positions)}")
        for p in positions:
            emoji = "🟢" if p.direction == "LONG" else "🔴"
            print(f"    {emoji} {p}")
