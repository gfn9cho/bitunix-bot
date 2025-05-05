from flask import request, jsonify
from datetime import datetime
import logging
from modules.utils import parse_signal, get_today_loss, place_order
from modules.config import MAX_DAILY_LOSS
from modules.logger_config import logger, error_logger, trade_logger, reversal_logger
from modules.state import position_state, save_position_state


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

        symbol = (payload_symbol or symbol or "BTCUSDT").upper()
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
        place_order(symbol, direction, entry, 10, order_type="MARKET", tp=tp1, sl=sl)
        # Limit orders
        #place_order(symbol, direction, zone_start, 10, order_type="LIMIT", tp=tp1, sl=sl)
        #place_order(symbol, direction, zone_middle, 10, order_type="LIMIT", tp=tp1, sl=sl)
        #place_order(symbol, direction, zone_bottom, 20, order_type="LIMIT", tp=tp1, sl=sl)

        return jsonify({
            "status": "parsed",
            "symbol": symbol,
            "direction": direction,
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.exception("Error in webhook handler")
        return jsonify({"status": "error", "message": str(e)}), 500