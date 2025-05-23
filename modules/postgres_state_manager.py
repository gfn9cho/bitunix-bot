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
                    status TEXT,
                    UNIQUE (symbol, direction, position_id )
                );
            """)
            conn.commit()


# Note: This assumes only one open position per (symbol, direction, temporary).
# Temporary false signal state is inserted with position_id = None and cleaned separately.

def get_or_create_symbol_direction_state(symbol, direction, position_id=''):
    logger.info(f"[DB] Fetching state for {symbol} {direction} {position_id} ")

    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Try to fetch an OPEN position
            cur.execute("""
                SELECT * FROM position_state
                WHERE symbol = %s AND direction = %s AND 
                ((status = 'OPEN' AND position_id != '') OR (status = 'PENDING' AND position_id = ''))
            """, (symbol, direction))
            row = cur.fetchone()

            if row:
                logger.info(f"[DB] Found OPEN state for {symbol} {direction}")
                return dict(row)

            # If a new signal creates a state, set status as PENDING until confirmed by WS
            logger.info(f"[DB] Creating new PENDING state for {symbol} {direction}")
            cur.execute("""
                INSERT INTO position_state (symbol, direction, position_id, entry_price, total_qty, step, tps, stop_loss, qty_distribution, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                symbol,
                direction,
                position_id,
                DEFAULT_STATE["entry_price"],
                DEFAULT_STATE["total_qty"],
                DEFAULT_STATE["step"],
                DEFAULT_STATE["tps"],
                DEFAULT_STATE["stop_loss"],
                DEFAULT_STATE["qty_distribution"],
                "PENDING"  # status field
            ))
            conn.commit()

            cur.execute("""
                SELECT * FROM position_state
                WHERE symbol = %s AND direction = %s AND status = 'PENDING'
            """, (symbol, direction))
            return dict(cur.fetchone())


def update_position_state(symbol, direction, position_id, updated_fields: dict):
    if not updated_fields:
        return

    # Allow position_id to be updated only if it's provided in the updated_fields explicitly
    include_position_id = "position_id" in updated_fields and updated_fields["position_id"]
    columns = [col for col in updated_fields.keys() if col not in ("symbol", "direction", "sl_order_id", "tp_orders")]
    values = [updated_fields[col] for col in columns]

    if not columns:
        logger.warning(f"[DB] No valid fields to update for {symbol} {direction} {position_id}")
        return

    set_clause = ", ".join([f"{col} = %s" for col in columns])
    logger.info(f"[DB] Updating state for {symbol} {direction} {position_id} with fields: {updated_fields}")

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            if position_id == '':
                # This is before position ID is known. Update using PENDING row.
                cur.execute(f"""
                    UPDATE position_state
                    SET {set_clause}
                    WHERE symbol = %s AND direction = %s AND status = 'PENDING'
                """, values + [symbol, direction])
            else:
                if include_position_id:
                    # This is the first websocket call, setting position_id into a PENDING row
                    cur.execute(f"""
                        UPDATE position_state
                        SET {set_clause}
                        WHERE symbol = %s AND direction = %s AND position_id = ''
                    """, values + [symbol, direction])
                else:
                    # Normal update once position_id is already known
                    cur.execute(f"""
                        UPDATE position_state
                        SET {set_clause}
                        WHERE symbol = %s AND direction = %s AND position_id = %s
                    """, values + [symbol, direction, position_id])

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
