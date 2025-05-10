import os

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BASE_URL = 'https://fapi.bitunix.com'

POSITION_SIZE = 10  # dollars per entry
LEVERAGE = 20
LEVERAGE_MODE = "CROSS"
MAX_DAILY_LOSS = -100  # Max loss in USD per day

FAILED_ORDER_LOG_FILE = "/var/data/bitunix-bot/failed_orders.json"
DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", ""),
    "user": os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
    "host": os.getenv("DB_HOST", ""),
    "port": os.getenv("DB_PORT", 5432)
}

DEFAULT_STATE = {
    "position_id": None,
    "entry_price": 0.0,
    "total_qty": 0.0,
    "step": 0,
    "tps": [],
    "stop_loss": 0.0,
    "qty_distribution": [0.7, 0.1, 0.1, 0.1]
}