import json
from modules.redis_client import redis as r
from modules.postgres_state_manager import get_or_create_symbol_direction_state as pg_get, \
    update_position_state as pg_update


# Initialize Redis client (adjust configuration as needed)
# r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)


def _redis_key(symbol: str, direction: str, position_id: str = "") -> str:
    base = f"position_state:{symbol}:{direction}"
    return f"{base}:{position_id}" if position_id else base


async def get_or_create_symbol_direction_state(symbol: str, direction: str, position_id: str = "") -> dict:
    key = _redis_key(symbol, direction)
    state_json = await r.get(key)
    if state_json:
        return json.loads(state_json)

    # Fallback to Postgres
    state = pg_get(symbol, direction, position_id)
    await r.set(key, json.dumps(state))
    return state


async def update_position_state(symbol: str, direction: str, position_id: str, updated_state: dict):
    key = _redis_key(symbol, direction)
    await r.set(key, json.dumps(updated_state))
    pg_update(symbol, direction, position_id, updated_state)


async def delete_position_state(symbol: str, direction: str, position_id: str):
    key = _redis_key(symbol, direction)
    await r.delete(key)
