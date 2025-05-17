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
# from modules.postgres_state_manager import get_or_create_symbol_direction_state, update_position_state
from modules.utils import place_tp_sl_order_async, cancel_all_new_orders, \
    modify_tp_sl_order_async, update_tp_quantity, update_sl_price

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
            state = await get_or_create_symbol_direction_state(symbol, direction, position_id=position_id)
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
                tp1 = state["tps"][0] if state.get("tps") else None
                sl_price = state["stop_loss"]
                if tp1:
                    tp_qty = round(new_qty * 0.7, 3)
                    logger.info(f"[INITIAL TP SET] {symbol} {direction} TP1 {tp1} Qty: {tp_qty}")
                    tp_order_id = await place_tp_sl_order_async(symbol, tp_price=tp1, sl_price=None,
                                                                position_id=position_id, tp_qty=tp_qty, qty=new_qty)
                    if tp_order_id:
                        logger.info(f"[INITIAL TP SET]: tp_order_id: {tp_order_id}")
                        state.setdefault("tp_orders", {})["TP1"] = tp_order_id

                    logger.info(f"[INITIAL SL SET] {symbol} {direction} SL {sl_price}, Qty: {new_qty}")
                    sl_order_id = await place_tp_sl_order_async(symbol, tp_price=None, sl_price=sl_price,
                                                                position_id=position_id, tp_qty=None, qty=new_qty)
                    if sl_order_id:
                        state["sl_order_id"] = sl_order_id
                for i in range(1, 4):
                    tp_price = tps[i]
                    tp_qty = round(new_qty * 0.1, 3)
                    logger.info(f"[INITIAL TP/SL SET] {symbol} {direction} TP{i} {tp_price}, tpQty: {tp_qty}")
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
            if position_event == "UPDATE" and new_qty > old_qty:
                for tp_label, order_id in state["tp_orders"].items():
                    step_index = int(tp_label.replace("TP", "")) - 1
                    tp_price = state["tps"][step_index]
                    tp_qty = round(new_qty * TP_DISTRIBUTION[step_index], 3)
                    sl_price = state["stop_loss"]
                    logger.info(
                        f"[TP/SL UPDATED ON QTY INCREASE] {symbol} {direction} Step {step_index} TP: {tp_price}, TPQty: {tp_qty}")
                    await update_tp_quantity(order_id, symbol, tp_qty, tp_price)
                    await update_sl_price(order_id, direction, symbol, new_qty, sl_price)
                # await modify_tp_sl_order_async(
                #     direction,
                #     symbol,
                #     tp_price=tp_price,
                #     sl_price=sl_price,
                #     position_id=position_id,
                #     tp_qty=tp_qty,
                #     sl_qty=new_qty
                # )
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
                # delete_position_state(symbol, direction, position_id)
                realized_pnl = float(pos_event.get("realizedPNL"))
                position_id = state.get("position_id")
                try:
                    await cancel_all_new_orders(symbol, direction)
                    await update_position_state(symbol, direction, position_id, {
                        "status": "CLOSED"
                    })
                    await delete_position_state(symbol, direction, position_id)
                except Exception as cancel_err:
                    logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
                try:
                    log_profit_loss(symbol, direction, str(position_id), round(realized_pnl, 4),
                                    "PROFIT" if realized_pnl > 0 else "LOSS",
                                    ctime, log_date)
                except Exception as log_pnl_error:
                    logger.info(f"[CLOSE POSITION ALL PNL TRIGGERED]: {log_pnl_error}")

        elif topic == "tpsl":
            try:
                tp_data = data.get("data", {})
                event = tp_data.get("event")
                status = tp_data.get("status")
                # trigger_price = float(tp_data.get("tpOrderPrice", 0))
                # logger.info(f"[TP/SL EVENT]: trigger_price: {trigger_price}")

                if event != "CLOSE" or status != "FILLED":
                    logger.info(f"[TP/SL EVENT SKIPPED] Ignored event: {tp_data} with status: {status}")
                    return
                else:
                    logger.info(f"[TP/SL EVENT] Processing event: {tp_data} with status: {status}")

                symbol = tp_data.get("symbol")
                position_id = tp_data.get("positionId")
                side = tp_data.get("side", "LONG").upper()
                direction = "BUY" if side == "LONG" else "SELL"
                logger.info(f"[TPSL EVENT]: {tp_data}")

                state = await get_or_create_symbol_direction_state(symbol, direction, position_id=position_id)
                tps = state.get("tps", [])
                step = state.get("step", 0)
                old_qty = float(state.get("total_qty", 0))
                tp_qty_triggered = float(tp_data.get("tpQty"))
                new_qty = round(old_qty - tp_qty_triggered, 3)
                next_step = step + 1
                trigger_price = float(tps[step])
                entry = float(state.get("entry_price", 0))
                logger.info(f"[TP SL INFO]:{state}")
                try:
                    if step == 0 and entry != 0:
                        if direction == "BUY":
                            new_sl = entry - (7 / 3) * (trigger_price - entry)
                        else:
                            new_sl = entry + (7 / 3) * (entry - trigger_price)
                    else:
                        new_sl = tps[step - 1]
                except Exception as e:
                    logger.info(f"[TP LEVEL 1 Beakeven calculation error]")
                    new_sl = float(state.get("entry_price")) if step == 0 else tps[max(step - 1, 0)]

                logger.info(f"[BREAKEVEN SL] {symbol} {direction} step={step} → SL={new_sl}")

                if step == 0:
                    try:
                        await cancel_all_new_orders(symbol, direction)
                    except Exception as cancel_err:
                        logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
                order_id = state.get("sl_order_id")
                await update_sl_price(order_id,direction, symbol, new_sl, new_qty)
                # if new_tp:
                #     logger.info(f"[TP_SL CHANNEL] STEP {step} → {next_step} | New TP: {new_tp} SL: {new_sl}")
                #     await update_sl_price(order_id,direction, symbol, new_sl, new_qty)
                #     await modify_tp_sl_order_async(direction, symbol=symbol, tp_price=new_tp, sl_price=new_sl,
                #                                    position_id=position_id, tp_qty=tp_qty, sl_qty=new_qty)
                # else:
                #     logger.info(f"[TP_SL CHANNEL] STEP {step} → {next_step} | Final SL Only: {new_sl}")
                #     await modify_tp_sl_order_async(direction, symbol=symbol, tp_price=None, sl_price=new_sl,
                #                                    position_id=position_id, tp_qty=None, sl_qty=new_qty)

                state["step"] = next_step
                state["total_qty"] = new_qty
                await update_position_state(symbol, direction, position_id, state)
            except Exception as e:
                logger.error(f"[TP_SL CHANNEL ERROR] {e}")

    except Exception as e:
        logger.error(f"WebSocket message handler error: {str(e)}")

# async def handle_ws_message(message):
#     try:
#         data = json.loads(message)
#         topic = data.get("ch")
#
#         if topic == "position":
#             pos_event = data.get("data", {})
#             symbol = pos_event.get("symbol", "BTCUSDT")
#             side = pos_event.get("side", "LONG").upper()
#             direction = "BUY" if side == "LONG" else "SELL"
#             position_event = pos_event.get("event")
#             new_qty = float(pos_event.get("qty", 0))
#             position_id = str(pos_event.get("positionId"))
#             state = get_or_create_symbol_direction_state(symbol, direction, position_id=position_id)
#             logger.info(f"State inside position: {state}")
#             # Weighted average entry price update
#             old_qty = state.get("total_qty", 0)
#             logger.info(f"[WEBSOCKET_HANDLER]: position_event: {position_event} new_qty: {new_qty} old_qty: {old_qty}")
#             # Extract and normalize time info
#             ctime_str = pos_event.get("ctime")
#             tps = state.get("tps", [])
#             try:
#                 ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
#             except Exception:
#                 ctime = datetime.utcnow()
#             log_date = ctime.strftime("%Y-%m-%d")
#             if position_event == "OPEN" or (position_event == "UPDATE" and new_qty > old_qty):
#                 position_id = pos_event.get("positionId")
#                 position_margin = float(pos_event.get("margin"))
#                 position_leverage = float(pos_event.get("leverage", 20))
#                 # position_rpnl = float(pos_event.get("realizedPNL"))
#                 position_fee = float(pos_event.get("fee"))
#                 state["position_id"] = position_id
#                 # Have to adjust and get it from order position or make a call to order.
#                 avg_entry = ((position_margin * position_leverage) + position_fee) / new_qty
#                 state["entry_price"] = round(avg_entry, 6)
#                 state["total_qty"] = round(new_qty, 3)
#                 state["status"] = "OPEN"
#                 logger.info(
#                     f"[WEBSOCKET_HANDLER]: position_event: {position_event} avg_entry: {avg_entry} old_qty: {old_qty}")
#                 if state.get("step", 0) == 0 and state.get("tps"):
#                     tp1 = state["tps"][0]
#                     logger.info(f"tp1:{tp1}")
#                     sl_price = state["stop_loss"]
#                     tp_qty = round(new_qty * 0.7, 3)
#                     full_qty = round(new_qty, 3)
#                     if tp_qty > 0 and position_id:
#                         if position_event != "OPEN" and new_qty > old_qty:
#                             logger.info(
#                                 f"[TP/SL MODIFIED FOR UPDATE/ADDED QTY] {symbol} {direction} TP1 {tp1}, SL {sl_price}, qty {tp_qty}/{full_qty}, positionId {position_id}")
#                             await modify_tp_sl_order_async(direction, symbol, tp1, sl_price, position_id, tp_qty,
#                                                            full_qty)
#                             # place_tp_sl_order(symbol=symbol, tp_price=tp1, sl_price=sl_price, position_id=position_id,
#                             #                 tp_qty=tp_qty, qty=full_qty)
#                         elif position_event == "OPEN":
#                             await place_tp_sl_order_async(symbol=symbol, tp_price=tp1, sl_price=sl_price,
#                                                           position_id=position_id,
#                                                           tp_qty=tp_qty, qty=full_qty)
#                             logger.info(
#                                 f"[TP/SL SET FOR OPEN ORDER] {symbol} {direction} TP1 {tp1}, SL {sl_price}, qty {tp_qty}/{full_qty}, positionId {position_id}")
#                 update_position_state(symbol, direction, position_id, state)
#
#             if position_event == "CLOSE" and new_qty == 0:
#                 # delete_position_state(symbol, direction, position_id)
#                 realized_pnl = float(pos_event.get("realizedPNL"))
#                 position_id = state.get("position_id")
#                 try:
#                     await cancel_all_new_orders(symbol, direction)
#                     update_position_state(symbol, direction, position_id, {
#                         "status": "CLOSED"
#                     })
#                 except Exception as cancel_err:
#                     logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
#                 try:
#                     log_profit_loss(symbol, direction, str(position_id), round(realized_pnl, 4),
#                                     "PROFIT" if realized_pnl > 0 else "LOSS",
#                                     ctime, log_date)
#                 except Exception as log_pnl_error:
#                     logger.info(f"[CLOSE POSITION ALL PNL TRIGGERED]: {log_pnl_error}")
#
#         elif topic == "tp_sl":
#             try:
#                 tp_data = data.get("data", {})
#                 symbol = tp_data.get("symbol")
#                 trigger_price = float(tp_data.get("triggerPrice"))
#                 position_id = tp_data.get("positionId")
#                 side = tp_data.get("side", "LONG").upper()
#                 direction = "BUY" if side == "LONG" else "SELL"
#                 trigger_type = tp_data.get("triggerType")
#                 event = tp_data.get("event")
#
#                 status = tp_data.get("status")
#                 if event != "CLOSE" or status != "FILLED":
#                     logger.info(f"[TP/SL EVENT SKIPPED] Ignored event: {event} with status: {status}")
#                     return
#
#                 state = get_or_create_symbol_direction_state(symbol, direction, position_id=position_id)
#                 tps = state.get("tps", [])
#                 step = state.get("step", 0)
#                 old_qty = state.get("total_qty", 0)
#                 tp_qty = round(old_qty * TP_DISTRIBUTION[step], 3)
#                 new_qty = round(old_qty - tp_qty, 3)
#                 next_step = step + 1
#
#                 new_sl = state.get("entry_price") if step == 0 else tps[step - 1]
#                 new_tp = tps[next_step] if next_step < len(tps) else None
#
#                 if step == 0:
#                     try:
#                         await cancel_all_new_orders(symbol, direction)
#                     except Exception as cancel_err:
#                         logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
#
#                 if new_tp:
#                     logger.info(f"[TP_SL CHANNEL] STEP {step} → {next_step} | New TP: {new_tp} SL: {new_sl}")
#                     await modify_tp_sl_order_async(direction, symbol=symbol, tp_price=new_tp, sl_price=new_sl,
#                                                    position_id=position_id, tp_qty=tp_qty, sl_qty=new_qty)
#                 else:
#                     logger.info(f"[TP_SL CHANNEL] STEP {step} → {next_step} | Final SL Only: {new_sl}")
#                     await modify_tp_sl_order_async(direction, symbol=symbol, tp_price=None, sl_price=new_sl,
#                                                    position_id=position_id, tp_qty=None, sl_qty=new_qty)
#
#                 state["step"] = next_step
#                 state["total_qty"] = new_qty
#                 update_position_state(symbol, direction, position_id, state)
#             except Exception as e:
#                 logger.error(f"[TP_SL CHANNEL ERROR] {e}")
#
#     except Exception as e:
#         logger.error(f"WebSocket message handler error: {str(e)}")
