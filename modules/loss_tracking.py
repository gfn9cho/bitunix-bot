import psycopg2
from psycopg2.extras import RealDictCursor
from modules.config import DB_CONFIG, MAX_DAILY_LOSS
import os


def get_db_conn():
    return psycopg2.connect(**DB_CONFIG)


def ensure_loss_table():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS loss_tracking (
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
                    type TEXT NOT NULL CHECK (type IN ('PROFIT', 'LOSS')),
                    date DATE NOT NULL,
                    pnl FLOAT NOT NULL,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    ctime TIMESTAMPTZ,
                    PRIMARY KEY (symbol, direction, type, date)
                );
            """)
            conn.commit()


def log_profit_loss(symbol, direction, pnl, entry_type, ctime, date):
    if entry_type not in ("PROFIT", "LOSS"):
        raise ValueError("entry_type must be 'PROFIT' or 'LOSS'")
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO loss_tracking (symbol, direction, type, date, pnl, ctime)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, direction, type, date)
                DO UPDATE SET
                    pnl = loss_tracking.pnl + EXCLUDED.pnl,
                    timestamp = NOW(),
                    ctime = EXCLUDED.ctime
            """, (symbol, direction, entry_type, date, pnl, ctime))
            conn.commit()


def is_daily_loss_limit_exceeded():
    net = get_today_net_loss()
    return net <= MAX_DAILY_LOSS


def get_today_net_loss():
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(SUM(
                    CASE WHEN type = 'LOSS' THEN -pnl ELSE pnl END
                ), 0) AS net
                FROM loss_tracking
                WHERE DATE(timestamp) = CURRENT_DATE
            """)
            result = cur.fetchone()
            return result["net"] if result else 0.0


# Ensure table exists on import
ensure_loss_table()
