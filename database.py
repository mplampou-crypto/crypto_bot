"""SQLite storage: per-strategy position state + closed-trade history."""
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = "trades.db"
_lock = threading.Lock()
_conn = None


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    global _conn
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_positions (
            strategy    TEXT PRIMARY KEY,
            symbol      TEXT,
            side        TEXT,            -- 'long' | 'short' | 'flat'
            qty         REAL,
            entry_price REAL,
            entry_time  TEXT
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy    TEXT,
            symbol      TEXT,
            side        TEXT,            -- 'long' | 'short'
            qty         REAL,
            entry_price REAL,
            exit_price  REAL,
            pnl         REAL,            -- net of fees, in USDT
            pnl_pct     REAL,
            win         INTEGER,         -- 1 win, 0 loss
            entry_time  TEXT,
            exit_time   TEXT
        )
    """)
    _conn.commit()


# ── strategy position state ─────────────────────────────────────
def get_strategy_position(strategy):
    cur = _conn.execute(
        "SELECT * FROM strategy_positions WHERE strategy=?", (strategy,))
    row = cur.fetchone()
    return dict(row) if row else None


def set_strategy_position(strategy, symbol, side, qty, entry_price, entry_time):
    with _lock:
        _conn.execute("""
            INSERT INTO strategy_positions
                (strategy, symbol, side, qty, entry_price, entry_time)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(strategy) DO UPDATE SET
                symbol=excluded.symbol, side=excluded.side, qty=excluded.qty,
                entry_price=excluded.entry_price, entry_time=excluded.entry_time
        """, (strategy, symbol, side, qty, entry_price, entry_time))
        _conn.commit()


def get_open_positions_for_symbol(symbol):
    """Return [{strategy, side, qty}] for all non-flat strategies on a symbol."""
    cur = _conn.execute(
        "SELECT strategy, side, qty FROM strategy_positions "
        "WHERE symbol=? AND side != 'flat'", (symbol,))
    return [dict(r) for r in cur.fetchall()]


def get_all_strategy_positions():
    cur = _conn.execute("SELECT * FROM strategy_positions")
    return [dict(r) for r in cur.fetchall()]


# ── closed trades ───────────────────────────────────────────────
def insert_trade(strategy, symbol, side, qty, entry_price, exit_price,
                 pnl, pnl_pct, win, entry_time):
    with _lock:
        _conn.execute("""
            INSERT INTO trades
                (strategy, symbol, side, qty, entry_price, exit_price,
                 pnl, pnl_pct, win, entry_time, exit_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (strategy, symbol, side, qty, entry_price, exit_price,
              pnl, pnl_pct, win, entry_time, _now()))
        _conn.commit()


def get_recent_trades(limit=25):
    cur = _conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]


def get_stats():
    """Overall + per-strategy aggregates."""
    o = _conn.execute("""
        SELECT COUNT(*) n,
               COALESCE(SUM(win),0) wins,
               COALESCE(SUM(pnl),0) pnl
        FROM trades
    """).fetchone()
    total = o["n"]
    overall = {
        "trades": total,
        "wins": o["wins"],
        "losses": total - o["wins"],
        "win_rate": round(o["wins"] / total * 100, 1) if total else 0.0,
        "pnl": round(o["pnl"], 2),
    }

    per = {}
    cur = _conn.execute("""
        SELECT strategy,
               COUNT(*) n,
               COALESCE(SUM(win),0) wins,
               COALESCE(SUM(pnl),0) pnl
        FROM trades GROUP BY strategy
    """)
    for r in cur.fetchall():
        n = r["n"]
        per[r["strategy"]] = {
            "trades": n,
            "wins": r["wins"],
            "losses": n - r["wins"],
            "win_rate": round(r["wins"] / n * 100, 1) if n else 0.0,
            "pnl": round(r["pnl"], 2),
        }
    return overall, per
