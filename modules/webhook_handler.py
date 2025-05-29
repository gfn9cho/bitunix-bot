import asyncio
import time
from datetime import datetime

from quart import request, jsonify

from modules.logger_config import logger, error_logger
from modules.loss_tracking import is_daily_loss_limit_exceeded
from modules.price_feed import validate_and_process_signal
# from modules.postgres_state_manager import get_or_create_symbol_direction_state, update_position_state
from modules.redis_state_manager import get_or_create_symbol_direction_state, \
                                        update_position_state, delete_position_state
from modules.utils import parse_signal, place_order, is_duplicate_signal, maybe_reverse_position, evaluate_signal_received
from modules.signal_limiter import should_accept_signal


async def webhook_handler(symbol):
    raw_data = await request.get_data(as_text=True)
    logger.info(f"Raw webhook data: {raw_data}")
    logger.info(f"Request headers: {dict(request.headers)}")

    try:
        if is_daily_loss_limit_exceeded():
            logger.warning("Max daily loss reached. Blocking trades.")
            return jsonify({"status": "blocked", "message": "Max daily loss reached"}), 403

        try:
            data = await request.get_json(force=True)
            message = data.get("message", "")
            alert_name = data.get("alert_name", "unknown")
            payload_symbol = data.get("symbol")
        except Exception:
            data = {}
            message = raw_data.strip()
            alert_name = "unknown"
            payload_symbol = None

        symbol_qty = (payload_symbol or symbol or "BTCUSDT").upper()
        if "_" in symbol_qty:
            parts = symbol_qty.split("_")
            symbol = parts[0]
            override_qty = float(parts[1]) if len(parts) > 1 else None
            interval = parts[2].lower() if len(parts) > 2 else "1m"
        else:
            symbol = symbol_qty
            override_qty = None
            interval = "1m"

        logger.info(f"Parsed data: {data}")
        logger.info(f"Received alert '{alert_name}' for {symbol}: {message}")
        logger.info(f"[SYMBOL_QTY] Using override_qty={override_qty} for all order placements")

        parsed = parse_signal(message)
        direction = parsed["direction"].upper()
        signal_time = datetime.utcnow()
        entry = parsed["entry_price"]

        async def process_trade(market_qty_revised):
            # Create a new pending position or get a open position if exists.
            if await is_duplicate_signal(symbol, direction):
                logger.warning(f"[DUPLICATE] Signal skipped for {symbol}-{direction}")
                return

            if not await should_accept_signal(symbol, direction, interval):
                return jsonify({"error": "Signal rate limit exceeded"})

            logger.info(f"[PROCESS TRADE]: CREATE STATE - {symbol} {direction} {entry}")
            state = await get_or_create_symbol_direction_state(symbol, direction)
            position_status = state.get("status")
            position_step = state.get("step")
            position_sl = state.get("stop_loss")
            new_signal_sl = parsed["stop_loss"]
            sl_threshold = new_signal_sl <= position_sl if direction == "BUY" else new_signal_sl >= position_sl
            if (position_step == 0 and position_status == "OPEN" and sl_threshold) \
                    or position_status == "PENDING":

                state["tps"] = parsed["take_profits"]
                state["entry_price"] = parsed["entry_price"]
                state["step"] = 0
                state["qty_distribution"] = [0.7, 0.1, 0.1, 0.1]
                state["stop_loss"] = new_signal_sl
                state["created_at"] = datetime.utcnow().isoformat()
                state["interval"] = interval
                state["signal_time"] = signal_time

                zone_start, zone_bottom = parsed["accumulation_zone"]
                logger.info(f"[ACC ZONES]: {zone_start}: {zone_bottom}")
                zone_middle = (zone_start + zone_bottom) / 2
                # tp1 = parsed["take_profits"][0]
                # sl = parsed["stop_loss"]

                # market_qty = override_qty if override_qty else 10
                logger.info(
                    f"[ORDER SUBMIT] Market order: symbol={symbol}, direction={direction}, \
                        price={entry}, qty_revised={market_qty_revised} base_qty={override_qty}")
                retries = 3

                for attempt in range(retries):
                    logger.info(f"[TEST TRACE] Reverse? {state}, Revised Qty: {market_qty_revised}")
                    response = await place_order(
                        symbol=symbol,
                        side=direction,
                        price=entry,
                        qty=market_qty_revised,
                        order_type="MARKET",
                        private=True
                    )
                    if response and response.get("code", -1) == 0:
                        await update_position_state(symbol, direction, '', state)
                        logger.info(f"[LOSS TRACKING] Awaiting TP or SL to update net P&L for {symbol}")
                        logger.info(f"[TEST TRACE] ORDER RESPONSE: {response}")
                        break
                    error_logger.error(
                        f"[ORDER FAILURE] Attempt {attempt + 1}/{retries} - symbol={symbol}, \
                            direction={direction}, response={response}")
                    time.sleep(1)

                logger.info(
                    f"[ORDER SUBMIT] Limit order 1: symbol={symbol}, direction={direction}, \
                        price={zone_start}, qty={market_qty_revised or 10}")
                await place_order(symbol=symbol, side=direction, price=zone_start, qty=market_qty_revised or 10,
                                  order_type="LIMIT")

                logger.info(
                    f"[ORDER SUBMIT] Limit order 2: symbol={symbol}, direction={direction}, \
                        price={zone_middle}, qty={market_qty_revised or 10}")
                await place_order(symbol=symbol, side=direction, price=zone_middle, qty=market_qty_revised or 10,
                                  order_type="LIMIT")

                bottom_qty = (market_qty_revised * 2 if market_qty_revised else 20)
                logger.info(
                    f"[ORDER SUBMIT] Limit order 3: symbol={symbol}, direction={direction}, \
                        price={zone_bottom}, qty={bottom_qty}")
                await place_order(symbol=symbol, side=direction, price=zone_bottom, qty=bottom_qty, order_type="LIMIT")
            else:
                # await delete_position_state(symbol, direction)
                logger.info(f"[TRADE SKIP]: As the existing position is open and in TP stage")

        async def wrapped_process():
            await validate_and_process_signal(
                symbol, entry, direction, interval, signal_time, override_qty,  process_trade
            )

        asyncio.create_task(wrapped_process())

        return jsonify({
            "status": "parsed",
            "symbol": symbol,
            "direction": direction,
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.exception("Error in webhook handler")
        return jsonify({"status": "error", "message": str(e)}), 500
