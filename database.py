import asyncpg
import asyncio
from datetime import datetime
from config import DATABASE_URL


async def get_db():
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    """Δημιουργεί τους πίνακες αν δεν υπάρχουν"""
    conn = await get_db()
    try:
        # Πίνακας χρηστών
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id       BIGINT PRIMARY KEY,
                username      TEXT,
                is_subscribed BOOLEAN DEFAULT FALSE,
                sub_pending   BOOLEAN DEFAULT FALSE,
                paysafe_code  TEXT,
                joined_at     TIMESTAMP DEFAULT NOW()
            )
        """)

        # Πίνακας trades
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          SERIAL PRIMARY KEY,
                symbol      TEXT NOT NULL,
                side        TEXT NOT NULL,
                entry_price FLOAT,
                sl_price    FLOAT,
                tp_price    FLOAT,
                leverage    INT,
                usdt_amount FLOAT,
                result      TEXT,
                pnl_usdt    FLOAT,
                signal_score INT,
                source      TEXT,
                opened_at   TIMESTAMP DEFAULT NOW(),
                closed_at   TIMESTAMP
            )
        """)

        # Πίνακας pending trades (περιμένουν έγκριση 11-14)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_trades (
                id           SERIAL PRIMARY KEY,
                symbol       TEXT NOT NULL,
                side         TEXT NOT NULL,
                signal_score INT,
                source       TEXT,
                sentiment    TEXT,
                price_target FLOAT,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)

        print("✅ Database initialized!")
    finally:
        await conn.close()


# ─── USERS ────────────────────────────────────────────────

async def get_user(chat_id: int):
    conn = await get_db()
    try:
        return await conn.fetchrow("SELECT * FROM users WHERE chat_id=$1", chat_id)
    finally:
        await conn.close()


async def create_user(chat_id: int, username: str):
    conn = await get_db()
    try:
        await conn.execute("""
            INSERT INTO users (chat_id, username)
            VALUES ($1, $2)
            ON CONFLICT (chat_id) DO NOTHING
        """, chat_id, username)
    finally:
        await conn.close()


async def set_subscription_pending(chat_id: int, paysafe_code: str):
    conn = await get_db()
    try:
        await conn.execute("""
            UPDATE users SET sub_pending=TRUE, paysafe_code=$1
            WHERE chat_id=$2
        """, paysafe_code, chat_id)
    finally:
        await conn.close()


async def approve_subscription(chat_id: int):
    conn = await get_db()
    try:
        await conn.execute("""
            UPDATE users SET is_subscribed=TRUE, sub_pending=FALSE
            WHERE chat_id=$1
        """, chat_id)
    finally:
        await conn.close()


async def get_pending_subscriptions():
    conn = await get_db()
    try:
        return await conn.fetch("SELECT * FROM users WHERE sub_pending=TRUE")
    finally:
        await conn.close()


async def is_subscribed(chat_id: int) -> bool:
    user = await get_user(chat_id)
    return user and user["is_subscribed"]


# ─── TRADES ───────────────────────────────────────────────

async def save_trade(symbol, side, entry_price, sl_price, tp_price,
                     leverage, usdt_amount, signal_score, source):
    conn = await get_db()
    try:
        row = await conn.fetchrow("""
            INSERT INTO trades
              (symbol, side, entry_price, sl_price, tp_price,
               leverage, usdt_amount, signal_score, source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
        """, symbol, side, entry_price, sl_price, tp_price,
             leverage, usdt_amount, signal_score, source)
        return row["id"]
    finally:
        await conn.close()


async def close_trade(trade_id: int, result: str, pnl_usdt: float):
    conn = await get_db()
    try:
        await conn.execute("""
            UPDATE trades
            SET result=$1, pnl_usdt=$2, closed_at=NOW()
            WHERE id=$3
        """, result, pnl_usdt, trade_id)
    finally:
        await conn.close()


async def get_last_trades(limit: int = 20):
    conn = await get_db()
    try:
        return await conn.fetch("""
            SELECT * FROM trades
            WHERE result IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT $1
        """, limit)
    finally:
        await conn.close()


async def get_stats():
    conn = await get_db()
    try:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result IS NOT NULL")
        wins = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result='WIN'")
        losses = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result='LOSS'")
        total_pnl = await conn.fetchval(
            "SELECT COALESCE(SUM(pnl_usdt),0) FROM trades WHERE result IS NOT NULL")
        return {
            "total": total or 0,
            "wins": wins or 0,
            "losses": losses or 0,
            "total_pnl": round(total_pnl or 0, 2),
            "winrate": round((wins / total * 100) if total > 0 else 0, 1)
        }
    finally:
        await conn.close()


# ─── PENDING TRADES ───────────────────────────────────────

async def save_pending_trade(symbol, side, signal_score, source, sentiment, price_target):
    conn = await get_db()
    try:
        row = await conn.fetchrow("""
            INSERT INTO pending_trades
              (symbol, side, signal_score, source, sentiment, price_target)
            VALUES ($1,$2,$3,$4,$5,$6)
            RETURNING id
        """, symbol, side, signal_score, source, sentiment, price_target)
        return row["id"]
    finally:
        await conn.close()


async def get_pending_trade(trade_id: int):
    conn = await get_db()
    try:
        return await conn.fetchrow(
            "SELECT * FROM pending_trades WHERE id=$1", trade_id)
    finally:
        await conn.close()


async def delete_pending_trade(trade_id: int):
    conn = await get_db()
    try:
        await conn.execute(
            "DELETE FROM pending_trades WHERE id=$1", trade_id)
    finally:
        await conn.close()
