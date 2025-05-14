import asyncio
import websockets
import hashlib
import time
import json
import random
import string
from modules.config import API_KEY, API_SECRET, LEVERAGE
from modules.logger_config import logger
# from modules.state import position_state, save_position_state, get_or_create_symbol_direction_state
from modules.postgres_state_manager import get_or_create_symbol_direction_state, update_position_state, \
    delete_position_state
from modules.utils import place_tp_sl_order_async, cancel_all_new_orders, modify_tp_sl_order_async
from modules.loss_tracking import log_profit_loss
from datetime import datetime

# TP distribution: 70% for TP1, 10% each for TP2–TP4
TP_DISTRIBUTION = [0.7, 0.1, 0.1, 0.1]

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
        asyncio.create_task(send_heartbeat(websocket))

        subscribe_request = {
            "op": "subscribe",
            "args": [
                {"ch": "order"},
                {"ch": "position"},
                {"ch": "tp_sl"}
            ]
        }
        await websocket.send(json.dumps(subscribe_request))

        while True:
            try:
                message = await websocket.recv()
                logger.info(f"[WS MESSAGE] {message}")
                await handle_ws_message(message)
            except websockets.exceptions.ConnectionClosedError as e:
                logger.warning(f"[WS] Connection closed unexpectedly: {e}")
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
            state = get_or_create_symbol_direction_state(symbol, direction, position_id=position_id)
            logger.info(f"State inside position: {state}")
            # Weighted average entry price update
            old_qty = state.get("total_qty", 0)
            logger.info(f"[WEBSOCKET_HANDLER]: position_event: {position_event} new_qty: {new_qty} old_qty: {old_qty}")
            # Extract and normalize time info
            ctime_str = pos_event.get("ctime")
            tps = state.get("tps", [])
            try:
                ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
            except Exception:
                ctime = datetime.utcnow()
            log_date = ctime.strftime("%Y-%m-%d")
            if position_event == "OPEN" or (position_event == "UPDATE" and new_qty > old_qty):
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
                if state.get("step", 0) == 0 and state.get("tps"):
                    tp1 = state["tps"][0]
                    logger.info(f"tp1:{tp1}")
                    sl_price = state["stop_loss"]
                    tp_qty = round(new_qty * 0.7, 3)
                    full_qty = round(new_qty, 3)
                    if tp_qty > 0 and position_id:
                        if position_event != "OPEN" and new_qty > old_qty:
                            logger.info(
                                f"[TP/SL MODIFIED FOR UPDATE/ADDED QTY] {symbol} {direction} TP1 {tp1}, SL {sl_price}, qty {tp_qty}/{full_qty}, positionId {position_id}")
                            await modify_tp_sl_order_async(direction, symbol, tp1, sl_price, position_id, tp_qty,
                                                           full_qty)
                            # place_tp_sl_order(symbol=symbol, tp_price=tp1, sl_price=sl_price, position_id=position_id,
                            #                 tp_qty=tp_qty, qty=full_qty)
                        elif position_event == "OPEN":
                            await place_tp_sl_order_async(symbol=symbol, tp_price=tp1, sl_price=sl_price,
                                                          position_id=position_id,
                                                          tp_qty=tp_qty, qty=full_qty)
                            logger.info(
                                f"[TP/SL SET FOR OPEN ORDER] {symbol} {direction} TP1 {tp1}, SL {sl_price}, qty {tp_qty}/{full_qty}, positionId {position_id}")
                update_position_state(symbol, direction, position_id, state)

            if position_event == "UPDATE" and new_qty < old_qty:
                step = state.get("step", 0)
                position_id = pos_event.get("positionId")
                profit_amount = float(pos_event.get("realizedPNL"))

                log_profit_loss(symbol, direction, str(position_id), round(profit_amount, 4),
                                "PROFIT" if profit_amount > 0 else "LOSS",
                                ctime, log_date)
                logger.info(
                    f"[P&L LOGGED] {'Profit' if profit_amount > 0 else 'Loss'} of {abs(profit_amount):.4f} logged for {symbol} {direction} at TP{step + 1}")

                if not tps or step >= len(tps):
                    logger.warning(f"No TP state for {symbol} {direction}. Skipping.")
                    return

                new_sl = state.get("entry_price") if step == 0 else tps[step - 1]
                next_step = step + 1
                new_tp = tps[next_step] if next_step < len(tps) else None
                tp_qty = round(old_qty * 0.1, 3)

                logger.info(f"Step {step} hit for {symbol} {direction}. New SL: {new_sl}, Next TP: {new_tp}")

                if step == 0:
                    try:
                        await cancel_all_new_orders(symbol, direction)
                    except Exception as cancel_err:
                        logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")

                if new_tp:
                    logger.info(
                        f"[POSITION_NEW_TP]: {step} {symbol} {new_tp} {new_sl} {position_id} {tp_qty} {new_qty}")
                    await modify_tp_sl_order_async(direction, symbol=symbol, tp_price=new_tp, sl_price=new_sl,
                                                   position_id=position_id,
                                                   tp_qty=tp_qty, sl_qty=new_qty)
                else:
                    logger.info(f"[POSITION_NO_TP]: {symbol} {new_tp} {new_sl} {position_id} {tp_qty} {new_qty}")
                    await modify_tp_sl_order_async(direction, symbol=symbol, tp_price=None, sl_price=new_sl,
                                                   position_id=position_id,
                                                   tp_qty=None, sl_qty=new_qty)

                state["step"] = next_step
                # state["total_qty"] = round(new_qty - tp_qty, 3)
                logger.info(
                    f"Step {step} hit for {symbol} {direction}. New SL: {new_sl}, Next TP: {new_tp} , tp_qty: {tp_qty}, sl_qty: {new_qty}")
                update_position_state(symbol, direction, position_id, state)

            if position_event == "CLOSE" and new_qty == 0:
                # delete_position_state(symbol, direction, position_id)
                realized_pnl = float(pos_event.get("realizedPNL"))
                position_id = state.get("position_id")
                try:
                    await cancel_all_new_orders(symbol, direction)
                    update_position_state(symbol, direction, position_id, {
                        "status": "CLOSED"
                    })
                except Exception as cancel_err:
                    logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
                try:
                    log_profit_loss(symbol, direction, str(position_id), round(realized_pnl, 4),
                                    "PROFIT" if realized_pnl > 0 else "LOSS",
                                    ctime, log_date)
                except Exception as log_pnl_error:
                    logger.info(f"[CLOSE POSITION ALL PNL TRIGGERED]: {log_pnl_error}")

    except Exception as e:
        logger.error(f"WebSocket message handler error: {str(e)}")
