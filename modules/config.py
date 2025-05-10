import os

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BASE_URL = 'https://fapi.bitunix.com'

POSITION_SIZE = 10  # dollars per entry
LEVERAGE = 20
LEVERAGE_MODE = "CROSS"
MAX_DAILY_LOSS = 100  # Max loss in USD per day

#LOSS_LOG_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/daily_loss_log.json"
#FAILED_ORDER_LOG_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/failed_orders.json"
LOSS_LOG_FILE = "/var/data/daily_loss_log.json"
FAILED_ORDER_LOG_FILE = "/Users/prabha/IdeaProjects/bitunix-bot/failed_orders.json"