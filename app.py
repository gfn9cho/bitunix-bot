
from flask import Flask
import threading
from modules.webhook_handler import webhook_handler
from modules.logger_config import logger, trade_logger, error_logger, reversal_logger
from modules.config import API_KEY, API_SECRET, BASE_URL, POSITION_SIZE, LEVERAGE, MAX_DAILY_LOSS
from modules.utils import parse_signal, calculate_zone_entries, calculate_quantities, update_loss, get_today_loss
from modules.websocket_handler import start_websocket_listener

app = Flask(__name__)

@app.route('/webhook/<symbol>', methods=['POST'])
def webhook(symbol):
    return webhook_handler(symbol)

if __name__ == '__main__':
    ws_thread = threading.Thread(target=start_websocket_listener, daemon=True)
    ws_thread.start()
    app.run(host='0.0.0.0', port=5000)
