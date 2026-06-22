"""Leaderboard: SQLite storage of per-trader positions + closed trades,
stats computation, and Telegram-formatted output."""
import sqlite3
import threading
from datetime import datetime, timezone

import config

_lock = threading.Lock()
_conn = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            trader      TEXT PRIMARY KEY,
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
            trader      TEXT,
            symbol      TEXT,
            side        TEXT,            -- 'long' | 'short'
            qty         REAL,
            entry_price REAL,
            exit_price  REAL,
            pnl         REAL,            -- net of fees (USDT)
            pnl_pct     REAL,
            win         INTEGER,
            entry_time  TEXT,
            exit_time   TEXT
        )
    """)
    _conn.commit()


# ── position state ──────────────────────────────────────────────
def get_position(trader):
    row = _conn.execute(
        "SELECT * FROM positions WHERE trader=?", (trader,)).fetchone()
    return dict(row) if row else None


def set_position(trader, symbol, side, qty, entry_price, entry_time):
    with _lock:
        _conn.execute("""
            INSERT INTO positions (trader, symbol, side, qty, entry_price, entry_time)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(trader) DO UPDATE SET
                symbol=excluded.symbol, side=excluded.side, qty=excluded.qty,
                entry_price=excluded.entry_price, entry_time=excluded.entry_time
        """, (trader, symbol, side, qty, entry_price, entry_time))
        _conn.commit()


def open_positions_for_symbol(symbol):
    cur = _conn.execute(
        "SELECT trader, side, qty FROM positions "
        "WHERE symbol=? AND side != 'flat'", (symbol,))
    return [dict(r) for r in cur.fetchall()]


def all_positions():
    return [dict(r) for r in _conn.execute("SELECT * FROM positions").fetchall()]


# ── closed trades ───────────────────────────────────────────────
def record_trade(trader, symbol, side, qty, entry_price, exit_price,
                 pnl, pnl_pct, win, entry_time):
    with _lock:
        _conn.execute("""
            INSERT INTO trades (trader, symbol, side, qty, entry_price,
                exit_price, pnl, pnl_pct, win, entry_time, exit_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (trader, symbol, side, qty, entry_price, exit_price,
              pnl, pnl_pct, win, entry_time, now_iso()))
        _conn.commit()


def recent_trades(limit=15):
    cur = _conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in cur.fetchall()]


# ── stats ───────────────────────────────────────────────────────
def overall_stats():
    o = _conn.execute("""
        SELECT COUNT(*) n, COALESCE(SUM(win),0) wins, COALESCE(SUM(pnl),0) pnl
        FROM trades
    """).fetchone()
    n = o["n"]
    return {
        "trades": n, "wins": o["wins"], "losses": n - o["wins"],
        "win_rate": round(o["wins"] / n * 100, 1) if n else 0.0,
        "pnl": round(o["pnl"], 2),
    }


def per_trader_stats():
    out = {}
    cur = _conn.execute("""
        SELECT trader, COUNT(*) n, COALESCE(SUM(win),0) wins,
               COALESCE(SUM(pnl),0) pnl
        FROM trades GROUP BY trader
    """)
    for r in cur.fetchall():
        n = r["n"]
        out[r["trader"]] = {
            "trades": n, "wins": r["wins"], "losses": n - r["wins"],
            "win_rate": round(r["wins"] / n * 100, 1) if n else 0.0,
            "pnl": round(r["pnl"], 2),
        }
    return out


# ── Telegram-formatted text ─────────────────────────────────────
def format_stats():
    o = overall_stats()
    sign = "+" if o["pnl"] >= 0 else ""
    return (
        "<b>📊 Stats</b>\n"
        f"Trades: {o['trades']}  |  W/L: {o['wins']}/{o['losses']}\n"
        f"Win rate: <b>{o['win_rate']}%</b>\n"
        f"PnL: <b>{sign}{o['pnl']} USDT</b>"
    )


def format_leaderboard():
    per = per_trader_stats()
    if not per:
        return "<b>🏆 Leaderboard</b>\nΚανένα κλεισμένο trade ακόμη."
    rows = sorted(per.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
    lines = ["<b>🏆 Leaderboard</b>", "<pre>name        win%   pnl     n"]
    for name, s in rows:
        sign = "+" if s["pnl"] >= 0 else ""
        lines.append(
            f"{name[:11]:<11} {s['win_rate']:>4}%  "
            f"{sign}{s['pnl']:<6} {s['trades']}")
    lines.append("</pre>")
    return "\n".join(lines)


def format_traders():
    lines = ["<b>🤖 Traders</b>"]
    pos = {p["trader"]: p for p in all_positions()}
    for name, cfg in config.TRADERS.items():
        state = "🟢" if cfg.get("enabled") else "⚪"
        p = pos.get(name)
        live = ""
        if p and p["side"] != "flat":
            live = f" — {p['side'].upper()} @ {p['entry_price']}"
        lines.append(
            f"{state} <b>{name}</b> ({cfg.get('symbol')}, "
            f"{cfg.get('size_usdt')} USDT){live}")
    return "\n".join(lines)
