import os

# API_KEY = os.getenv("API_KEY")
# API_SECRET = os.getenv("API_SECRET")
# BASE_URL = 'https://fapi.bitunix.com'
#
# POSITION_SIZE = 10  # dollars per entry
# LEVERAGE = 20
# LEVERAGE_MODE = "CROSS"
# MAX_DAILY_LOSS = -100  # Max loss in USD per day
#
# FAILED_ORDER_LOG_FILE = "/var/data/bitunix-bot/failed_orders.json"
# DB_CONFIG = {
#     "dbname": os.getenv("DB_NAME", ""),
#     "user": os.getenv("DB_USER", ""),
#     "password": os.getenv("DB_PASSWORD", ""),
#     "host": os.getenv("DB_HOST", ""),
#     "port": os.getenv("DB_PORT", 5432)
# }
#
# DEFAULT_STATE = {
#     "position_id": "",
#     "entry_price": 0.0,
#     "total_qty": 0.0,
#     "step": 0,
#     "tps": [],
#     "stop_loss": 0.0,
#     "qty_distribution": [0.7, 0.1, 0.1, 0.1],
#     "temporary": True
# }

API_KEY = "aa098df1e071273bed1ebe97e3ba4387"
API_SECRET = "c30b4a6f88792529288abddf9cf51528"
BASE_URL = 'https://fapi.bitunix.com'

POSITION_SIZE = 10  # dollars per entry
LEVERAGE = 20
LEVERAGE_MODE = "CROSS"
MAX_DAILY_LOSS = 100  # Max loss in USD per day

LOSS_LOG_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/daily_loss_log.json"
FAILED_ORDER_LOG_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/failed_orders.json"
# LOSS_LOG_FILE = "/var/data/daily_loss_log.json"

DB_CONFIG = {
    "dbname": os.getenv("DB_NAME", "order_state"),
    "user": os.getenv("DB_USER", "gfn9cho"),
    "password": os.getenv("DB_PASSWORD", "nL10KRch5XUWYjNmpLkHbY29DQM1N8V8"),
    "host": os.getenv("DB_HOST", "dpg-d0fnjpbe5dus73f3o500-a.oregon-postgres.render.com"),
    "port": os.getenv("DB_PORT", 5432)
}

DEFAULT_STATE = {
    "position_id": None,
    "entry_price": 0.0,
    "total_qty": 0.0,
    "step": 0,
    "tps": [],
    "stop_loss": 0.0,
    "qty_distribution": [0.7, 0.1, 0.1, 0.1],
    "temporary": True
}