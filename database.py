import asyncpg
from datetime import datetime, timezone, timedelta
from config import DATABASE_URL, SUBSCRIPTION_DAYS


async def get_db():
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    conn = await get_db()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id        BIGINT PRIMARY KEY,
                username       TEXT,
                is_subscribed  BOOLEAN DEFAULT FALSE,
                sub_pending    BOOLEAN DEFAULT FALSE,
                paysafe_code   TEXT,
                sub_expires_at TIMESTAMP WITH TIME ZONE,
                joined_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id             SERIAL PRIMARY KEY,
                symbol         TEXT NOT NULL,
                side           TEXT NOT NULL,
                entry_price    FLOAT,
                sl_price       FLOAT,
                tp_price       FLOAT,
                current_sl     FLOAT,
                leverage       INT,
                usdt_amount    FLOAT,
                qty            FLOAT,
                order_id       TEXT,
                result         TEXT,
                pnl_usdt       FLOAT,
                signal_score   INT,
                source         TEXT,
                is_breakeven   BOOLEAN DEFAULT FALSE,
                opened_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                closed_at      TIMESTAMP WITH TIME ZONE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_trades (
                id           SERIAL PRIMARY KEY,
                symbol       TEXT NOT NULL,
                side         TEXT NOT NULL,
                signal_score INT,
                source       TEXT,
                sentiment    TEXT,
                price_target FLOAT,
                created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rejected_signals (
                id           SERIAL PRIMARY KEY,
                symbol       TEXT,
                side         TEXT,
                score        INT,
                reason       TEXT,
                created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date               DATE PRIMARY KEY DEFAULT CURRENT_DATE,
                trades_count       INT DEFAULT 0,
                wins               INT DEFAULT 0,
                losses             INT DEFAULT 0,
                pnl_usdt           FLOAT DEFAULT 0,
                consecutive_losses INT DEFAULT 0
            )
        """)

        # ─── MIGRATIONS ───
        migrations = [
            # ✅ FIX: TIMESTAMP WITH TIME ZONE για να αποφύγουμε timezone errors
            "ALTER TABLE users ALTER COLUMN sub_expires_at TYPE TIMESTAMP WITH TIME ZONE USING sub_expires_at AT TIME ZONE 'UTC'",
            "ALTER TABLE users ALTER COLUMN joined_at     TYPE TIMESTAMP WITH TIME ZONE USING joined_at     AT TIME ZONE 'UTC'",
            "ALTER TABLE trades ALTER COLUMN opened_at    TYPE TIMESTAMP WITH TIME ZONE USING opened_at     AT TIME ZONE 'UTC'",
            "ALTER TABLE trades ALTER COLUMN closed_at    TYPE TIMESTAMP WITH TIME ZONE USING closed_at     AT TIME ZONE 'UTC'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS sub_pending    BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS paysafe_code   TEXT",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS current_sl    FLOAT",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS qty           FLOAT",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS order_id      TEXT",
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS is_breakeven  BOOLEAN DEFAULT FALSE",
        ]
        for migration in migrations:
            try:
                await conn.execute(migration)
            except Exception as e:
                print(f"Migration note: {e}")

        print("✅ Database initialized!")
    finally:
        await conn.close()


# ─── USERS ────────────────────────────────────────────────

async def get_user(chat_id: int):
    conn = await get_db()
    try:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE chat_id=$1", chat_id)
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
    """
    Εγκρίνει συνδρομή για 30 μέρες.
    ✅ FIX: χρησιμοποιεί NOW() της DB για να αποφύγει timezone conflicts.
    """
    conn = await get_db()
    try:
        await conn.execute("""
            UPDATE users
            SET is_subscribed  = TRUE,
                sub_pending    = FALSE,
                sub_expires_at = NOW() + ($1 || ' days')::interval
            WHERE chat_id = $2
        """, str(SUBSCRIPTION_DAYS), chat_id)
    finally:
        await conn.close()


async def deactivate_subscription(chat_id: int):
    """Απενεργοποιεί συνδρομή (λήξη ή manual)"""
    conn = await get_db()
    try:
        await conn.execute("""
            UPDATE users
            SET is_subscribed = FALSE
            WHERE chat_id = $1
        """, chat_id)
    finally:
        await conn.close()


async def get_pending_subscriptions():
    conn = await get_db()
    try:
        return await conn.fetch(
            "SELECT * FROM users WHERE sub_pending=TRUE")
    finally:
        await conn.close()


async def is_subscribed(chat_id: int) -> bool:
    """
    Ελέγχει αν ο χρήστης έχει ενεργή συνδρομή.
    ✅ FIX: σύγκριση γίνεται μέσα στη DB για να αποφύγουμε timezone errors.
    """
    conn = await get_db()
    try:
        row = await conn.fetchrow("""
            SELECT is_subscribed, sub_expires_at
            FROM users
            WHERE chat_id = $1
        """, chat_id)

        if not row or not row["is_subscribed"]:
            return False

        # Αν δεν υπάρχει expiry → θεωρούμε ενεργή
        if row["sub_expires_at"] is None:
            return True

        # ✅ FIX: σύγκριση μέσα στη DB με NOW()
        expired = await conn.fetchval("""
            SELECT sub_expires_at < NOW()
            FROM users
            WHERE chat_id = $1
        """, chat_id)

        if expired:
            # Απενεργοποίηση αυτόματα
            await conn.execute("""
                UPDATE users SET is_subscribed = FALSE
                WHERE chat_id = $1
            """, chat_id)
            return False

        return True
    finally:
        await conn.close()


async def get_sub_expiry(chat_id: int):
    """Επιστρέφει την ημερομηνία λήξης συνδρομής"""
    user = await get_user(chat_id)
    return user["sub_expires_at"] if user else None


async def get_expiring_subs(days_ahead: int = 1):
    """
    Επιστρέφει users που λήγουν σε X μέρες.
    ✅ FIX: χρησιμοποιεί NOW() της DB για να αποφύγει timezone errors.
    Default: 1 μέρα (στέλνει reminder μία μέρα πριν)
    """
    conn = await get_db()
    try:
        return await conn.fetch("""
            SELECT * FROM users
            WHERE is_subscribed = TRUE
            AND sub_expires_at IS NOT NULL
            AND sub_expires_at <= NOW() + ($1 || ' days')::interval
            AND sub_expires_at > NOW()
        """, str(days_ahead))
    finally:
        await conn.close()



# ─── TRADES ───────────────────────────────────────────────

async def save_trade(symbol, side, entry_price, sl_price, tp_price,
                     leverage, usdt_amount, qty, order_id, signal_score, source):
    conn = await get_db()
    try:
        row = await conn.fetchrow("""
            INSERT INTO trades
              (symbol, side, entry_price, sl_price, tp_price,
               current_sl, leverage, usdt_amount, qty, order_id,
               signal_score, source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            RETURNING id
        """, symbol, side, entry_price, sl_price, tp_price,
             sl_price, leverage, usdt_amount, qty, order_id,
             signal_score, source)
        return row["id"]
    finally:
        await conn.close()


async def update_trade_sl(trade_id: int, new_sl: float, is_breakeven: bool = False):
    conn = await get_db()
    try:
        await conn.execute("""
            UPDATE trades SET current_sl=$1, is_breakeven=$2
            WHERE id=$3
        """, new_sl, is_breakeven, trade_id)
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

        # Update daily stats
        await conn.execute("""
            INSERT INTO daily_stats
                (date, trades_count, wins, losses, pnl_usdt, consecutive_losses)
            VALUES (CURRENT_DATE, 1,
                CASE WHEN $1='WIN'  THEN 1 ELSE 0 END,
                CASE WHEN $1='LOSS' THEN 1 ELSE 0 END,
                $2, 0)
            ON CONFLICT (date) DO UPDATE SET
                trades_count = daily_stats.trades_count + 1,
                wins   = daily_stats.wins   + CASE WHEN $1='WIN'  THEN 1 ELSE 0 END,
                losses = daily_stats.losses + CASE WHEN $1='LOSS' THEN 1 ELSE 0 END,
                pnl_usdt = daily_stats.pnl_usdt + $2,
                consecutive_losses = CASE
                    WHEN $1='LOSS' THEN daily_stats.consecutive_losses + 1
                    ELSE 0
                END
        """, result, pnl_usdt)
    finally:
        await conn.close()


async def get_open_trades():
    conn = await get_db()
    try:
        return await conn.fetch("""
            SELECT * FROM trades
            WHERE result IS NULL
            ORDER BY opened_at DESC
        """)
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
        total      = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result IS NOT NULL") or 0
        wins       = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result='WIN'") or 0
        losses     = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result='LOSS'") or 0
        breakevens = await conn.fetchval(
            "SELECT COUNT(*) FROM trades WHERE result='BREAKEVEN'") or 0
        total_pnl  = await conn.fetchval(
            "SELECT COALESCE(SUM(pnl_usdt),0) FROM trades WHERE result IS NOT NULL") or 0

        today = await conn.fetchrow(
            "SELECT * FROM daily_stats WHERE date=CURRENT_DATE")

        return {
            "total":       total,
            "wins":        wins,
            "losses":      losses,
            "breakevens":  breakevens,
            "total_pnl":   round(total_pnl, 2),
            "winrate":     round((wins / total * 100) if total > 0 else 0, 1),
            "today_trades": today["trades_count"]       if today else 0,
            "today_pnl":    round(today["pnl_usdt"], 2) if today else 0,
            "consecutive_losses": today["consecutive_losses"] if today else 0,
        }
    finally:
        await conn.close()


async def get_today_trades_count() -> int:
    conn = await get_db()
    try:
        row = await conn.fetchrow(
            "SELECT trades_count FROM daily_stats WHERE date=CURRENT_DATE")
        return row["trades_count"] if row else 0
    finally:
        await conn.close()


async def get_consecutive_losses() -> int:
    conn = await get_db()
    try:
        row = await conn.fetchrow(
            "SELECT consecutive_losses FROM daily_stats WHERE date=CURRENT_DATE")
        return row["consecutive_losses"] if row else 0
    finally:
        await conn.close()


# ─── REJECTED SIGNALS ─────────────────────────────────────

async def save_rejected_signal(symbol: str, side: str, score: int, reason: str):
    conn = await get_db()
    try:
        await conn.execute("""
            INSERT INTO rejected_signals (symbol, side, score, reason)
            VALUES ($1,$2,$3,$4)
        """, symbol, side, score, reason)
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
