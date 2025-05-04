from flask import request, jsonify
from datetime import datetime
import hmac, hashlib, time, requests, json, logging, re
from modules.utils import parse_signal, calculate_zone_entries, calculate_quantities, update_loss, get_today_loss
from modules.config import API_KEY, API_SECRET, BASE_URL, POSITION_SIZE, LEVERAGE, MAX_DAILY_LOSS
from modules.logger_config import logger, trade_logger, error_logger, reversal_logger
from modules.state import save_position_state

from modules.state import position_state

def webhook_handler(symbol):
    raw_data = request.get_data(as_text=True)
    logger.info(f"Raw webhook data: {raw_data}")
    #logger.info(f"Request headers: {dict(request.headers)}")

    try:
        if get_today_loss() >= MAX_DAILY_LOSS:
            logger.info(f"MAX_DAILY_LOSS = {MAX_DAILY_LOSS}")
            logger.info(f"get_today_loss() = {get_today_loss()}")
            logger.warning("Max daily loss reached. Blocking trades.")
            save_position_state()
        return jsonify({"status": "blocked", "message": "Max daily loss reached", "max daily loss": {MAX_DAILY_LOSS}, "todays loss": {get_today_loss()}}), 403

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
        # TODO: Add order execution logic
        save_position_state()
        return jsonify({
            "status": "parsed",
            "symbol": symbol,
            "direction": parsed["direction"],
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.exception("Error in webhook handler")
        save_position_state()
        return jsonify({"status": "error", "message": str(e)}), 500
