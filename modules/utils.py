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
from modules.redis_state_manager import get_or_create_symbol_direction_state, update_position_state
from modules.price_feed import get_latest_mark_price
from modules.redis_client import get_redis


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
                            mark_price: float, buffer_pct: float = 0.02) -> bool:
    if direction == "BUY":
        return sl_price < mark_price * (1 + buffer_pct)
    else:
        return sl_price > mark_price * (1 - buffer_pct)


async def is_valid_tp_price(direction: str, tp_price: float,
                            mark_price: float, buffer_pct: float = 0.02) -> bool:
    if direction == "BUY":
        return tp_price > mark_price * (1 + buffer_pct)
    else:
        return tp_price < mark_price * (1 - buffer_pct)


async def safe_submit_sl_update(symbol: str, direction: str, sl_payload: dict, sl_price: float, retries: int = 3,
                                retry_delay: int = 2) -> bool:
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
                logger.warning(
                    f"[SL ❌] SL {sl_price} invalid vs mark {mark_price} on {symbol}. Attempt {attempt + 1}/{retries}")
                await asyncio.sleep(retry_delay)

        except Exception as e:
            logger.error(f"[SL ERROR] Retry {attempt + 1} for {symbol} {direction}: {e}")
            await asyncio.sleep(retry_delay)

    logger.error(f"[SL FAILED] Giving up SL update for {symbol} {direction} after {retries} retries.")
    return False


async def safe_submit_tp_update(symbol: str, direction: str, tp_payload: dict, tp_price: float, retries: int = 3,
                                retry_delay: int = 2) -> bool:
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
                logger.warning(
                    f"[TP ❌] TP {tp_price} invalid vs mark {mark_price} on {symbol}. Attempt {attempt + 1}/{retries}")
                await asyncio.sleep(retry_delay)

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


async def modify_tp_sl_order_async(direction, symbol, tp_price, sl_price, position_id, tp_qty, sl_qty):
    # Fetch pending TP/SL orders
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
    match = next((o for o in orders if o["positionId"] == position_id), None)
    if not match:
        logger.warning(
            f"[MODIFY FALLBACK] No matching TP/SL order found for {symbol} {position_id}. Attempting cancel and re-place.")
        return

    order_id = match["id"]

    # Validate TP/SL prices before submission
    try:
        mark_price = await get_latest_mark_price(symbol)

        tp_valid = tp_price and await is_valid_tp_price(direction, tp_price, mark_price)
        sl_valid = sl_price and await is_valid_sl_price(direction, sl_price, mark_price)

        if not tp_valid and not sl_valid:
            logger.warning(f"[TP/SL INVALID] Neither TP nor SL valid for {symbol} at mark {mark_price}")
            return

        payload = {
            "symbol": symbol,
            "orderId": order_id
        }

        if tp_valid:
            payload.update({
                "tpPrice": str(tp_price),
                "tpStopType": "MARK_PRICE",
                "tpOrderType": "MARKET",
                "tpQty": str(tp_qty)
            })
            logger.info(f"[TP ✅] Submitting TP {tp_price} (mark: {mark_price}) for {symbol} {direction}")

        if sl_valid:
            payload.update({
                "slPrice": str(sl_price),
                "slStopType": "MARK_PRICE",
                "slOrderType": "MARKET",
                "slQty": str(sl_qty)
            })
            logger.info(f"[SL ✅] Submitting SL {sl_price} (mark: {mark_price}) for {symbol} {direction}")

        modify_url = f"{BASE_URL}/api/v1/futures/tpsl/modify_order"
        async with httpx.AsyncClient() as client:
            res = await client.post(modify_url, headers=headers, content=json.dumps(payload))
            res.raise_for_status()
            logger.info(f"[TP/SL MODIFY SUCCESS] {json.dumps(payload)}")
            logger.info(f"[TP/SL MODIFY SUCCESS] {res.json()}")

    except Exception as e:
        logger.error(f"[TP/SL MODIFY FAILED] {e}")


# async def modify_tp_sl_order_async(direction, symbol, tp_price, sl_price, position_id, tp_qty, sl_qty):
#     url = f"{BASE_URL}/api/v1/futures/tpsl/get_pending_orders"
#     random_bytes = secrets.token_bytes(32)
#     nonce = base64.b64encode(random_bytes).decode('utf-8')
#     timestamp = str(int(time.time() * 1000))
#
#     data = {"symbol": symbol}
#     method = "get"
#     sign = generate_get_sign_api(nonce, timestamp, method, data)
#
#     headers = {
#         "api-key": API_KEY,
#         "nonce": nonce,
#         "timestamp": timestamp,
#         "sign": sign,
#         "language": "en-US",
#         "Content-Type": "application/json"
#     }
#
#     try:
#         try:
#             async with httpx.AsyncClient(timeout=10.0) as client:
#                 response = await client.request(method, url, headers=headers, params=data)
#                 response.raise_for_status()
#                 response_data = response.json()
#         except httpx.RequestError as e:
#             logger.error(f"[PENDING TP/SL ORDERS] {e}")
#             if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
#                 logger.error(f"[PENDING TP/SL ORDERS] Response: {e.response.text}")
#             return None
#
#         orders = response_data.get("data", {})
#         logger.info(f"[PENDING TP/SL ORDERS]: {response_data}")
#
#         if not orders:
#             logger.warning(f"[MODIFY TP/SL] No pending TP/SL orders found for {symbol} position {position_id}")
#             return
#
#         sl_orders = None
#         tp_orders = None
#
#         matched_orders = [o for o in orders if o["positionId"] == position_id]
#         pending_orders_length = len(matched_orders)
#
#         for o in matched_orders:
#             if o["tpPrice"] is None:
#                 sl_orders = {
#                     "data": {
#                         "symbol": symbol,
#                         "orderId": o["id"],
#                         "slPrice": str(sl_price),
#                         "slStopType": "MARK_PRICE",
#                         "slOrderType": "MARKET",
#                         "slQty": str(sl_qty),
#                     }
#                 }
#                 if pending_orders_length == 1:
#                     tp_orders = {
#                         "data": {
#                             "symbol": symbol,
#                             "orderId": o["id"],
#                             "tpPrice": str(tp_price),
#                             "tpStopType": "MARK_PRICE",
#                             "tpOrderType": "MARKET",
#                             "tpQty": str(tp_qty),
#                         }
#                     }
#             else:
#                 tp_orders = {
#                     "data": {
#                         "symbol": symbol,
#                         "orderId": o["id"],
#                         "tpPrice": str(tp_price),
#                         "tpStopType": "MARK_PRICE",
#                         "tpOrderType": "MARKET",
#                         "tpQty": str(tp_qty),
#                     }
#                 }
#
#         logger.info(f"[MODIFY ORDER] {sl_orders} {tp_orders}")
#
#         if sl_orders:
#             success = await safe_submit_sl_update(symbol, direction, sl_orders["data"],
#                                                   float(sl_orders["data"]["slPrice"]))
#             if not success:
#                 logger.warning(f"[SL WARNING] SL update failed for {symbol} {direction}")
#
#         if tp_orders:
#             success = await safe_submit_tp_update(symbol, direction, tp_orders["data"],
#                                                   float(tp_orders["data"]["tpPrice"]))
#             if not success:
#                 logger.warning(f"[TP WARNING] TP update failed for {symbol} {direction}")
#
#     except Exception as e:
#         logger.error(f"[MODIFY TP/SL ERROR] Failed for {symbol} {direction}: {e}")


async def place_tp_sl_order_async(symbol, tp_price, sl_price, position_id, tp_qty, qty):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "positionId": position_id,
        "tpPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET",
        "slPrice": str(sl_price),
        "slTriggerType": "MARKET_PRICE",
        "slOrderType": "MARKET",
        "tpQty": str(tp_qty),
        "slQty": str(qty)
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
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"[ORDER FAILED] {e}")
        if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
            logger.error(f"[ORDER FAILED] Response: {e.response.text}")
        return None


async def maybe_reverse_position(symbol: str, new_direction: str, new_qty: float):
    """
    If an opposite position is open, closes it and opens a new one with doubled quantity.
    """
    opposite_direction = "SELL" if new_direction == "BUY" else "BUY"
    existing_state = await get_or_create_symbol_direction_state(symbol, opposite_direction, True)

    if not existing_state or existing_state.get("status") != "OPEN":
        logger.info(f"[REVERSE CHECK] No open {opposite_direction} position for {symbol}. Proceeding normally.")
        return new_qty  # no reversal needed

    logger.info(f"[REVERSAL DETECTED] Closing {opposite_direction} position on {symbol} to open {new_direction}")

    try:
        opposite_position_id = existing_state.get("position_id")
        # Optional: cancel any remaining limit/TP/SL orders
        await cancel_all_new_orders(symbol, opposite_direction, context="reversal")

        # Update old state as CLOSED
        await update_position_state(symbol, opposite_direction, opposite_position_id, {"status": "CLOSED"})

        # Return doubled quantity to use for new entry
        return round(existing_state["total_qty"] + new_qty, 3)

    except Exception as e:
        logger.error(f"[REVERSAL ERROR] Failed to close and flip position for {symbol}: {e}")
        return new_qty


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

    logger.info(f"[ORDER DATA] {order_data}")

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
