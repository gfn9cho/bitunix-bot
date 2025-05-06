
from flask import Flask, jsonify
import threading
from modules.webhook_handler import webhook_handler
from modules.logger_config import logger, trade_logger, error_logger, reversal_logger
from modules.config import API_KEY, API_SECRET, BASE_URL, POSITION_SIZE, LEVERAGE, MAX_DAILY_LOSS
from modules.utils import parse_signal, calculate_zone_entries, calculate_quantities, update_loss, get_today_loss
from flask import request, jsonify
from modules.websocket_handler import handle_tp_sl
from threading import Thread
import os
import hmac
import hashlib
import json
import random
import time
import base64
import secrets
import asyncio
import threading
from modules.websocket_handler import start_websocket_listener

app = Flask(__name__)



def start_ws_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_websocket_listener())

threading.Thread(target=start_ws_thread, daemon=True).start()


@app.route("/debug-signature", methods=["GET"])
def debug_signature():
    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    timestamp = str(int(time.time() * 1000))
    random_bytes = secrets.token_bytes(32)
    nonce = base64.b64encode(random_bytes).decode('utf-8')

    order_data = {
        "symbol": "BTCUSDT",
        "price": "95000",
        "vol": "10",
        "side": "BUY",
        "type": "MARKET",
        "open_type": "ISOLATED",
        "position_id": 0,
        "leverage": 20,
        "external_oid": timestamp,
        "position_mode": "ONE_WAY"
    }

    body_json = json.dumps(order_data, separators=(',', ':'))
    pre_sign = f"{timestamp}{nonce}{body_json}"
    signature = hmac.new(API_SECRET.encode('utf-8'), pre_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    return jsonify({
        "api_key": API_KEY,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": signature,
        "pre_sign": pre_sign,
        "body_json": body_json
    })



@app.route("/simulate-tp", methods=["POST"])
def simulate_tp():
    try:
        data = request.json
        symbol = data.get("symbol")
        price = data.get("triggerPrice")

        if not symbol or not price:
            return jsonify({"error": "Missing 'symbol' or 'triggerPrice' in payload"}), 400

        fake_tp_data = {
            "topic": "futures.tp_sl",
            "data": {
                "symbol": symbol.upper(),
                "triggerPrice": float(price)
            }
        }

        handle_tp_sl(fake_tp_data)
        return jsonify({"status": "TP event processed"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/webhook/<symbol>', methods=['POST'])
def webhook(symbol):
    return webhook_handler(symbol)

if __name__ == '__main__':
    ws_thread = threading.Thread(target=start_websocket_listener, daemon=True)
    ws_thread.start()
    app.run(host='0.0.0.0', port=5000)
