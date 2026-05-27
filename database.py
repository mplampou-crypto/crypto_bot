"""
database.py
-----------
SQLite βάση δεδομένων για αποθήκευση:
  - Traders που ακολουθούμε
  - Trades που έχουμε εκτελέσει
  - P&L στατιστικά
"""

import sqlite3
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)
DB_PATH = "copybot.db"


@dataclass
class TradeRecord:
    id: int
    trader_uid: str
    trader_nickname: str
    symbol: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    status: str        # "open" | "closed"
    opened_at: str
    closed_at: str


class Database:

    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._init_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS traders (
                    uid         TEXT PRIMARY KEY,
                    nickname    TEXT NOT NULL,
                    followed_at TEXT NOT NULL,
                    active      INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    trader_uid       TEXT NOT NULL,
                    trader_nickname  TEXT NOT NULL,
                    symbol           TEXT NOT NULL,
                    side             TEXT NOT NULL,
                    size             REAL NOT NULL,
                    entry_price      REAL NOT NULL,
                    exit_price       REAL DEFAULT 0,
                    pnl              REAL DEFAULT 0,
                    order_id         TEXT DEFAULT '',
                    status           TEXT DEFAULT 'open',
                    opened_at        TEXT NOT NULL,
                    closed_at        TEXT DEFAULT ''
                );
            """)
        logger.info("✅ Database initialized")

    # ── Traders ──────────────────────────────────────────────────────────────

    def add_trader(self, uid: str, nickname: str):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO traders (uid, nickname, followed_at, active)
                VALUES (?, ?, ?, 1)
            """, (uid, nickname, datetime.now().isoformat()))
        logger.info(f"DB: + trader {nickname}")

    def remove_trader(self, uid: str):
        with self._conn() as conn:
            conn.execute("UPDATE traders SET active=0 WHERE uid=?", (uid,))

    def get_active_traders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM traders WHERE active=1"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Trades ───────────────────────────────────────────────────────────────

    def open_trade(
        self,
        trader_uid: str,
        trader_nickname: str,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        order_id: str = "",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO trades
                  (trader_uid, trader_nickname, symbol, side, size,
                   entry_price, order_id, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """, (trader_uid, trader_nickname, symbol, side, size,
                  entry_price, order_id, datetime.now().isoformat()))
            return cur.lastrowid

    def close_trade(
        self,
        trader_uid: str,
        symbol: str,
        side: str,
        exit_price: float,
        pnl: float,
    ):
        with self._conn() as conn:
            conn.execute("""
                UPDATE trades
                SET status='closed', exit_price=?, pnl=?, closed_at=?
                WHERE trader_uid=? AND symbol=? AND side=? AND status='open'
            """, (exit_price, pnl, datetime.now().isoformat(),
                  trader_uid, symbol, side))

    def get_open_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_trades(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Στατιστικά ───────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)                          AS total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
                    ROUND(SUM(pnl), 2)                AS total_pnl,
                    ROUND(AVG(pnl), 2)                AS avg_pnl
                FROM trades WHERE status='closed'
            """).fetchone()

        total = row["total_trades"] or 0
        wins  = row["wins"] or 0
        return {
            "total_trades": total,
            "wins":         wins,
            "losses":       row["losses"] or 0,
            "win_rate":     round(wins / total * 100, 1) if total > 0 else 0,
            "total_pnl":    row["total_pnl"] or 0,
            "avg_pnl":      row["avg_pnl"] or 0,
        }
