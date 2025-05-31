import asyncio
import hashlib
import json
import random
import string
import time
from datetime import datetime

import websockets

from modules.config import API_KEY, API_SECRET
from modules.logger_config import logger
from modules.loss_tracking import log_profit_loss
# from modules.state import position_state, save_position_state, get_or_create_symbol_direction_state
from modules.redis_state_manager import get_or_create_symbol_direction_state, \
    update_position_state, delete_position_state
from modules.redis_client import get_redis
# from modules.postgres_state_manager import get_or_create_symbol_direction_state, update_position_state
from modules.utils import place_tp_sl_order_async, cancel_all_new_orders, \
    modify_tp_sl_order_async, update_tp_quantity, update_sl_price

# TP distribution: 70% for TP1, 10% each for TP2–TP4
TP_DISTRIBUTION = [0.7, 0.1, 0.1, 0.1]
INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60, "4h": 240}

__all__ = ["start_websocket_listener"]


def generate_signature(api_key, secret_key, nonce):
    timestamp = int(time.time())
    pre_sign = f"{nonce}{timestamp}{api_key}"
    sign = hashlib.sha256(pre_sign.encode()).hexdigest()
    final_sign = hashlib.sha256((sign + secret_key).encode()).hexdigest()
    return final_sign, timestamp


def generate_nonce(length=32):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def send_heartbeat(websocket):
    while True:
        await websocket.send(json.dumps({"op": "ping", "ping": int(time.time())}))
        await asyncio.sleep(20)


async def listen_and_process(ws_url):
    nonce = generate_nonce()
    sign, timestamp = generate_signature(API_KEY, API_SECRET, nonce)
    async with websockets.connect(ws_url, ping_interval=None) as websocket:
        logger.info("WS launched")
        connect_msg = await websocket.recv()
        logger.info(f"[WS CONNECT] {connect_msg}")

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
        await websocket.recv()
        heartbeat_task = asyncio.create_task(send_heartbeat(websocket))

        subscribe_request = {
            "op": "subscribe",
            "args": [
                {"ch": "order"},
                {"ch": "position"},
                {"ch": "tpsl"}
            ]
        }
        await websocket.send(json.dumps(subscribe_request))

        while True:
            try:
                message = await websocket.recv()
                # logger.info(f"[WS MESSAGE] {message}")
                await handle_ws_message(message)
            except websockets.exceptions.ConnectionClosedError as e:
                logger.warning(f"[WS] Connection closed unexpectedly: {e}")
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    logger.info("[WS] Heartbeat task cancelled cleanly")
                raise
            except Exception as e:
                logger.error(f"[WS MESSAGE ERROR] {e}")


async def start_websocket_listener():
    ws_url = "wss://fapi.bitunix.com/private/"
    while True:
        try:
            await listen_and_process(ws_url)
        except Exception as outer_e:
            logger.error(f"[WS CONNECTION ERROR] {outer_e}")
            logger.info("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)


async def handle_ws_message(message):
    try:
        data = json.loads(message)
        topic = data.get("ch")

        if topic == "position":
            pos_event = data.get("data", {})
            symbol = pos_event.get("symbol", "BTCUSDT")
            side = pos_event.get("side", "LONG").upper()
            direction = "BUY" if side == "LONG" else "SELL"
            position_event = pos_event.get("event")
            new_qty = float(pos_event.get("qty", 0))
            position_id = str(pos_event.get("positionId"))
            state = await get_or_create_symbol_direction_state(symbol, direction, position_id=position_id) \
                if position_event != "CLOSE" else {}
            # logger.info(f"State inside position: {state}")
            # Weighted average entry price update
            old_qty = state.get("total_qty", 0)
            if new_qty != old_qty:
                logger.info(
                    f"[WEBSOCKET_HANDLER]: position_event: {position_event} new_qty: {new_qty} old_qty: {old_qty}")
            # Extract and normalize time info
            ctime_str = pos_event.get("ctime")
            tps = state.get("tps", [])
            try:
                ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
            except Exception:
                ctime = datetime.utcnow()
            log_date = ctime.strftime("%Y-%m-%d")
            if position_event == "OPEN":
                position_id = pos_event.get("positionId")
                position_margin = float(pos_event.get("margin"))
                position_leverage = float(pos_event.get("leverage", 20))
                # position_rpnl = float(pos_event.get("realizedPNL"))
                position_fee = float(pos_event.get("fee"))
                state["position_id"] = position_id
                # Have to adjust and get it from order position or make a call to order.
                avg_entry = ((position_margin * position_leverage) + position_fee) / new_qty
                state["entry_price"] = round(avg_entry, 6)
                state["total_qty"] = round(new_qty, 3)
                state["status"] = "OPEN"
                logger.info(
                    f"[WEBSOCKET_HANDLER]: position_event: {position_event} avg_entry: {avg_entry} old_qty: {old_qty}")
                sl_price = state["stop_loss"]
                logger.info(f"[INITIAL SL SET] {symbol} {direction} SL {sl_price}, Qty: {new_qty}")
                sl_order_id = await place_tp_sl_order_async(symbol, tp_price=None, sl_price=sl_price,
                                                            position_id=position_id, tp_qty=None, qty=new_qty)
                if sl_order_id:
                    state["sl_order_id"] = sl_order_id
                for i in range(0, 4):
                    tp_price = tps[i]
                    tp_ratio = TP_DISTRIBUTION[i]
                    tp_qty = round(new_qty * tp_ratio, 3)
                    logger.info(f"[INITIAL TP/SL SET] {symbol} {direction} TP{i + 1} {tp_price}, tpQty: {tp_qty}")
                    if tp_price:
                        order_id = await place_tp_sl_order_async(symbol, tp_price=tp_price, sl_price=None,
                                                                 position_id=position_id, tp_qty=tp_qty, qty=new_qty)
                        if order_id:
                            state.setdefault("tp_orders", {})[f"TP{i + 1}"] = order_id
                    else:
                        logger.info(
                            f"[INITIAL TP/SL NOT SET FOR]: {symbol} {direction} TP{i} {tp_price}, tpQty: {tp_qty}")

                await update_position_state(symbol, direction, position_id, state)

            # New logic: if position qty increases after step > 0, update TP/SL
            if position_event == "UPDATE" and new_qty > old_qty > 0:
                for tp_label, order_id in state["tp_orders"].items():
                    step_index = int(tp_label.replace("TP", "")) - 1
                    tp_price = state["tps"][step_index]
                    tp_qty = round(new_qty * TP_DISTRIBUTION[step_index], 3)
                    logger.info(
                        f"[TP/SL UPDATED ON QTY INCREASE] {symbol} {direction} Step {step_index} TP: {tp_price}, TPQty: {tp_qty}")
                    await update_tp_quantity(order_id, symbol, tp_qty, tp_price)
                sl_order_id = state.get("sl_order_id")
                sl_price = state["stop_loss"]
                await update_sl_price(sl_order_id, direction, symbol, sl_price, new_qty)
                try:
                    position_margin = float(pos_event.get("margin"))
                    position_leverage = float(pos_event.get("leverage", 20))
                    position_fee = float(pos_event.get("fee"))
                    avg_entry = ((position_margin * position_leverage) + position_fee) / new_qty
                    state["entry_price"] = round(avg_entry, 6)
                    state["total_qty"] = round(new_qty, 3)
                    await update_position_state(symbol, direction, position_id, state)
                except Exception as e:
                    logger.warning(f"[STATE UPDATE ERROR on qty increase] {e}")

            if position_event == "CLOSE" and new_qty == 0:
                realized_pnl = float(pos_event.get("realizedPNL"))
                position_id = pos_event.get("positionId")
                interval = state.get("interval", "5m")
                try:
                    await cancel_all_new_orders(symbol, direction)
                    await update_position_state(symbol, direction, position_id, {
                        "status": "CLOSED"
                    })
                    await delete_position_state(symbol, direction, position_id)
                except Exception as cancel_err:
                    logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
                try:
                    is_sl = True if realized_pnl < -0.3 else False
                    if is_sl:
                        buffer_key = f"reverse_loss:{symbol}:{direction}:{interval}"
                        if old_qty == 0:
                            old_qty = await get_or_create_symbol_direction_state(symbol, direction, position_id)
                        buffer_value = {
                            "qty": old_qty,
                            "interval": interval,
                            "closed_at": datetime.utcnow().isoformat()
                        }
                        # interval_minutes = INTERVAL_MINUTES.get(interval, 5)
                        # buffer_ttl = interval_minutes * 60 + 15
                        r = get_redis()
                        await r.set(buffer_key, json.dumps(buffer_value))
                        logger.info(f"[BUFFERED SL CLOSE] {symbol}-{direction} qty={old_qty} interval={interval}")
                        log_profit_loss(symbol, direction, str(position_id), round(realized_pnl, 4),
                                        "PROFIT" if realized_pnl > 0 else "LOSS",
                                        ctime, log_date)
                    else:
                        r = get_redis()
                        await r.delete(f"reverse_loss:{symbol}:{direction}:{interval}")
                except Exception as log_pnl_error:
                    logger.info(f"[CLOSE POSITION ALL PNL TRIGGERED]: {log_pnl_error}")

        elif topic == "tpsl":
            try:
                tp_data = data.get("data", {})
                event = tp_data.get("event")
                status = tp_data.get("status")
                tp_qty = tp_data.get("tpQty")

                if event != "CLOSE" or status != "FILLED" or tp_qty is None:
                    # logger.info(f"[TP/SL EVENT SKIPPED] Ignored event: {tp_data} with status: {status}")
                    return
                else:
                    logger.info(f"[TP/SL EVENT] Processing event: {tp_data} with status: {status}")

                symbol = tp_data.get("symbol")
                position_id = tp_data.get("positionId")
                tp_direction = tp_data.get("side", "BUY").upper()
                position_direction = "SELL" if tp_direction == "BUY" else "BUY"
                # logger.info(f"[TPSL EVENT]: {tp_data}")

                state = await get_or_create_symbol_direction_state(symbol, position_direction, position_id=position_id)
                tps = state.get("tps", [])
                step = state.get("step", 0)
                old_qty = float(state.get("total_qty", 0))
                # tp_qty_triggered = float(tp_data.get("tpQty"))
                next_step = step + 1
                triggered_qty = sum(TP_DISTRIBUTION[:next_step]) * old_qty
                new_qty = round(old_qty - triggered_qty, 3)
                trigger_price = float(tps[step])
                entry = float(state.get("entry_price", 0))
                logger.info(f"[TP SL INFO]:{state}")

                try:
                    if step == 0 and entry != 0:
                        if position_direction == "BUY":
                            new_sl = entry - (3 / 7) * ((trigger_price - entry) * 0.2)
                        else:
                            new_sl = entry + (3 / 7) * ((entry - trigger_price) * 0.2)
                    else:
                        new_sl = tps[step - 1]
                except Exception as e:
                    logger.info(f"[TP LEVEL 1 Beakeven calculation error]")
                    new_sl = float(state.get("entry_price")) if step == 0 else tps[max(step - 1, 0)]

                logger.info(f"[BREAKEVEN SL] {symbol} {position_direction} step={step} → SL={new_sl}")

                if step == 0:
                    try:
                        await cancel_all_new_orders(symbol, position_direction)
                    except Exception as cancel_err:
                        logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
                order_id = state.get("sl_order_id")
                await update_sl_price(order_id, position_direction, symbol, new_sl, new_qty)
                state["step"] = next_step
                # state["total_qty"] = new_qty
                await update_position_state(symbol, position_direction, position_id, state)
            except Exception as e:
                logger.error(f"[TP_SL CHANNEL ERROR] {e}")

    except Exception as e:
        logger.error(f"WebSocket message handler error: {str(e)}")
