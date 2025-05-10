import psycopg2
from psycopg2.extras import RealDictCursor
from config import DB_CONFIG, DEFAULT_STATE



def get_db_conn():
    return psycopg2.connect(**DB_CONFIG)


def ensure_table():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS position_state (
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
                    position_id TEXT,
                    entry_price FLOAT,
                    total_qty FLOAT,
                    step INTEGER,
                    tps FLOAT[],
                    stop_loss FLOAT,
                    qty_distribution FLOAT[],
                    PRIMARY KEY (symbol, direction)
                );
            """)
            conn.commit()


def get_or_create_symbol_direction_state(symbol, direction):
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM position_state WHERE symbol = %s AND direction = %s
            """, (symbol, direction))
            row = cur.fetchone()

            if row:
                return dict(row)
            else:
                # Insert default
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
                return DEFAULT_STATE.copy()


def update_position_state(symbol, direction, updated_fields: dict):
    if not updated_fields:
        return

    columns = list(updated_fields.keys())
    values = [updated_fields[col] for col in columns]

    set_clause = ", ".join([f"{col} = %s" for col in columns])

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE position_state SET {set_clause}
                WHERE symbol = %s AND direction = %s
            """, values + [symbol, direction])
            conn.commit()


def delete_position_state(symbol, direction):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM position_state WHERE symbol = %s AND direction = %s
            """, (symbol, direction))
            conn.commit()

# Ensure table exists at import
ensure_table()
