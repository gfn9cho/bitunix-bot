import asyncio
import base64
import hashlib
import httpx
import json
import re
import secrets
import time
from datetime import datetime

from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger
# from modules.postgres_state_manager import update_position_state, get_or_create_symbol_direction_state
from modules.redis_state_manager import get_or_create_symbol_direction_state, update_position_state, delete_position_state
from modules.redis_client import get_redis
from typing import Optional


def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def generate_get_sign_api(nonce, timestamp, method, data):
    query_params = ""
    body = ""
    if data:
        if method.lower() == "get":
            data = {k: v for k, v in data.items() if v is not None}
            query_params = '&'.join([f"{k}={v}" for k, v in sorted(data.items())])
            query_params = re.sub(r'[^a-zA-Z0-9]', '', query_params)
        if method.lower() == "post":
            # body = str(data).replace(" ", "")
            body = str(data)

    digest_input = nonce + timestamp + API_KEY + query_params + body
    digest = hashlib.sha256(digest_input.encode()).hexdigest()
    sign_input = digest + API_SECRET
    sign = hashlib.sha256(sign_input.encode()).hexdigest()

    return sign


async def is_valid_sl_price(direction: str, sl_price: float,
                            mark_price: float, buffer_pct: float = 0.001) -> bool:
    if direction == "BUY":
        return sl_price < mark_price * (1 + buffer_pct)
    else:
        return sl_price > mark_price * (1 - buffer_pct)


async def is_valid_tp_price(direction: str, tp_price: float,
                            mark_price: float, buffer_pct: float = 0.001) -> bool:
    if direction == "BUY":
        return tp_price > mark_price * (1 + buffer_pct)
    else:
        return tp_price < mark_price * (1 - buffer_pct)


async def safe_submit_sl_update(symbol: str, direction: str, sl_payload: dict, sl_price: float, retries: int = 3,
                                retry_delay: int = 2) -> bool:
    from modules.price_feed import get_latest_mark_price
    for attempt in range(retries):
        try:
            mark_price = await get_latest_mark_price(symbol)
            if not mark_price:
                raise ValueError("Mark price unavailable")

            if await is_valid_sl_price(direction, sl_price, mark_price):
                logger.info(f"[SL ✅] Submitting SL {sl_price} (mark: {mark_price}) for {symbol} {direction}")
                await submit_modified_tp_sl_order_async(sl_payload)
                return True
            else:
                buffer_pct = 0.001
                adjusted_sl = mark_price * (1 + buffer_pct) if direction == "BUY" else mark_price * (1 - buffer_pct)
                sl_payload["slPrice"] = str(round(adjusted_sl, 6))
                logger.warning(f"[TP ❌] Adjusting SL to {adjusted_sl} due to invalid original value")
                await submit_modified_tp_sl_order_async(sl_payload)
                return True
        except Exception as e:
            logger.error(f"[SL ERROR] Retry {attempt + 1} for {symbol} {direction}: {e}")
            await asyncio.sleep(retry_delay)

    logger.error(f"[SL FAILED] Giving up SL update for {symbol} {direction} after {retries} retries.")
    return False


async def safe_submit_tp_update(symbol: str, direction: str, tp_payload: dict, tp_price: float, retries: int = 3,
                                retry_delay: int = 2) -> bool:
    from modules.price_feed import get_latest_mark_price
    for attempt in range(retries):
        try:
            mark_price = await get_latest_mark_price(symbol)
            if not mark_price:
                raise ValueError("Mark price unavailable")

            if await is_valid_tp_price(direction, tp_price, mark_price):
                logger.info(f"[TP ✅] Submitting TP {tp_price} (mark: {mark_price}) for {symbol} {direction}")
                await submit_modified_tp_sl_order_async(tp_payload)
                return True
            else:
                buffer_pct = 0.001
                adjusted_tp = mark_price * (1 + buffer_pct) if direction == "BUY" else mark_price * (1 - buffer_pct)
                tp_payload["tpPrice"] = str(round(adjusted_tp, 6))
                logger.warning(f"[TP ❌] Adjusting TP to {adjusted_tp} due to invalid original value")
                await submit_modified_tp_sl_order_async(tp_payload)
                return True
        except Exception as e:
            logger.error(f"[TP ERROR] Retry {attempt + 1} for {symbol} {direction}: {e}")
            await asyncio.sleep(retry_delay)

    logger.error(f"[TP FAILED] Giving up TP update for {symbol} {direction} after {retries} retries.")
    return False


async def submit_modified_tp_sl_order_async(order_data):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    body_json = json.dumps(order_data, separators=(',', ':'))
    digest_input = nonce + timestamp + API_KEY + body_json
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign_input = digest + API_SECRET
    signature = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "api-key": API_KEY,
        "sign": signature,
        "timestamp": timestamp,
        "nonce": nonce
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(f"{BASE_URL}/api/v1/futures/tpsl/modify_order", headers=headers,
                                         content=body_json)
            response.raise_for_status()
            logger.info(f"[TP/SL MODIFY SUCCESS] {body_json}")
            logger.info(f"[TP/SL MODIFY SUCCESS] {response.json()}")
            return response.json()

    except httpx.RequestError as e:
        logger.error(f"[TP/SL MODIFY SUCCESS] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[TP/SL MODIFY SUCCESS] Response: {e.response.text}")
        return None


async def update_tp_quantity(order_id: str, symbol: str, new_tp_qty: float, new_tp_price: float):
    payload = {
        "symbol": symbol,
        "orderId": order_id,
        "tpQty": str(round(new_tp_qty, 3)),
        "tpOrderType": "MARKET",
        "tpStopType": "MARK_PRICE",
        "tpPrice": str(new_tp_price)
    }

    logger.info(f"[MODIFY TP QTY] {symbol} orderId={order_id} new_tp_qty={new_tp_qty}")
    await submit_modified_tp_sl_order_async(payload)


async def update_sl_price(order_id: str, direction: str, symbol: str, new_sl_price: float, sl_qty: float):
    payload = {
        "symbol": symbol,
        "orderId": order_id,
        "slPrice": str(round(new_sl_price, 6)),
        "slQty": str(round(sl_qty, 3)),
        "slOrderType": "MARKET",
        "slStopType": "MARK_PRICE"
    }

    logger.info(f"[MODIFY SL] {symbol} orderId={order_id} new_sl_price={new_sl_price}")
    await safe_submit_sl_update(symbol, direction, payload, new_sl_price)


async def modify_tp_sl_order_async(direction, symbol, tp_price, sl_price, position_id, tp_qty, sl_qty):
    url = f"{BASE_URL}/api/v1/futures/tpsl/get_pending_orders"
    random_bytes = secrets.token_bytes(32)
    nonce = base64.b64encode(random_bytes).decode('utf-8')
    timestamp = str(int(time.time() * 1000))

    data = {"symbol": symbol}
    method = "get"
    sign = generate_get_sign_api(nonce, timestamp, method, data)

    headers = {
        "api-key": API_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
        "language": "en-US",
        "Content-Type": "application/json"
    }

    try:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(method, url, headers=headers, params=data)
                response.raise_for_status()
                response_data = response.json()
        except httpx.RequestError as e:
            logger.error(f"[PENDING TP/SL ORDERS] {e}")
            if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                logger.error(f"[PENDING TP/SL ORDERS] Response: {e.response.text}")
            return None

        orders = response_data.get("data", {})
        logger.info(f"[PENDING TP/SL ORDERS]: {response_data}")

        if not orders:
            logger.warning(f"[MODIFY TP/SL] No pending TP/SL orders found for {symbol} position {position_id}")
            return

        sl_orders = None
        tp_orders = None

        matched_orders = [o for o in orders if o["positionId"] == position_id]
        pending_orders_length = len(matched_orders)

        for o in matched_orders:
            if o["tpPrice"] is None:
                if pending_orders_length == 1:
                    sl_orders = {
                        "data": {
                            "symbol": symbol,
                            "orderId": o["id"],
                            "tpPrice": str(tp_price),
                            "tpStopType": "MARK_PRICE",
                            "tpOrderType": "MARKET",
                            "tpQty": str(tp_qty),
                            "slPrice": str(sl_price),
                            "slStopType": "MARK_PRICE",
                            "slOrderType": "MARKET",
                            "slQty": str(sl_qty),
                        }
                    }
                else:
                    sl_orders = {
                        "data": {
                            "symbol": symbol,
                            "orderId": o["id"],
                            "slPrice": str(sl_price),
                            "slStopType": "MARK_PRICE",
                            "slOrderType": "MARKET",
                            "slQty": str(sl_qty),
                        }
                    }
            else:
                tp_orders = {
                    "data": {
                        "symbol": symbol,
                        "orderId": o["id"],
                        "tpPrice": str(tp_price),
                        "tpStopType": "MARK_PRICE",
                        "tpOrderType": "MARKET",
                        "tpQty": str(tp_qty),
                    }
                }

        logger.info(f"[MODIFY ORDER] {sl_orders} {tp_orders}")
        if pending_orders_length == 1:
            success = await safe_submit_sl_update(symbol, direction, sl_orders["data"],
                                                  float(sl_orders["data"]["slPrice"]))
            if not success:
                logger.warning(f"[SL WARNING] Take Profit update failed for {symbol} {direction}")
        else:
            if sl_orders:
                success = await safe_submit_sl_update(symbol, direction, sl_orders["data"],
                                                      float(sl_orders["data"]["slPrice"]))
                if not success:
                    logger.warning(f"[SL WARNING] Take Profit SL update failed for {symbol} {direction}")

            if tp_orders:
                success = await safe_submit_tp_update(symbol, direction, tp_orders["data"],
                                                      float(tp_orders["data"]["tpPrice"]))
                if not success:
                    logger.warning(f"[TP WARNING] Take Profit TP update failed for {symbol} {direction}")

    except Exception as e:
        logger.error(f"[MODIFY TP/SL ERROR] Failed for {symbol} {direction}: {e}")


async def place_tp_sl_order_async(symbol, tp_price, sl_price, position_id, tp_qty, qty):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
    if sl_price:
        order_data = {
            "symbol": symbol,
            "positionId": position_id,
            "slPrice": str(sl_price),
            "slStopType": "MARK_PRICE",
            "slOrderType": "MARKET",
            "slQty": str(qty)
        }
    else:
        order_data = {
            "symbol": symbol,
            "positionId": position_id,
            "tpPrice": str(tp_price),
            "tpStopType": "MARK_PRICE",
            "tpOrderType": "MARKET",
            "tpQty": str(tp_qty)
        }

    body_json = json.dumps(order_data, separators=(',', ':'))
    digest_input = nonce + timestamp + API_KEY + body_json
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign_input = digest + API_SECRET
    signature = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "api-key": API_KEY,
        "sign": signature,
        "timestamp": timestamp,
        "nonce": nonce
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{BASE_URL}/api/v1/futures/tpsl/place_order", headers=headers,
                                         content=body_json)
            response.raise_for_status()
            logger.info(f"[TP/SL ORDER SUCCESS] {response.json()}")
            response_data = response.json().get("data")

            order_id = None

            if isinstance(response_data, list) and len(response_data) > 0:
                order_id = response_data[0].get("orderId")
            elif isinstance(response_data, dict):
                order_id = response_data.get("orderId")
            return order_id
    except httpx.RequestError as e:
        logger.error(f"[ORDER FAILED] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[ORDER FAILED] Response: {e.response.text}")
        return None


async def reduce_buffer_loss(symbol: str, direction: str, profit: float) -> None:
    """Apply realized profit to buffered losses for the symbol/direction."""
    pattern = f"reverse_loss:{symbol}:{direction}:*"
    r = get_redis()
    entries = []
    async for key in r.scan_iter(match=pattern):
        raw = await r.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            entries.append((key, data))
        except Exception:
            continue

    # Sort by closed_at so older losses are reduced first
    entries.sort(key=lambda x: x[1].get("closed_at", ""))
    remaining = profit
    for key, data in entries:
        if remaining <= 0:
            break
        loss_val = float(data.get("pnl", 0))
        qty = float(data.get("qty", 0))
        if loss_val >= 0:
            continue
        updated = loss_val + remaining
        if updated >= 0:
            await r.delete(key)
            remaining = updated
        else:
            data["pnl"] = updated
            if loss_val != 0:
                data["qty"] = qty * abs(updated) / abs(loss_val)
            await r.set(key, json.dumps(data))
            remaining = 0


async def get_total_buffered_loss(symbol: str, direction: str) -> float:
    pattern = f"reverse_loss:{symbol}:{direction}:*"
    total_qty = 0.0
    net_pnl = 0.0
    keys = []
    r = get_redis()
    async for key in r.scan_iter(match=pattern):
        value = await r.get(key)
        if value:
            try:
                data = json.loads(value)
                qty = float(data.get("qty", 0))
                pnl = float(data.get("pnl", 0))
                net_pnl += pnl
                if pnl < 0:
                    total_qty += qty
                keys.append(key)
            except Exception as e:
                print(f"[REDIS ERROR] Failed to parse buffer for {key}: {e}")

    if net_pnl >= 0:
        for key in keys:
            await r.delete(key)
        return 0.0

    return total_qty


async def reduce_buffered_loss(symbol: str, direction: str, profit: float, interval: Optional[str] = None) -> None:
    """Reduce stored loss across all intervals when profit is realized."""
    if profit <= 0:
        return
    r = get_redis()
    keys = []
    async for key in r.scan_iter(match=f"reverse_loss:{symbol}:{direction}:*"):
        if isinstance(key, bytes):
            key = key.decode()
        keys.append(key)
    target_key = f"reverse_loss:{symbol}:{direction}:{interval}" if interval else None
    if target_key and target_key in keys:
        keys.remove(target_key)
        keys.insert(0, target_key)

    for key in keys:
        if profit <= 0:
            break
        raw = await r.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            loss = float(data.get("loss", 0))
            qty = float(data.get("qty", 0))
        except Exception as e:
            print(f"[REDIS ERROR] Failed to parse buffer for {key}: {e}")
            continue

        if profit >= loss:
            profit -= loss
            await r.delete(key)
        else:
            remaining_loss = loss - profit
            new_qty = qty * remaining_loss / loss if loss else qty
            data.update({"loss": remaining_loss, "qty": new_qty})
            await r.set(key, json.dumps(data))
            profit = 0

    if profit > 0:
        async for k in r.scan_iter(match=f"reverse_loss:{symbol}:{direction}:*"):
            await r.delete(k)



async def evaluate_signal_received(symbol: str, new_direction: str, new_qty: float, new_interval: str):
    """
    Evaluates whether a reversal is needed based on multi-timeframe rank.
    Handles safe closure and state cleanup of opposite direction positions.
    """
    opposite_direction = "SELL" if new_direction == "BUY" else "BUY"
    existing_state = await get_or_create_symbol_direction_state(symbol, opposite_direction, '', True)
    same_direction_state = await get_or_create_symbol_direction_state(symbol, new_direction, '', False, True)

    active_state = None
    if existing_state and existing_state.get("status") == "OPEN":
        active_state = existing_state
    elif same_direction_state and same_direction_state.get("status") == "OPEN":
        active_state = same_direction_state

    if active_state:
        result = evaluate_multi_timeframe_strategy(
            existing_direction=active_state["direction"],
            existing_interval=active_state["interval"],
            existing_qty=active_state["total_qty"],
            new_direction=new_direction,
            new_interval=new_interval
        )
    # CASE 1: Cleanup stale state if it’s not OPEN
    else:
        logger.info(f"[REVERSE BUFFER CHECK] No open {opposite_direction} position for {symbol}. Deleting stale state.")
        await delete_position_state(symbol, opposite_direction, "")
        # Check for buffered reversal quantity
        # buffer_key = f"reverse_loss:{symbol}:{opposite_direction}:{new_interval}"
        # r = get_redis()
        # buffer_raw = await r.get(buffer_key)
        previous_loss_qty = await get_total_buffered_loss(symbol, new_direction)
        if previous_loss_qty:
            new_qty += previous_loss_qty
        logger.info(f"[REVERSE BUFFER APPLIED] {symbol}-{new_direction} +{previous_loss_qty}")
        # if buffer_raw:
        #     try:
        #         buffer_data = json.loads(buffer_raw)
        #         buffered_qty = float(buffer_data.get("qty", 0))
        #         new_qty += buffered_qty
        #         await r.delete(buffer_key)
        #         logger.info(f"[REVERSE BUFFER APPLIED] {symbol}-{new_direction} +{buffered_qty}")
        #     except Exception as buffer_error:
        #         logger.warning(f"[BUFFER ERROR] Failed to apply buffered quantity for {symbol}: {buffer_error}")
        return {"action": "open", "reverse_qty": new_qty}

    action = result["action"]
    position_id = active_state.get("position_id") if active_state else ""

    # CASE 2: Reversal authorized
    if action == "reverse":
        logger.info(f"[REVERSAL DETECTED] Closing {opposite_direction} position on {symbol} to open {new_direction}")
        try:
            if await flash_close_positions(symbol, position_id) == "failed":
                await flash_close_positions(symbol, position_id)
            # await update_position_state(symbol, opposite_direction, position_id, {"status": "CLOSED"})
            # await delete_position_state(symbol, opposite_direction, position_id)
            total_qty = active_state["total_qty"] + new_qty
            return {"action": "reverse", "reverse_qty": round(total_qty, 3)}
        except Exception as e:
            logger.error(f"[REVERSAL ERROR] Failed to close and flip position for {symbol}: {e}")
            total_qty = active_state["total_qty"] + new_qty
            return {"action": "reverse", "reverse_qty": round(total_qty, 3)}
    elif action == "open":
        logger.info(f"[LOW INTERVAL SIGNAL] Opening {new_direction} position on {symbol} at interval {new_interval}")
        return {"action": "open", "reverse_qty": new_qty}
    elif action == "upgrade":
        logger.info(f"[LOW OR SAME INTERVAL SIGNAL] Upgrading {new_direction} position on {symbol} at interval {new_interval}")
        return {"action": "upgrade", "reverse_qty": 2 * new_qty}
    # CASE 3: Reversal NOT allowed — keep OPEN state intact and upgrade with new qty and price
    logger.info(f"[REVERSE CHECK] No reversal permitted for {symbol}. Existing opposite position retained.")
    return {"action": "ignore", "reverse_qty": new_qty}


TIMEFRAME_RANK = {"3m": 1, "5m": 2, "15m": 3, "1h": 4, "4h": 5, "1d": 6}


def evaluate_multi_timeframe_strategy(
        existing_direction: str,
        existing_interval: str,
        existing_qty: float,
        new_direction: str,
        new_interval: str
) -> dict:
    """
    Decide action for a new signal based on existing position's direction and timeframe.

    Returns:
        {
            "action": "ignore" | "upgrade" | "reverse" | "open",
            "reverse_qty": float  # only used if action == "reverse"
        }
    """
    existing_rank = TIMEFRAME_RANK.get(existing_interval, 0)
    new_rank = TIMEFRAME_RANK.get(new_interval, 0)

    if existing_direction == new_direction:
        if existing_rank > new_rank:
            return {"action": "ignore", "reverse_qty": 0}
        return {"action": "upgrade", "reverse_qty": existing_qty}
    # Opposite direction: consider reversal only if new is higher ranked
    elif existing_rank <= new_rank:
        return {"action": "reverse", "reverse_qty": existing_qty}
    else:
        return {"action": "open", "reverse_qty": 0}


async def maybe_reverse_position(symbol: str, new_direction: str):
    """
    If an opposite position is open, closes it and opens a new one with doubled quantity.
    """
    opposite_direction = "SELL" if new_direction == "BUY" else "BUY"
    existing_state = await get_or_create_symbol_direction_state(symbol, opposite_direction, '', True)

    if not existing_state or existing_state.get("status") != "OPEN":
        logger.info(f"[REVERSE CHECK] No open {opposite_direction} position for {symbol}. Proceeding normally.")
        await delete_position_state(symbol, opposite_direction)
        return 0  # no reversal needed

    logger.info(f"[REVERSAL DETECTED] Closing {opposite_direction} position on {symbol} to open {new_direction}")

    try:
        opposite_position_id = existing_state.get("position_id")
        # Optional: cancel any remaining limit/TP/SL orders
        # await cancel_all_new_orders(symbol, opposite_direction, context="reversal")
        await close_all_positions(symbol)
        await close_all_positions(symbol)
    # Update old state as CLOSED
        await update_position_state(symbol, opposite_direction, opposite_position_id, {"status": "CLOSED"})
        await delete_position_state(symbol, opposite_direction, opposite_position_id)

        # Return doubled quantity to use for new entry
        return round(existing_state["total_qty"], 3)

    except Exception as e:
        logger.error(f"[REVERSAL ERROR] Failed to close and flip position for {symbol}: {e}")
        return 0


async def place_order(symbol, side, price, qty, order_type="LIMIT", leverage=20, tp=None, sl=None, private=True,
                      reduce_only=False):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "qty": str(qty),
        "price": str(price),
        "side": side.upper(),
        "orderType": order_type.upper(),
        "tradeSide": "OPEN",
        "effect": "GTC",
        "clientId": timestamp,
    }

    if reduce_only:
        order_data["reduceOnly"] = True
        order_data["tradeSide"] = "CLOSE"
    if tp:
        order_data["tpPrice"] = str(tp)
        order_data["tpOrderType"] = "MARKET"
        order_data["tpStopType"] = "MARK_PRICE"
    if sl:
        order_data.update({
            "slPrice": str(sl),
            "slStopType": "MARK_PRICE",
            "slOrderType": "MARKET"
        })

    body_json = json.dumps(order_data, separators=(',', ':'))
    digest_input = nonce + timestamp + API_KEY + body_json
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign_input = digest + API_SECRET
    signature = hashlib.sha256(sign_input.encode('utf-8')).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "api-key": API_KEY,
        "sign": signature,
        "timestamp": timestamp,
        "nonce": nonce
    }

    # logger.info(f"[ORDER DATA] {order_data}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{BASE_URL}/api/v1/futures/trade/place_order",
                headers=headers,
                content=body_json
            )
            response.raise_for_status()
            logger.info(f"[ORDER SUCCESS] {response.json()}")
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"[ORDER FAILED] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[ORDER FAILED] Response: {e.response.text}")
        return None


def parse_signal(message):
    lines = message.split('\n')
    signal_type = lines[0].strip().lower()
    direction = 'SELL' if 'short' in signal_type else 'BUY'

    def extract_value(key):
        for line in lines:
            if key in line:
                return float(re.findall(r"[\d.]+", line)[0])
        return None

    entry_price = extract_value("Entry Price")
    stop_loss = extract_value("Stop Loss")
    tps = [float(tp) for tp in re.findall(r"TP\d+:\s*([\d.]+)", message)]
    logger.info(f"[tps]: {tps}")
    acc_zone_match = re.search(r"Accumulation Zone: ([\d.]+) - ([\d.]+)", message)
    acc_top, acc_bottom = float(acc_zone_match.group(1)), float(acc_zone_match.group(2))

    return {
        "direction": direction,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profits": tps,
        "accumulation_zone": [acc_top, acc_bottom]
    }


def calculate_zone_entries(acc_zone):
    top, bottom = acc_zone
    mid = (top + bottom) / 2
    return [top, mid, bottom]


def calculate_quantities(prices, direction):
    multipliers = [10, 10, 20]  # $ amounts
    return [round(m / p, 6) for m, p in zip(multipliers, prices)]


async def is_duplicate_signal(symbol, direction, buffer_secs=5):
    r = get_redis()
    key = f"signal_lock:{symbol}:{direction}"
    logger.info(f"[DUPLICATE SIGNAL]: {key}")
    current_ts = int(time.time())
    logger.info(f"[DUPLICATE SIGNAL]: {current_ts}")

    # Atomic set-if-not-exists with expiration
    try:
        was_set = await r.set(key, current_ts, nx=True, ex=buffer_secs)
    except Exception as e:
        logger.error(f"[REDIS ERROR] Failed to check duplicate signal for {key}: {e}")
        return True  # fail-safe: treat it as duplicate to avoid bad order
    logger.info(f"[DUPLICATE SIGNAL]: {was_set}")
    if not was_set:
        logger.info(f"[DUPLICATE SIGNAL CONFIRMED]")
        return True  # already locked
    return False


async def close_all_positions(symbol):
    close_position_payload = {
        "symbol": symbol
    }

    close_position_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
    close_position_ts = str(int(time.time() * 1000))
    close_position_body = json.dumps(close_position_payload, separators=(',', ':'))
    close_position_digest = hashlib.sha256((close_position_nonce + close_position_ts + API_KEY + close_position_body).encode()).hexdigest()
    close_position_sign = hashlib.sha256((close_position_digest + API_SECRET).encode()).hexdigest()

    close_position_headers = {
        "api-key": API_KEY,
        "sign": close_position_sign,
        "nonce": close_position_nonce,
        "timestamp": close_position_ts,
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            close_position_response = await client.post(
                f"{BASE_URL}/api/v1/futures/trade/close_all_position",
                headers=close_position_headers,
                content=close_position_body
            )
            close_position_response.raise_for_status()
            logger.info(f"[ORDER CANCEL SUCCESS] {symbol}: {close_position_response.json()}")
    except httpx.RequestError as e:
        logger.error(f"[ORDER CANCEL FAILED] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[ORDER CANCEL FAILED] Response: {e.response.text}")
        return None


async def flash_close_positions(symbol, position_id):
    close_position_payload = {
        "positionId": position_id
    }

    close_position_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
    close_position_ts = str(int(time.time() * 1000))
    close_position_body = json.dumps(close_position_payload, separators=(',', ':'))
    close_position_digest = hashlib.sha256((close_position_nonce + close_position_ts + API_KEY + close_position_body).encode()).hexdigest()
    close_position_sign = hashlib.sha256((close_position_digest + API_SECRET).encode()).hexdigest()

    close_position_headers = {
        "api-key": API_KEY,
        "sign": close_position_sign,
        "nonce": close_position_nonce,
        "timestamp": close_position_ts,
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            close_position_response = await client.post(
                f"{BASE_URL}/api/v1/futures/trade/flash_close_position",
                headers=close_position_headers,
                content=close_position_body
            )
            close_position_response.raise_for_status()
            if close_position_response.json().get("code") != 0:
                logger.warning(f"[FLASH CLOSE IGNORED] {symbol} {position_id}: {close_position_response.json().get('msg')}")
                return "failed"
            else:
                logger.info(f"[FLASH CLOSE SUCCESS] {symbol} {position_id}: {close_position_response.json()}")
                return "success"
    except httpx.RequestError as e:
        logger.error(f"[FLASH CLOSE FAILED] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[FLASH CLOSE FAILED] Response: {e.response.text}")
        return None


async def get_order_detail(order_id):
    # Step 1: Prepare authentication and GET headers
    method = "get"
    data = {"orderId": order_id}
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
    timestamp = str(int(time.time() * 1000))
    signature = generate_get_sign_api(nonce, timestamp, method, data)

    headers = {
        "api-key": API_KEY,
        "sign": signature,
        "nonce": nonce,
        "timestamp": timestamp,
        "language": "en-US",
        "Content-Type": "application/json"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{BASE_URL}/api/v1/futures/trade/get_order_detail",
                headers=headers,
                params=data
            )
            response.raise_for_status()
            order_entry_price = response.json().get("data", {}).get("price", 0.0)
            return float(round(order_entry_price, 6))
    except httpx.RequestError as e:
        logger.error(f"[PENDING ORDER CAPTURE FAILED] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[PENDING ORDER CAPTURE FAILED] Response: {e.response.text}")
        return 0.0


async def cancel_all_new_orders(symbol, direction, context="tp"):
    try:
        # Map direction to Bitunix side string
        side_map = {"BUY": "LONG", "SELL": "SHORT"}
        bitunix_side = side_map.get(direction.upper())
        if not bitunix_side:
            logger.error(f"[CANCEL ORDERS] Invalid direction: {direction}")
            return

        # Step 1: Prepare authentication and GET headers
        method = "get"
        data = {"symbol": symbol}
        nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
        timestamp = str(int(time.time() * 1000))
        signature = generate_get_sign_api(nonce, timestamp, method, data)

        headers = {
            "api-key": API_KEY,
            "sign": signature,
            "nonce": nonce,
            "timestamp": timestamp,
            "language": "en-US",
            "Content-Type": "application/json"
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{BASE_URL}/api/v1/futures/trade/get_pending_orders",
                    headers=headers,
                    params=data
                )
                response.raise_for_status()
                orders = response.json().get("data", {}).get("orderList", [])
        except httpx.RequestError as e:
            logger.error(f"[PENDING ORDER CAPTURE FAILED] {e}")
            if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                logger.error(f"[PENDING ORDER CAPTURE FAILED] Response: {e.response.text}")
            return None

        # Filter orders based on context
        if context == "reversal":
            cancel_list = [
                {"orderId": o["orderId"]}
                for o in orders
                if o.get("side") in {bitunix_side, direction.upper()}
            ]
        else:
            cancel_list = [
                {"orderId": o["orderId"]}
                for o in orders
                if (o.get("status", "").startswith("NEW") or o.get("status", "").startswith("PART")) and
                   (o.get("side") in {bitunix_side, direction.upper()})
            ]

        if not cancel_list:
            logger.info(f"[CANCEL ORDERS] No orders to cancel for {symbol} ({context})")
            return

        cancel_payload = {
            "symbol": symbol,
            "orderList": cancel_list
        }

        cancel_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        cancel_ts = str(int(time.time() * 1000))
        cancel_body = json.dumps(cancel_payload, separators=(',', ':'))
        cancel_digest = hashlib.sha256((cancel_nonce + cancel_ts + API_KEY + cancel_body).encode()).hexdigest()
        cancel_sign = hashlib.sha256((cancel_digest + API_SECRET).encode()).hexdigest()

        cancel_headers = {
            "api-key": API_KEY,
            "sign": cancel_sign,
            "nonce": cancel_nonce,
            "timestamp": cancel_ts,
            "Content-Type": "application/json"
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                cancel_response = await client.post(
                    f"{BASE_URL}/api/v1/futures/trade/cancel_orders",
                    headers=cancel_headers,
                    content=cancel_body
                )
                cancel_response.raise_for_status()
                logger.info(f"[ORDER CANCEL SUCCESS] {symbol}: {cancel_list} {cancel_response.json()}")
        except httpx.RequestError as e:
            logger.error(f"[ORDER CANCEL FAILED] {e}")
            if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
                logger.error(f"[ORDER CANCEL FAILED] Response: {e.response.text}")
            return None

    except Exception as e:
        logger.error(f"[CANCEL ORDERS FAILED] {symbol}: {e}")
