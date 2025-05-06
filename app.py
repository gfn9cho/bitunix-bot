from flask import Flask, request, jsonify
from modules.webhook_handler import webhook_handler
from modules.websocket_handler import start_websocket_listener, handle_tp_sl
from modules.logger_config import logger
import threading
import asyncio
import os
import time
import json
import base64
import secrets
import hmac

app = Flask(__name__)

@app.route("/debug-signature", methods=["GET"])
def debug_signature():
    API_KEY = os.getenv("API_KEY")
    API_SECRET = os.getenv("API_SECRET")
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

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
    signature = hmac.new(API_SECRET.encode(), pre_sign.encode(), digestmod="sha256").hexdigest()

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

# WebSocket runner in separate thread-safe asyncio loop
def run_ws_listener():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_websocket_listener())

if __name__ == '__main__':
    threading.Thread(target=run_ws_listener, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))