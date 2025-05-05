import websocket
import ssl
import json
import time
import hmac
import hashlib
import logging
import requests
from datetime import datetime
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger, error_logger
from modules.state import position_state, save_position_state

def start_websocket_listener():
    def on_open(ws):
        logger.info("WebSocket opened. Sending login request.")
        timestamp = str(int(time.time() * 1000))
        pre_sign = timestamp + "GET" + "/user/verify"
        signature = hmac.new(API_SECRET.encode(), pre_sign.encode(), hashlib.sha256).hexdigest()

        auth_payload = {
            "op": "login",
            "args": [API_KEY, timestamp, signature]
        }
        ws.send(json.dumps(auth_payload))
        logger.info("Login payload sent.")

        subscribe_payload = {
            "op": "subscribe",
            "args": ["futures.position", "futures.tp_sl", "futures.order"]
        }
        ws.send(json.dumps(subscribe_payload))
        logger.info("Subscription payload sent.")

    def on_message(ws, message):
        try:
            data = json.loads(message)
            topic = data.get("topic")

            if topic == "futures.tp_sl":
                handle_tp_sl(data)
            elif topic == "futures.order":
                handle_order(data)

        except Exception as e:
            error_logger.error(json.dumps({"timestamp": datetime.utcnow().isoformat(), "error": str(e)}))
            logger.error(f"WebSocket message handler error: {str(e)}")

    def handle_tp_sl(data):
        tp_event = data.get("data", {})
        tp_price_hit = float(tp_event.get("triggerPrice", 0))
        symbol = tp_event.get("symbol", "BTCUSDT")

        logger.info(f"TP trigger detected for {symbol} at price: {tp_price_hit}")
        state = position_state.get(symbol, {})
        tps = state.get("tps", [])
        step = state.get("step", 0)

        if not tps or step >= len(tps):
            logger.warning(f"No TP state for {symbol}. Skipping.")
            return

        new_sl = state.get("entry") if step == 0 else tps[step - 1]
        next_step = step + 1
        new_tp = tps[next_step] if next_step < len(tps) else None

        logger.info(f"Step {step} hit. New SL: {new_sl}, Next TP: {new_tp}")

        if new_tp:
            modify_body = {
                "symbol": symbol,
                "tpTriggerPrice": str(new_tp),
                "tpTriggerType": "MARKET_PRICE",
                "slTriggerPrice": str(new_sl),
                "slTriggerType": "MARKET_PRICE"
            }

            body_json = json.dumps(modify_body, separators=(',', ':'))
            nonce = str(int(time.time() * 1000))
            timestamp = nonce
            pre_sign = f"{timestamp}{nonce}{body_json}"
            signature = hmac.new(API_SECRET.encode('utf-8'), pre_sign.encode('utf-8'), hashlib.sha256).hexdigest()

            headers = {
                "api-key": API_KEY,
                "sign": signature,
                "nonce": nonce,
                "timestamp": timestamp,
                "Content-Type": "application/json"
            }

            response = requests.post(f"{BASE_URL}/api/v1/futures/position/modify_tp_sl", headers=headers, data=body_json)
            response.raise_for_status()
            logger.info(f"TP/SL updated: {response.json()}")

        position_state[symbol].update({"step": next_step, "tps": tps})
        save_position_state()

    def handle_order(data):
        order_event = data.get("data", {})
        order_status = order_event.get("status")
        order_id = order_event.get("orderId")
        symbol = order_event.get("symbol", "BTCUSDT")

        if order_status == "FILLED":
            logger.info(f"Order filled for {symbol}: {order_event}")
            state = position_state.setdefault(symbol, {"filled_orders": set()})

            if order_id in state.get("filled_orders", set()):
                logger.info(f"Order {order_id} already handled. Skipping TP/SL setup.")
                return

            state["filled_orders"].add(order_id)
            logger.info(f"Order {order_id} marked as filled and TP/SL will be managed.")
            save_position_state()

    def on_error(ws, error):
        logger.error(f"WebSocket error: {error}")

    def on_close(ws, close_status_code, close_msg):
        logger.warning(f"WebSocket closed: {close_status_code} - {close_msg}")

    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://fapi.bitunix.com/private/",
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            logger.error(f"WebSocket connection error, retrying: {e}")
            time.sleep(5)
