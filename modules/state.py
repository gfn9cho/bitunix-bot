# modules/state.py
import json
import os
from modules.logger_config import logger

# STATE_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/position_state.json"
STATE_FILE = "/var/data/bitunix-bot/position_state.json"

try:
    with open(STATE_FILE, "r") as f:
        position_state = json.load(f)
except Exception:
    position_state = {}

def save_position_state():
    with open(STATE_FILE, "w") as f:
        json.dump(position_state, f, indent=2)

def get_or_create_symbol_direction_state(symbol: str, direction: str):
    direction = direction.upper()  # Normalize direction
    logger.info(f"direction: {direction}")
    symbol_state = position_state.setdefault(symbol, {})
    logger.info(f"symbol_state: {symbol_state}")
    if direction not in symbol_state:
        symbol_state[direction] = {
            "position_id": None,
            "entry_price": 0.0,
            "total_qty": 0.0,
            "step": 0,
            "tps": []
        }
    return symbol_state[direction]
