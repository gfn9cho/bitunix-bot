from quart import Quart, request, jsonify
from modules.webhook_handler import webhook_handler
from modules.logger_config import logger
from modules.websocket_handler import start_websocket_listener
from modules.redis_client import get_redis
from quart import Blueprint, jsonify
import json
import asyncio
import os
import json
import time
import hmac
import base64
import secrets

app = Quart(__name__)


# --- Launch WebSocket Listener Correctly ---
@app.before_serving
async def startup():
    asyncio.create_task(start_websocket_listener())


# --- Debug Signature Endpoint ---
@app.route("/debug-signature", methods=["GET"])
async def debug_signature():
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
    signature = hmac.new(API_SECRET.encode(), pre_sign.encode(), digestmod='sha256').hexdigest()

    return jsonify({
        "api_key": API_KEY,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": signature,
        "pre_sign": pre_sign,
        "body_json": body_json
    })


# --- Simulated TP Trigger ---
@app.route("/simulate-tp", methods=["POST"])
async def simulate_tp():
    try:
        data = await request.get_json()
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

        # handle_tp_sl(fake_tp_data)
        return jsonify({"status": "TP event processed"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Webhook Route ---
@app.route('/webhook/<symbol>', methods=['POST'])
async def webhook(symbol):
    logger.info(f"symbol: {symbol}")
    return await webhook_handler(symbol)


admin_tools = Blueprint("admin_tools", __name__)

@admin_tools.route("/debug/redis-keys", methods=["GET"])
async def list_redis_keys():
    r = get_redis()
    keys = await r.keys("position_state:*")
    return jsonify({
        "total_keys": len(keys),
        "keys": keys
    }), 200


@admin_tools.route("/debug/cleanup-position-true", methods=["POST"])
async def cleanup_invalid_position_ids():
    r = get_redis()
    keys = await r.keys("position_state:*")
    deleted = []

    for key in keys:
        try:
            raw = await r.get(key)
            if not raw:
                continue

            value = json.loads(raw)
            if value.get("position_id") is True:
                await r.delete(key)
                deleted.append(key)

        except Exception as e:
            print(f"[CLEANUP ERROR] {key}: {e}")

    return jsonify({
        "status": "completed",
        "keys_deleted": deleted,
        "total_deleted": len(deleted)
    }), 200

# --- Start Quart ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
