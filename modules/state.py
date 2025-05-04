# modules/state.py
import json
import os

#STATE_FILE = "/var/data/position_state.json"
STATE_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/position_state.json"

try:
    with open(STATE_FILE, "r") as f:
        position_state = json.load(f)
        # Convert filled_orders from list back to set
        for sym in position_state:
            if isinstance(position_state[sym].get("filled_orders"), list):
                position_state[sym]["filled_orders"] = set(position_state[sym]["filled_orders"])
except Exception:
    position_state = {}

def save_position_state():
    serializable_state = {
        sym: {
            **data,
            "filled_orders": list(data.get("filled_orders", []))
        } for sym, data in position_state.items()
    }
    with open(STATE_FILE, "w") as f:
        json.dump(serializable_state, f, indent=2)