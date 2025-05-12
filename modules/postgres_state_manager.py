import psycopg2
from psycopg2.extras import RealDictCursor
from modules.config import DB_CONFIG, DEFAULT_STATE
from modules.logger_config import logger


def get_db_conn():
    return psycopg2.connect(**DB_CONFIG)


def ensure_table():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            logger.info("Ensuring position_state table exists")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_state (
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
                    position_id TEXT NULL,
                    entry_price FLOAT,
                    total_qty FLOAT,
                    step INTEGER,
                    tps FLOAT[],
                    stop_loss FLOAT,
                    qty_distribution FLOAT[],
                    UNIQUE (symbol, direction, position_id )
                );
            """)
            conn.commit()


# Note: This assumes only one open position per (symbol, direction, temporary).
# Temporary false signal state is inserted with position_id = None and cleaned separately.


def get_or_create_symbol_direction_state(symbol, direction, position_id=None):
    logger.info(f"[DB] Fetching state for {symbol} {direction} {position_id} ")
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if position_id:
                cur.execute("""
                    UPDATE position_state
                    SET position_id = %s
                    WHERE symbol = %s AND direction = %s AND position_id='' 
                """, (position_id, symbol, direction))
                cur.execute(f"""
                                SELECT * FROM position_state WHERE symbol = %s AND direction = %s AND position_id = %s
                """, (symbol, direction, position_id))
                row = cur.fetchone()
            else:
                cur.execute(f"""
                                SELECT * FROM position_state WHERE symbol = %s AND direction = %s AND position_id=''
                                """, (symbol, direction))
                row = cur.fetchone()

            if row:
                logger.info(f"[DB] Found existing state for {symbol} {direction} {position_id}")
                return dict(row)
            else:
                logger.info(f"[DB] Creating new state for {symbol} {direction} {position_id}")
                cur.execute("""
                    INSERT INTO position_state (symbol, direction, position_id, entry_price, total_qty, step, tps, stop_loss, qty_distribution)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    symbol,
                    direction,
                    DEFAULT_STATE["position_id"],
                    DEFAULT_STATE["entry_price"],
                    DEFAULT_STATE["total_qty"],
                    DEFAULT_STATE["step"],
                    DEFAULT_STATE["tps"],
                    DEFAULT_STATE["stop_loss"],
                    DEFAULT_STATE["qty_distribution"]
                ))
                conn.commit()
                # Fetch and return the inserted row
                cur.execute("""
                    SELECT * FROM position_state WHERE symbol = %s AND direction = %s AND position_id=''
                """, (symbol, direction))
                row = cur.fetchone()
                return dict(row)


def update_position_state(symbol, direction, position_id, updated_fields: dict):
    if not updated_fields:
        return
    # Remove symbol and direction if mistakenly included
    # Only include fields that are explicitly updated to avoid overwriting with defaults
    columns = [col for col in updated_fields.keys() if col not in ("symbol", "direction", "position_id")]
    values = [updated_fields[col] for col in columns]
    if not columns:
        logger.warning(f"[DB] No valid fields to update for {symbol} {direction} {position_id}")
        return
    placeholders = ", ".join(["%s"] * len(values))
    set_clause = ", ".join([f"{col} = EXCLUDED.{col}" for col in columns])

    logger.info(f"[DB] Updating state for {symbol} {direction} {position_id} with fields: {updated_fields}")

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            if position_id is not None:
                cur.execute(f"""
                        INSERT INTO position_state (symbol, direction, position_id,  {', '.join(columns)})
                        VALUES (%s, %s, %s, {placeholders})
                        ON CONFLICT (symbol, direction, position_id ) DO UPDATE SET {set_clause}
                """, [symbol, direction, position_id] + values)
            if position_id is None:
                cur.execute(f"""
                        INSERT INTO position_state (symbol, direction, position_id, {', '.join(columns)})
                        VALUES (%s, %s %s, {placeholders})
                        ON CONFLICT (symbol, direction, position_id ) DO UPDATE SET {set_clause}
                    """, [symbol, direction, position_id] + values)

            conn.commit()


def delete_position_state(symbol, direction, position_id=None):
    logger.info(f"[DB] Deleting state for {symbol} {direction} {position_id}")
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            if position_id:
                cur.execute("""
                    DELETE FROM position_state WHERE symbol = %s AND direction = %s AND position_id = %s
                """, (symbol, direction, position_id))
            else:
                cur.execute("""
                    DELETE FROM position_state WHERE symbol = %s AND direction = %s AND position_id=''
                """, (symbol, direction))
            conn.commit()


# Ensure table exists at import
ensure_table()
