import websocket
import ssl
import json
import time
import hmac
import hashlib
import logging
import requests
from datetime import datetime
import base64
import secrets
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger, error_logger
from modules.state import position_state, save_position_state
from modules.utils import update_profit, update_loss, place_tp_sl_order, modify_tp_sl_order

__all__ = ["start_websocket_listener", "handle_tp_sl"]

def start_websocket_listener():
    def on_open(ws):
        logger.info("WebSocket opened. Sending login request.")

        timestamp = str(int(time.time() * 1000))
        random_bytes = secrets.token_bytes(32)
        nonce = base64.b64encode(random_bytes).decode('utf-8')
        digest_input = nonce + timestamp + API_KEY
        digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
        sign_input = digest + API_SECRET
        signature = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()

        auth_payload = {
            "event": "login",
            "params": {
                "apiKey": API_KEY,
                "timestamp": timestamp,
                "nonce": nonce,
                "sign": signature
            }
        }

        ws.send(json.dumps(auth_payload))
        logger.info(f"Login payload sent: {auth_payload}")

        subscribe_payload = {
            "event": "subscribe",
            "params": {
                "channels": ["futures.position", "futures.tp_sl", "futures.order"]
            }
        }

        ws.send(json.dumps(subscribe_payload))
        logger.info("Subscription payload sent.")

    def on_message(ws, message):
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
                if state.get("step", 0) == 0:
                    tp1 = state.get("tps", [])[0]
                    position_id = order_event.get("positionId")
                    qty_distribution = state.get("qty_distribution", [0])
                    qty = round(qty_distribution[0] * 0.7, 6) if len(qty_distribution) > 0 else None
                    if qty:
                        place_tp_sl_order(symbol, tp1, position_id, qty)
                        logger.info(f"Placed TP1 for {symbol} at {tp1} with qty {qty}")
                save_position_state()

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
            logger.info("Starting WebSocket connection...")
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            logger.error(f"WebSocket connection error, retrying: {e}")
            time.sleep(5)

def handle_tp_sl(data):
    """Expose TP handler for use in test/simulation endpoints."""
    state = {}
    tp_event = data.get("data", {})
    tp_price_hit = float(tp_event.get("triggerPrice", 0))
    symbol = tp_event.get("symbol", "BTCUSDT")
    order_id = tp_event.get("orderId")
    state = position_state.get(symbol, {})

    logger.info(f"TP trigger detected for {symbol} at price: {tp_price_hit}")

    try:
        step = state.get("step", 0)
        entry_price = state.get("entry_price")
        qty_distribution = state.get("qty_distribution", [1])
        qty = qty_distribution[step] if step < len(qty_distribution) else 0

        if state.get("direction") == "SELL":
            profit_amount = (entry_price - tp_price_hit) * qty
        else:
            profit_amount = (tp_price_hit - entry_price) * qty

        if profit_amount > 0:
            update_profit(round(profit_amount, 4))
        else:
            update_loss(round(abs(profit_amount), 4))
        logger.info(f"[P&L LOGGED] {'Profit' if profit_amount > 0 else 'Loss'} of {abs(profit_amount):.4f} logged for {symbol} at TP{step + 1}")
    except Exception as e:
        logger.warning(f"[P&L LOGGING FAILED] Could not log profit for {symbol}: {str(e)}")

    state = position_state.get(symbol, {})
    tps = state.get("tps", [])
    step = state.get("step", 0)

    if not tps or step >= len(tps):
        logger.warning(f"No TP state for {symbol}. Skipping.")
        return

    new_sl = state.get("entry_price") if step == 0 else tps[step - 1]
    next_step = step + 1
    new_tp = tps[next_step] if next_step < len(tps) else None

    logger.info(f"Step {step} hit. New SL: {new_sl}, Next TP: {new_tp}")

    if step == 0:
        try:
            random_bytes = secrets.token_bytes(32)
            nonce = base64.b64encode(random_bytes).decode('utf-8')
            timestamp = str(int(time.time() * 1000))
            body_json = json.dumps({"symbol": symbol}, separators=(',', ':'))
            digest_input = nonce + timestamp + API_KEY + body_json
            digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
            sign_input = digest + API_SECRET
            signature = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()

            headers = {
                "api-key": API_KEY,
                "sign": signature,
                "nonce": nonce,
                "timestamp": timestamp,
                "Content-Type": "application/json"
            }

            cancel_resp = requests.post(
                f"{BASE_URL}/api/v1/futures/trade/cancel_all_orders",
                headers=headers,
                data=body_json
            )
            cancel_resp.raise_for_status()
            logger.info(f"[LIMIT ORDERS CANCELLED] {cancel_resp.json()}")
        except Exception as cancel_err:
            logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")

    if new_tp:
        qty_distribution = state.get("qty_distribution", [1])
        qty = qty_distribution[step] if step < len(qty_distribution) else 0
        modify_tp_sl_order(symbol, new_tp, new_sl, order_id, qty)

    state["step"] = next_step
    save_position_state()
