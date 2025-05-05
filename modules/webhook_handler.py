from flask import request, jsonify
from datetime import datetime
import logging
from modules.utils import parse_signal, get_today_loss, place_order
from modules.config import MAX_DAILY_LOSS
from modules.logger_config import logger, error_logger, trade_logger, reversal_logger
from modules.state import position_state, save_position_state
import time


def webhook_handler(symbol):
    raw_data = request.get_data(as_text=True)
    logger.info(f"Raw webhook data: {raw_data}")
    logger.info(f"Request headers: {dict(request.headers)}")

    try:
        today_loss = get_today_loss()
        logger.info(f"[LOSS GUARD] Today: {today_loss} / Limit: {MAX_DAILY_LOSS}")

        if today_loss >= MAX_DAILY_LOSS:
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
            symbol, qty_str = symbol_qty.split("_", 1)
            try:
                override_qty = float(qty_str)
                logger.info(f"[SYMBOL_QTY] Parsed symbol: {symbol}, Custom qty override: {override_qty}")
            except ValueError:
                override_qty = None
                logger.warning(f"[SYMBOL_QTY] Invalid quantity format in symbol: {symbol_qty}, ignoring override.")
        else:
            symbol = symbol_qty
            override_qty = None
            logger.info(f"[SYMBOL_QTY] Using default quantity. Parsed symbol: {symbol}")

        logger.info(f"Parsed data: {data}")
        logger.info(f"Received alert '{alert_name}' for {symbol}: {message}")

        parsed = parse_signal(message)

        position_state[symbol] = {
            "step": 0,
            "tps": parsed["take_profits"],
            "direction": parsed["direction"],
            "filled_orders": set()
        }
        save_position_state()

        # Execute trades
        direction = parsed["direction"]
        entry = parsed["entry_price"]
        zone_start, zone_bottom = parsed["accumulation_zone"]
        zone_middle = (zone_start + zone_bottom) / 2
        tp1 = parsed["take_profits"][0]
        sl = parsed["stop_loss"]

        # Market order
        market_qty = override_qty if override_qty else 10
        place_order(symbol, direction, entry, market_qty, order_type="MARKET", tp=tp1, sl=sl)
        retries = 3
        for attempt in range(retries):
            response = place_order(...)
            if response and response.get("code", -1) == 0:
                break
            error_logger.error(...)
            time.sleep(1)

        # Limit orders
        place_order(symbol, direction, zone_start, override_qty or 10, order_type="LIMIT", tp=tp1, sl=sl)
        place_order(symbol, direction, zone_middle, override_qty or 10, order_type="LIMIT", tp=tp1, sl=sl)
        place_order(symbol, direction, zone_bottom, (override_qty * 2 if override_qty else 20), order_type="LIMIT", tp=tp1, sl=sl)

        return jsonify({
            "status": "parsed",
            "symbol": symbol,
            "direction": direction,
            "market_qty": market_qty,
            "entry": entry,
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.exception("Error in webhook handler")
        return jsonify({"status": "error", "message": str(e)}), 500