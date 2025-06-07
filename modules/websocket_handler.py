import asyncio
import hashlib
import json
import random
import string
import time
from datetime import datetime

import websockets

from modules.config import API_KEY, API_SECRET
from modules.logger_config import logger, setup_asset_logging
from modules.loss_tracking import log_profit_loss
# from modules.state import position_state, save_position_state, get_or_create_symbol_direction_state
from modules.redis_state_manager import get_or_create_symbol_direction_state, \
    update_position_state, delete_position_state
from modules.redis_client import get_redis
# from modules.postgres_state_manager import get_or_create_symbol_direction_state, update_position_state
from modules.utils import place_tp_sl_order_async, cancel_all_new_orders, \
    modify_tp_sl_order_async, update_tp_quantity, update_sl_price, reduce_buffer_loss

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
            setup_asset_logging(symbol)
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
            avg_entry = state.get("entry_price", 0)
            if new_qty != old_qty:
                logger.info(
                    f"[WEBSOCKET_HANDLER]: position_event: {pos_event} new_qty: {new_qty} old_qty: {old_qty}")
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
                avg_entry = state.get("entry_price", 0)
                # position_margin = float(pos_event.get("margin"))
                # position_leverage = float(pos_event.get("leverage", 20))
                # position_rpnl = float(pos_event.get("realizedPNL"))
                # position_fee = float(pos_event.get("fee"))
                state["position_id"] = position_id
                state["status"] = "OPEN"
                state["order_type"] = "limit"
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

                pending_qty = state.get("pending_qty", 0)
                ws_extra = {
                    "symbol": symbol,
                    "direction": direction,
                    "interval": state.get("interval", "")
                }
                logger.info(
                    f"[POSITION OPEN] old_qty={old_qty} new_qty={new_qty} pending_qty={pending_qty}",
                    extra=ws_extra,
                )
                state["total_qty"] = new_qty
                state["pending_qty"] = 0
                await update_position_state(symbol, direction, position_id, state)

            # New logic: if position qty increases after step > 0, update TP/SL
            if position_event == "UPDATE" and new_qty > old_qty:
                tp_acc_zone_id = state.get("tp_acc_zone_id")
                trade_action = state.get("trade_action").lower()
                order_type = state.get("order_type", "market")
                revised_qty = state.get("revised_qty", 0)
                acc_qty = new_qty - revised_qty
                logger.info(
                    f"[POSITION UPDATE] {symbol} {direction} action={trade_action} order_type={order_type}",
                    extra={"symbol": symbol, "direction": direction, "interval": state.get("interval", "")},
                )
                if order_type == "market":
                    for tp_label, order_id in state["tp_orders"].items():
                        step_index = int(tp_label.replace("TP", "")) - 1
                        tp_price = state["tps"][step_index]
                        tp_qty = round(new_qty * TP_DISTRIBUTION[step_index], 3)
                        logger.info(
                            f"[TP/SL UPDATED ON QTY INCREASE] {symbol} {direction} Step {step_index} TP: {tp_price}, TPQty: {tp_qty}")
                        await update_tp_quantity(order_id, symbol, tp_qty, tp_price)
                    state["order_type"] = "limit"
                else:
                    if not tp_acc_zone_id:
                        logger.info(
                                f"[TP/SL OPEN FOR ACC ENTRIES] {symbol} {direction} TP: {avg_entry}, TPQty: {new_qty}")
                        order_id = await place_tp_sl_order_async(symbol, tp_price=avg_entry, sl_price=None,
                                                      position_id=position_id, tp_qty=acc_qty, qty=new_qty)
                        if order_id:
                            state.setdefault("tp_acc_zone_id", order_id)
                    else:
                        logger.info(
                            f"[TP/SL UPDATE FOR ACC ENTRIES] {symbol} {direction} TP: {avg_entry}, TPQty: {new_qty}")
                        await update_tp_quantity(tp_acc_zone_id, symbol, acc_qty, avg_entry)
                    limit_order_qty = new_qty - old_qty
                    state["limit_order_qty"] = limit_order_qty

                sl_order_id = state.get("sl_order_id")
                sl_price = state["stop_loss"]
                await update_sl_price(sl_order_id, direction, symbol, sl_price, new_qty)
                pending_qty = state.get("pending_qty", 0)
                logger.info(
                    f"[POSITION UPDATE] old_qty={old_qty} new_qty={new_qty} pending_qty={pending_qty}",
                    extra=ws_extra,
                )
                state["total_qty"] = new_qty
                state["pending_qty"] = 0
                await update_position_state(symbol, direction, position_id, state)
            if position_event == "CLOSE" and new_qty == 0:
                state = await get_or_create_symbol_direction_state(symbol, direction, position_id)
                realized_pnl = float(pos_event.get("realizedPNL"))
                position_id = pos_event.get("positionId")
                old_qty = state.get("total_qty", 0)
                interval = state.get("interval", "5m")
                try:
                    buffer_key = f"reverse_loss:{symbol}:{direction}:{interval}"
                    r = get_redis()
                    existing_raw = await r.get(buffer_key)
                    if existing_raw:
                        buffer_value = json.loads(existing_raw)
                    else:
                        buffer_value = {"qty": 0, "pnl": 0, "interval": interval}

                    buffer_value["qty"] += old_qty
                    buffer_value["pnl"] += realized_pnl
                    buffer_value["closed_at"] = datetime.utcnow().isoformat()

                    await r.set(buffer_key, json.dumps(buffer_value))
                    logger.info(
                        f"[BUFFERED POSITION CLOSE] {symbol}-{direction} qty={buffer_value['qty']} pnl={buffer_value['pnl']} interval={interval}",
                        extra={"symbol": symbol, "direction": direction, "interval": interval},
                    )
                except Exception as log_pnl_error:
                    logger.info(
                        f"[REVERSAL LOSS BUFFER ERROR]: {log_pnl_error}",
                        extra={"symbol": symbol, "direction": direction, "interval": interval},
                    )

                try:
                    await cancel_all_new_orders(symbol, direction)
                    await update_position_state(symbol, direction, position_id, {
                        "status": "CLOSED"
                    })
                    await delete_position_state(symbol, direction, position_id)
                    log_profit_loss(
                        symbol,
                        direction,
                        str(position_id),
                        round(realized_pnl, 4),
                        "PROFIT" if realized_pnl > 0 else "LOSS",
                        ctime,
                        log_date,
                    )
                except Exception as cancel_err:
                    logger.error(
                        f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}",
                        extra={"symbol": symbol, "direction": direction, "interval": interval},
                    )
        elif topic == "tpsl":
            # This flow handles only take profit event.
            try:
                tp_data = data.get("data", {})
                event = tp_data.get("event")
                status = tp_data.get("status")
                tp_qty = tp_data.get("tpQty")
                sl_qty = tp_data.get("slQty")
                symbol = tp_data.get("symbol")
                setup_asset_logging(symbol)
                position_id = tp_data.get("positionId")
                tp_direction = tp_data.get("side", "BUY").upper()
                position_direction = "SELL" if tp_direction == "BUY" else "BUY"
                try:
                    state = await get_or_create_symbol_direction_state(symbol, position_direction, position_id=position_id)
                    interval = state.get("interval","15m")
                except Exception as e:
                    logger.info(
                        f"[TP SL EVENT] Position might closed for {symbol} {position_direction}",
                        extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                    )
                    return
                if event != "CLOSE" or status != "FILLED" or tp_qty is None:
                    # logger.info(f"[TP/SL EVENT SKIPPED] Ignored event: {tp_data} with status: {status}")
                    return
                else:
                    logger.info(
                        f"[TP/SL EVENT] Processing event: {tp_data} with status: {status}",
                        extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                    )
                tp_acc_zone_id = state.get("tp_acc_zone_id")
                if tp_acc_zone_id and tp_acc_zone_id != "":
                    try:
                        await cancel_all_new_orders(symbol, position_direction)
                        logger.info(
                            f"[TP ACC EVENT] Canceled limit order: {tp_data} with status: {status}",
                            extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                        )
                        state["tp_acc_zone_id"] = ""
                        await update_position_state(symbol, position_direction, position_id, state)
                        return
                    except Exception as cancel_err:
                        logger.error(
                            f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}",
                            extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                        )
                tps = state.get("tps", [])
                step = state.get("step", 0)
                try:
                    is_sl = True if sl_qty and sl_qty > 0 else False
                    entry_price = float(state.get("entry_price", 0))
                    r = get_redis()
                    if is_sl:
                        sl_price = float(state.get("stop_loss", entry_price))
                        loss_value = -abs(entry_price - sl_price) * float(sl_qty)
                        buffer_key = f"reverse_loss:{symbol}:{tp_direction}:{interval}"
                        existing_raw = await r.get(buffer_key)
                        if existing_raw:
                            buffer_value = json.loads(existing_raw)
                        else:
                            buffer_value = {"qty": 0, "pnl": 0, "interval": interval}
                        buffer_value["qty"] += float(sl_qty)
                        buffer_value["pnl"] += loss_value
                        buffer_value["closed_at"] = datetime.utcnow().isoformat()
                        await r.set(buffer_key, json.dumps(buffer_value))
                        logger.info(
                            f"[BUFFERED SL CLOSE] {symbol}-{tp_direction} qty={buffer_value['qty']} pnl={buffer_value['pnl']} interval={interval}",
                            extra={"symbol": symbol, "direction": tp_direction, "interval": interval},
                        )
                    else:
                        tp_price = float(tps[step]) if step < len(tps) else entry_price
                        profit_value = abs(tp_price - entry_price) * float(tp_qty)
                        await reduce_buffer_loss(symbol, tp_direction, profit_value)
                except Exception as log_pnl_error:
                    logger.info(
                        f"[CLOSE POSITION ALL PNL TRIGGERED]: {log_pnl_error}",
                        extra={"symbol": symbol, "direction": tp_direction, "interval": interval},
                    )
                # logger.info(f"[TPSL EVENT]: {tp_data}")

                tps = state.get("tps", [])
                step = state.get("step", 0)
                old_qty = float(state.get("total_qty", 0))
                # tp_qty_triggered = float(tp_data.get("tpQty"))
                next_step = step + 1
                triggered_qty = sum(TP_DISTRIBUTION[:next_step]) * old_qty
                new_qty = round(old_qty - triggered_qty, 3)
                trigger_price = float(tps[step])
                entry = float(state.get("entry_price", 0))
                logger.info(
                    f"[TP SL INFO]:{state}",
                    extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                )

                try:
                    if step == 0 and entry != 0:
                        if position_direction == "BUY":
                            new_sl = entry - (3 / 7) * ((trigger_price - entry) * 0.2)
                        else:
                            new_sl = entry + (3 / 7) * ((entry - trigger_price) * 0.2)
                    else:
                        new_sl = tps[step - 1]
                except Exception as e:
                    logger.info(
                        f"[TP LEVEL 1 Beakeven calculation error]",
                        extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                    )
                    new_sl = float(state.get("entry_price")) if step == 0 else tps[max(step - 1, 0)]

                logger.info(
                    f"[BREAKEVEN SL] {symbol} {position_direction} step={step} → SL={new_sl}",
                    extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                )

                if step == 0:
                    try:
                        await cancel_all_new_orders(symbol, position_direction)
                    except Exception as cancel_err:
                        logger.error(
                            f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}",
                            extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                        )
                order_id = state.get("sl_order_id")
                await update_sl_price(order_id, position_direction, symbol, new_sl, new_qty)
                state["step"] = next_step
                # state["total_qty"] = new_qty
                await update_position_state(symbol, position_direction, position_id, state)
            except Exception as e:
                logger.error(
                    f"[TP_SL CHANNEL ERROR] {e}",
                    extra={"symbol": symbol, "direction": position_direction, "interval": interval},
                )
    except Exception as e:
        logger.error(
            f"WebSocket message handler error: {str(e)}",
            extra={"symbol": symbol if 'symbol' in locals() else None},
        )
