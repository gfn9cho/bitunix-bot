import asyncio
import json
from datetime import datetime
import httpx
import secrets
import base64

from modules.logger_config import logger
from modules.redis_client import get_redis
from modules.redis_state_manager import update_position_state
from modules.utils import (
    place_tp_sl_order_async,
    update_tp_quantity,
    update_sl_price,
    generate_get_sign_api
)
from modules.config import BASE_URL, API_KEY, API_SECRET

TP_DISTRIBUTION = [0.7, 0.1, 0.1, 0.1]


async def fetch_bitunix_positions():
    url = f"{BASE_URL}/api/v1/futures/position/get_pending-positions"
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
    timestamp = str(int(datetime.utcnow().timestamp() * 1000))
    sign = generate_get_sign_api(nonce, timestamp, "get", "")

    headers = {
        "api-key": API_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
        "language": "en-US",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"[ORPHAN CHECK] Failed to fetch live positions: {e}")
        return []


async def fetch_pending_tp_sl(symbol: str):
    url = f"{BASE_URL}/api/v1/futures/tpsl/get_pending_orders"
    params = {"symbol": symbol.upper()}
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
    timestamp = str(int(datetime.utcnow().timestamp() * 1000))
    sign = generate_get_sign_api(nonce, timestamp, "get", params)

    headers = {
        "api-key": API_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": sign,
        "language": "en-US",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json().get("data", [])
    except Exception as e:
        logger.error(f"[ORPHAN CHECK] Failed to fetch TP/SL for {symbol}: {e}")
        return []


async def check_orphaned_positions():
    r = get_redis()
    keys = await r.keys("position_state:*")
    logger.info(f"[ORPHAN CHECK] Scanning {len(keys)} keys")

    live_positions = await fetch_bitunix_positions()
    redis_position_ids = set()

    for key in keys:
        state_json = await r.get(key)
        if not state_json:
            continue
        state = json.loads(state_json)
        if state.get("status") == "OPEN":
            redis_position_ids.add(state.get("position_id"))

    for p in live_positions:
        position_id = p["positionId"]
        symbol = p["symbol"]
        direction = "BUY" if p["side"] == 1 else "SELL"
        entry_price = float(p["avgEntryPrice"])
        qty = float(p["positionSize"])

        if position_id not in redis_position_ids:
            logger.warning(f"[ORPHAN REDIS MISS] Rebuilding state for {symbol}-{direction}")

            tp_sl_orders = await fetch_pending_tp_sl(symbol)
            tp_orders = [o for o in tp_sl_orders if o.get("tpPrice") is not None and o["side"] == direction]
            tps = sorted(
                [float(o["price"]) for o in tp_orders],
                reverse=(direction == "SELL")
            )

            sl_order = next(
                (o for o in tp_sl_orders if o.get("tpPrice") is None and o["side"] == direction),
                None
            )
            sl_price = float(sl_order["price"]) if sl_order else None

            state = {
                "symbol": symbol,
                "direction": direction,
                "position_id": position_id,
                "entry_price": entry_price,
                "total_qty": qty,
                "step": 0,
                "status": "OPEN",
                "interval": "5m",
                "created_at": datetime.utcnow().isoformat(),
                "tps": tps,
                "stop_loss": sl_price,
                "tp_orders": [o["orderId"] for o in tp_orders],
                "sl_order_id": sl_order["orderId"] if sl_order else None
            }

            await update_position_state(symbol, direction, position_id, state)

        else:
            redis_state = json.loads(await r.get(f"position_state:{symbol}:{direction}"))

            tp_sl_orders = await fetch_pending_tp_sl(symbol)
            tp_orders = [o for o in tp_sl_orders if o.get("tpPrice") is not None and o["side"] == direction]
            sl_order = next((o for o in tp_sl_orders if o.get("tpPrice") is None and o["side"] == direction), None)

            expected_step = redis_state.get("step", 0)
            expected_tps = redis_state.get("tps", [])[expected_step:]
            expected_qty = float(redis_state.get("total_qty", 0))
            expected_sl_price = float(redis_state.get("stop_loss") or 0)

            tp_mismatch = False
            if len(tp_orders) != len(expected_tps):
                tp_mismatch = True
            else:
                for i, o in enumerate(tp_orders):
                    actual_price = float(o["price"])
                    actual_qty = float(o["quantity"])
                    expected_price = expected_tps[i]
                    if abs(actual_price - expected_price) > 0.01 or abs(actual_qty - expected_qty * 0.1) > 0.01:
                        tp_mismatch = True
                        break

            sl_mismatch = False
            if sl_order:
                actual_sl_price = float(sl_order["price"])
                actual_sl_qty = float(sl_order["quantity"])
                if abs(actual_sl_price - expected_sl_price) > 0.01 or abs(actual_sl_qty - expected_qty) > 0.01:
                    sl_mismatch = True

            if not tp_orders or not sl_order:
                logger.warning(f"[ORPHAN TPSL MISSING] Placing TP/SL for {symbol}-{direction}")
                new_qty = expected_qty
                sl_order_id = await place_tp_sl_order_async(symbol, tp_price=None, sl_price=expected_sl_price,
                                                            position_id=position_id, tp_qty=None, qty=new_qty)
                if sl_order_id:
                    redis_state["sl_order_id"] = sl_order_id
                redis_state["tp_orders"] = {}
                for i in range(0, 4):
                    tp_price = expected_tps[i] if i < len(expected_tps) else None
                    tp_ratio = TP_DISTRIBUTION[i]
                    tp_qty = round(new_qty * tp_ratio, 3)
                    logger.info(f"[INITIAL TP/SL SET] {symbol} {direction} TP{i + 1} {tp_price}, tpQty: {tp_qty}")
                    if tp_price:
                        order_id = await place_tp_sl_order_async(symbol, tp_price=tp_price, sl_price=None,
                                                                 position_id=position_id, tp_qty=tp_qty, qty=new_qty)
                        if order_id:
                            redis_state["tp_orders"][f"TP{i + 1}"] = order_id
                    else:
                        logger.info(
                            f"[INITIAL TP/SL NOT SET FOR]: {symbol} {direction} TP{i + 1} {tp_price}, tpQty: {tp_qty}")
            else:
                if tp_mismatch:
                    logger.warning(f"[ORPHAN TP MISMATCH] Updating TP for {symbol}-{direction}")
                    for i, o in enumerate(tp_orders):
                        order_id = o["orderId"]
                        new_tp_price = expected_tps[i]
                        new_tp_qty = round(expected_qty * 0.1, 3)  # adjust if you use TP_DISTRIBUTION
                        await update_tp_quantity(order_id, symbol, new_tp_qty, new_tp_price)

                if sl_mismatch:
                    logger.warning(f"[ORPHAN SL MISMATCH] Updating SL for {symbol}-{direction}")
                    order_id = sl_order["orderId"]
                    await update_sl_price(order_id, direction, symbol, expected_sl_price, expected_qty)

            await update_position_state(symbol, direction, position_id, redis_state)

    return f"[ORPHAN CHECK] Completed"
