import asyncio
import websockets
import hashlib
import time
import json
import random
import string
from modules.config import API_KEY, API_SECRET
from modules.logger_config import logger
# from modules.state import position_state, save_position_state, get_or_create_symbol_direction_state
from postgres_state_manager import get_or_create_symbol_direction_state, update_position_state, delete_position_state
from modules.utils import place_tp_sl_order, cancel_all_new_orders
from loss_tracking import log_profit_loss
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


async def start_websocket_listener():
    ws_url = "wss://fapi.bitunix.com/private/"
    nonce = generate_nonce()
    sign, timestamp = generate_signature(API_KEY, API_SECRET, nonce)

    try:
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
            try:
                while True:
                    message = await websocket.recv()
                    logger.info(f"[WS MESSAGE] {message}")
                    await handle_ws_message(message)
            except Exception as e:
                logger.error(f"[WS ERROR] {e}")
    except Exception as outer_e:
        logger.error(f"[WS CONNECTION ERROR] {outer_e}")


async def handle_ws_message(message):
    try:
        data = json.loads(message)
        topic = data.get("ch")

        if topic == "tpsl":
            tp_event = data.get("data", {})
            symbol = tp_event.get("symbol", "BTCUSDT")
            direction = tp_event.get("side")
            tp_price_hit = float(tp_event.get("tpPrice", 0))


            # if "slOrderPrice" in tp_event:
            #     sl_price_hit = float(tp_event.get("slOrderPrice", 0))
            #     loss_amount = 0
            #     position_state = get_or_create_symbol_direction_state(symbol, direction)
            #     entry_price = position_state.get("entry_price", 0)
            #     total_qty = position_state.get("total_qty", 0)
            #
            #     if direction == "SELL":
            #         loss_amount = (entry_price - sl_price_hit) * total_qty
            #     else:
            #         loss_amount = (sl_price_hit - entry_price) * total_qty
            #     log_profit_loss(symbol, direction, pnl, entry_type, ctime, date)
            #     # update_loss(round(abs(loss_amount), 4))
            #     del position_state[symbol][direction]
            #     logger.info(
            #         f"[SL HIT] Loss of {abs(loss_amount):.4f} logged for {symbol} {direction} at SL price {sl_price_hit}")

        if topic == "position":
            pos_event = data.get("data", {})
            symbol = pos_event.get("symbol", "BTCUSDT")
            side = pos_event.get("side", "LONG").upper()
            direction = "BUY" if side == "LONG" else "SELL"
            position_event = pos_event.get("event")
            new_qty = float(pos_event.get("qty", 0))
            state = get_or_create_symbol_direction_state(symbol, direction)
            logger.info(f"State inside position: {state}")
            # Weighted average entry price update
            old_qty = state.get("total_qty", 0)
            logger.info("In Here 1")
            # Extract and normalize time info
            ctime_str = pos_event.get("ctime")
            tps = state.get("tps", [])
            try:
                ctime = datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
            except Exception:
                ctime = datetime.utcnow()
            log_date = ctime.strftime("%Y-%m-%d")
            if position_event == "OPEN" or (position_event == "UPDATE" and new_qty > old_qty):
                if direction not in state.get(symbol, {}):
                    logger.warning(
                        f"[DIRECTION MISMATCH] {symbol} {direction} not initialized. Skipping TP/SL placement.")
                    return
                current_position_id = state.get("position_id")
                position_id = current_position_id if current_position_id is not None else pos_event.get("positionId")
                logger.info(f"positionId: {position_id}")
                position_margin = float(pos_event.get("margin"))
                logger.info(f"position_margion: {position_margin}")
                # position_rpnl = float(pos_event.get("realizedPNL"))
                position_fee = float(pos_event.get("fee"))
                logger.info(f"position_fee: {position_fee}")
                state["position_id"] = position_id
                avg_entry = (position_margin + position_fee) / new_qty
                logger.info(f"avg_entry: {avg_entry}")
                state["entry_price"] = round(avg_entry, 6)
                state["total_qty"] = round(new_qty, 3)
                logger.info("In here 2")
                x = state.get("tps")
                logger.info(f"In Here 3:{x}")
                if state.get("step", 0) == 0 and state.get("tps"):
                    tp1 = state["tps"][0]
                    logger.info(f"tp1:{tp1}")
                    sl_price = state["stop_loss"]
                    tp_qty = round(new_qty * 0.7, 3)
                    full_qty = round(new_qty, 3)
                    if tp_qty > 0 and position_id:
                        if position_event != "OPEN" and new_qty > old_qty:
                            # modify_tp_sl_order(symbol, tp1, sl_price, position_id, tp_qty, full_qty)
                            place_tp_sl_order(symbol=symbol, tp_price=tp1, sl_price=sl_price, position_id=position_id,
                                              tp_qty=tp_qty, qty=full_qty)
                        else:
                            place_tp_sl_order(symbol=symbol, tp_price=tp1, sl_price=sl_price, position_id=position_id,
                                          tp_qty=tp_qty, qty=full_qty)
                            logger.info(
                            f"[TP/SL SET] {symbol} {direction} TP1 {tp1}, SL {sl_price}, qty {tp_qty}/{full_qty}, positionId {position_id}")
                logger.info(f"In Here 4: {state}")
                update_position_state(symbol, direction, state)

            if position_event == "UPDATE" and new_qty < old_qty:
                step = state.get("step", 0)
                current_position_id = state.get("position_id")
                position_id = current_position_id if current_position_id is not None else pos_event.get("positionId")
                total_qty = state.get("total_qty", 0)
                profit_amount = float(pos_event.get("realizedPNL"))

                log_profit_loss(symbol, direction, round(profit_amount, 4), "PROFIT" if profit_amount > 0 else "LOSS", ctime, log_date)

                logger.info(f"[P&L LOGGED] {'Profit' if profit_amount > 0 else 'Loss'} of {abs(profit_amount):.4f} logged for {symbol} {direction} at TP{step + 1}")

                if not tps or step >= len(tps):
                    logger.warning(f"No TP state for {symbol} {direction}. Skipping.")
                    return

                new_sl = state.get("entry_price") if step == 0 else tps[step - 1]
                next_step = step + 1
                new_tp = tps[next_step] if next_step < len(tps) else None
                tp_qty = round(total_qty * 0.1, 3)

                logger.info(f"Step {step} hit for {symbol} {direction}. New SL: {new_sl}, Next TP: {new_tp}")

                if step == 0:
                    try:
                        cancel_all_new_orders(symbol)
                    except Exception as cancel_err:
                        logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")

                if new_tp:
                    place_tp_sl_order(symbol=symbol, tp_price=new_tp, sl_price=new_sl, position_id=position_id,
                                      tp_qty=tp_qty, qty=total_qty)
                else:
                    place_tp_sl_order(symbol=symbol, tp_price=None, sl_price=new_sl, position_id=position_id,
                                      tp_qty=None, qty=total_qty)

                state["step"] = next_step
                state["total_qty"] = round(total_qty - tp_qty, 3)
                update_position_state(symbol, direction, state)

            if position_event == "CLOSE" and new_qty == 0:
                delete_position_state(symbol, direction)
                realized_pnl = float(pos_event.get("realizedPNL"))
                log_profit_loss(symbol, direction, round(realized_pnl, 4), "PROFIT" if realized_pnl > 0 else "LOSS", ctime, log_date)

    except Exception as e:
        logger.error(f"WebSocket message handler error: {str(e)}")


# def handle_tp_sl(data):
#     tp_event = data.get("data", {})
#     tp_price_hit = float(tp_event.get("tpPrice", 0))
#     symbol = tp_event.get("symbol", "BTCUSDT")
#     direction = tp_event.get("side")
#
#     state = get_or_create_symbol_direction_state(symbol, direction)
#     logger.info(f"TP trigger detected for {symbol} {direction} at price: {tp_price_hit}")
#
#     try:
#         step = state.get("step", 0)
#         entry_price = state.get("entry_price")
#         total_qty = state.get("total_qty", 0)
#
#         qty_distribution = TP_DISTRIBUTION
#         qty = round(total_qty * qty_distribution[step], 3) if step < len(qty_distribution) else 0
#
#         if direction == "SELL":
#             profit_amount = (entry_price - tp_price_hit) * qty
#         else:
#             profit_amount = (tp_price_hit - entry_price) * qty
#
#         if profit_amount > 0:
#             update_profit(round(profit_amount, 4))
#         else:
#             update_loss(round(abs(profit_amount), 4))
#
#         logger.info(
#             f"[P&L LOGGED] {'Profit' if profit_amount > 0 else 'Loss'} of {abs(profit_amount):.4f} logged for {symbol} {direction} at TP{step + 1}")
#     except Exception as e:
#         logger.warning(f"[P&L LOGGING FAILED] Could not log profit for {symbol} {direction}: {str(e)}")
#
#     tps = state.get("tps", [])
#     if not tps or step >= len(tps):
#         logger.warning(f"No TP state for {symbol} {direction}. Skipping.")
#         return
#
#     new_sl = state.get("entry_price") if step == 0 else tps[step - 1]
#     next_step = step + 1
#     new_tp = tps[next_step] if next_step < len(tps) else None
#
#     logger.info(f"Step {step} hit for {symbol} {direction}. New SL: {new_sl}, Next TP: {new_tp}")
#
#     if step == 0:
#         try:
#             random_bytes = secrets.token_bytes(32)
#             nonce = base64.b64encode(random_bytes).decode('utf-8')
#             timestamp = str(int(time.time() * 1000))
#             body_json = json.dumps({"symbol": symbol}, separators=(',', ':'))
#             digest_input = nonce + timestamp + API_KEY + body_json
#             digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
#             sign_input = digest + API_SECRET
#             signature = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()
#
#             headers = {
#                 "api-key": API_KEY,
#                 "sign": signature,
#                 "nonce": nonce,
#                 "timestamp": timestamp,
#                 "Content-Type": "application/json"
#             }
#
#             cancel_resp = requests.post(
#                 f"{BASE_URL}/api/v1/futures/trade/cancel_all_orders",
#                 headers=headers,
#                 data=body_json
#             )
#             cancel_resp.raise_for_status()
#             logger.info(f"[LIMIT ORDERS CANCELLED] {cancel_resp.json()}")
#         except Exception as cancel_err:
#             logger.error(f"[CANCEL LIMIT ORDERS FAILED] {cancel_err}")
#
#     order_id = state.get("primary_order_id")
#     if new_tp:
#         next_qty = round(total_qty * 0.1, 3)
#         modify_tp_sl_order(symbol, new_tp, new_sl, order_id, qty=next_qty)
#     else:
#         modify_tp_sl_order(symbol, None, new_sl, order_id, qty=round(total_qty * 0.1, 3))
#
#     state["step"] = next_step
#     state["total_qty"] = round(total_qty - qty, 3)
#     update_position_state(symbol, direction, state)

