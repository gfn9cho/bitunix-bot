import psycopg2
from psycopg2.extras import RealDictCursor
from modules.config import DB_CONFIG, MAX_DAILY_LOSS
from modules.logger_config import error_logger, logger
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
                    positionid TEXT NOT NULL,
                    type TEXT NOT NULL CHECK (type IN ('PROFIT', 'LOSS')),
                    date DATE NOT NULL,
                    pnl FLOAT NOT NULL,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    ctime TIMESTAMPTZ,
                    PRIMARY KEY (symbol, direction, type, date)
                );
            """)
            conn.commit()


# Add to loss_tracking.py:
def log_false_signal(symbol, direction, entry_price, interval, reason, signal_time):
    log_date = signal_time.strftime("%Y-%m-%d")
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO false_signals (symbol, direction, entry_price, interval, reason, signal_time, log_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (symbol, direction, entry_price, interval, reason, signal_time.isoformat(), log_date))
                conn.commit()
    except Exception as e:
        error_logger.error(f"[FALSE SIGNAL LOGGING ERROR] {e}")

# Make sure to run this in your database to create the table:
#
# CREATE TABLE false_signals (
#     id SERIAL PRIMARY KEY,
#     symbol TEXT NOT NULL,
#     direction TEXT NOT NULL,
#     entry_price NUMERIC NOT NULL,
#     interval TEXT NOT NULL,
#     reason TEXT NOT NULL,
#     signal_time TIMESTAMP NOT NULL,
#     log_date DATE NOT NULL
# );

def log_profit_loss(symbol, direction, positionid,  pnl, entry_type, ctime, date):
    if entry_type not in ("PROFIT", "LOSS"):
        raise ValueError("entry_type must be 'PROFIT' or 'LOSS'")
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO loss_tracking (symbol, direction, type, date, pnl, ctime)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, direction, positionid)
                    DO UPDATE SET
                        pnl = EXCLUDED.pnl,
                        timestamp = NOW(),
                        ctime = EXCLUDED.ctime
                """, (symbol, direction, positionid, entry_type, date, pnl, ctime))
                conn.commit()
            except Exception as insert_error:
                logger.info(f"[POSTGRES PNL CAPTURE]: {insert_error}")


def is_daily_loss_limit_exceeded():
    net = get_today_net_loss()
    return net <= MAX_DAILY_LOSS


def get_today_net_loss():
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(SUM(pnl), 0) AS net
                FROM loss_tracking
                WHERE DATE(timestamp) = CURRENT_DATE
            """)
            result = cur.fetchone()
            return result["net"] if result else 0.0


# Ensure table exists on import
ensure_loss_table()
