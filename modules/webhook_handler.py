from flask import request, jsonify
from datetime import datetime
from modules.utils import parse_signal, place_order
from modules.logger_config import logger, error_logger
from modules.postgres_state_manager import get_or_create_symbol_direction_state, update_position_state
from modules.loss_tracking import  is_daily_loss_limit_exceeded
from modules.price_feed import validate_and_process_signal
import time
import asyncio
import threading

def webhook_handler(symbol):
    raw_data = request.get_data(as_text=True)
    logger.info(f"Raw webhook data: {raw_data}")
    logger.info(f"Request headers: {dict(request.headers)}")

    try:
        if is_daily_loss_limit_exceeded():
            logger.warning("Max daily loss reached. Blocking trades.")
            return jsonify({"status": "blocked", "message": "Max daily loss reached"}), 403

        try:
            data = request.get_json(force=True)
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
            interval = parts[2] if len(parts) > 2 else "1m"
        else:
            symbol = symbol_qty
            override_qty = None
            interval = "1m"

        logger.info(f"Parsed data: {data}")
        logger.info(f"Received alert '{alert_name}' for {symbol}: {message}")
        logger.info(f"[SYMBOL_QTY] Using override_qty={override_qty} for all order placements")

        parsed = parse_signal(message)

        direction = parsed["direction"].upper()
        # Temporary pre-position state (position_id will be set later by websocket)
        state = get_or_create_symbol_direction_state(symbol, direction, True)
        state["tps"] = parsed["take_profits"]
        state["entry_price"] = parsed["entry_price"]
        state["step"] = 0
        state["qty_distribution"] = [0.7, 0.1, 0.1, 0.1]
        state["stop_loss"] = parsed["stop_loss"]


        entry = parsed["entry_price"]
        zone_start, zone_bottom = parsed["accumulation_zone"]
        logger.info(f"[ACC ZONES]: {zone_start}: {zone_bottom}")
        zone_middle = (zone_start + zone_bottom) / 2
        # tp1 = parsed["take_profits"][0]
        # sl = parsed["stop_loss"]

        signal_time = datetime.utcnow()

        async def process_trade():
            market_qty = override_qty if override_qty else 10
            logger.info(f"[ORDER SUBMIT] Market order: symbol={symbol}, direction={direction}, price={entry}, qty={market_qty}")
            retries = 3
            for attempt in range(retries):
                response = place_order(
                    symbol=symbol,
                    side=direction,
                    price=entry,
                    qty=market_qty,
                    order_type="MARKET",
                    private=True
                )
                if response and response.get("code", -1) == 0:
                    state["temporary"] = False
                    update_position_state(symbol, direction, None, False, state)
                    after_update_state = get_or_create_symbol_direction_state(symbol, direction, False)
                    logger.info(f"[State]:{after_update_state}")
                    logger.info(f"[LOSS TRACKING] Awaiting TP or SL to update net P&L for {symbol}")
                    break
                error_logger.error(f"[ORDER FAILURE] Attempt {attempt + 1}/{retries} - symbol={symbol}, direction={direction}, response={response}")
                time.sleep(1)

            logger.info(f"[ORDER SUBMIT] Limit order 1: symbol={symbol}, direction={direction}, price={zone_start}, qty={override_qty or 10}")
            place_order(symbol=symbol, side=direction, price=zone_start, qty=override_qty or 10, order_type="LIMIT")

            logger.info(f"[ORDER SUBMIT] Limit order 2: symbol={symbol}, direction={direction}, price={zone_middle}, qty={override_qty or 10}")
            place_order(symbol=symbol, side=direction, price=zone_middle, qty=override_qty or 10, order_type="LIMIT")

            bottom_qty = (override_qty * 2 if override_qty else 20)
            logger.info(f"[ORDER SUBMIT] Limit order 3: symbol={symbol}, direction={direction}, price={zone_bottom}, qty={bottom_qty}")
            place_order(symbol=symbol, side=direction, price=zone_bottom, qty=bottom_qty, order_type="LIMIT")

        async def wrapped_process():
            await validate_and_process_signal(
                symbol, entry, direction, interval, signal_time, process_trade
            )

        threading.Thread(target=lambda: asyncio.run(wrapped_process())).start()

        return jsonify({
            "status": "parsed",
            "symbol": symbol,
            "direction": direction,
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.exception("Error in webhook handler")
        return jsonify({"status": "error", "message": str(e)}), 500