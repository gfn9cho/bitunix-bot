import asyncio
import websockets
import hashlib
import time
import json
import random
import string
from datetime import datetime
import logging
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger
from modules.state import position_state, save_position_state
from modules.utils import update_profit, update_loss, place_tp_sl_order, modify_tp_sl_order
import secrets
import base64
import requests


__all__ = ["start_websocket_listener", "handle_tp_sl"]


def generate_signature(api_key, secret_key, nonce):
    timestamp = str(int(time.time()))  # MUST be int, not str
    pre_sign = f"{nonce}{timestamp}{api_key}"
    sign = hashlib.sha256(pre_sign.encode()).hexdigest()
    final_sign = hashlib.sha256((sign + secret_key).encode()).hexdigest()
    return final_sign, timestamp


def generate_nonce(length=32):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def send_heartbeat(websocket):
    while True:
        try:
            await websocket.send(json.dumps({"op": "ping", "ping": int(time.time())}))
            logger.debug("[HEARTBEAT] Ping sent")
            await asyncio.sleep(20)
        except Exception as e:
            logger.warning(f"[HEARTBEAT] Failed to send ping: {e}")
            break

async def start_websocket_listener():
    ws_url = "wss://fapi.bitunix.com/private/"
    nonce = generate_nonce()
    sign, timestamp = generate_signature(API_KEY, API_SECRET, nonce)

    try:
        async with websockets.connect(ws_url, ping_interval=None) as websocket:
            logger.info("WS launched")

            # Step 1: Wait for connect confirmation
            connect_msg = await websocket.recv()
            logger.info(f"[WS CONNECT] {connect_msg}")

            # Step 2: Start heartbeat
            asyncio.create_task(send_heartbeat(websocket))

            # Step 3: Send login
            login_request = {
                "op": "login",
                "args": [
                    {
                        "apiKey": API_KEY,
                        "timestamp": timestamp,
                        "nonce": nonce,
                        "sign": sign,
                    }
                ],
            }
            await websocket.send(json.dumps(login_request))
            logger.info(f"[WS LOGIN SENT] {login_request}")

            login_response = await websocket.recv()
            logger.info(f"[WS LOGIN RESPONSE] {login_response}")

            # Step 4: Subscribe
            subscribe_request = {
                "op": "subscribe",
                "args": [
                    {"ch": "order"},
                    {"ch": "position"},
                    {"ch": "tp_sl"}
                ]
            }
            await websocket.send(json.dumps(subscribe_request))
            logger.info("[WS SUBSCRIBE SENT]")

            # Step 5: Start receiving
            while True:
                message = await websocket.recv()
                logger.info(f"[WS MESSAGE] {message}")
                await handle_ws_message(message)
    except Exception as e:
        logger.error(f"[WS CONNECTION ERROR] {e}")


async def handle_ws_message(message):
    try:
        data = json.loads(message)
        topic = data.get("topic")

        if topic == "futures.tp_sl":
            handle_tp_sl(data)
        elif topic == "futures.order":
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
                    market_qty = state.get("qty_distribution", [0.7])[0]
                    position_id = order_event.get("positionId")
                    if position_id:
                        place_tp_sl_order(symbol=symbol, tp_price=tp1, position_id=position_id, qty=market_qty)
                        logger.info(f"Placed TP1 for {symbol} at {tp1} with qty {market_qty} and positionId {position_id}")
                save_position_state()
    except Exception as e:
        logger.error(f"WebSocket message handler error: {str(e)}")

def handle_tp_sl(data):
    tp_event = data.get("data", {})
    tp_price_hit = float(tp_event.get("triggerPrice", 0))
    symbol = tp_event.get("symbol", "BTCUSDT")
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
        modify_tp_sl_order(symbol, new_tp, new_sl)

    state["step"] = next_step
    save_position_state()