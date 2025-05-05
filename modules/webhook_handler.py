from flask import request, jsonify
from datetime import datetime
import logging
from modules.utils import parse_signal, get_today_net_loss, place_order, update_profit, update_loss, place_tp_sl_order
from modules.config import MAX_DAILY_LOSS
from modules.logger_config import logger, error_logger, trade_logger, reversal_logger
from modules.state import position_state, save_position_state
import os
import hmac
import hashlib
import json
import random
import time


def webhook_handler(symbol):
    raw_data = request.get_data(as_text=True)
    logger.info(f"Raw webhook data: {raw_data}")
    logger.info(f"Request headers: {dict(request.headers)}")

    try:
        today_loss = get_today_net_loss()
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
            symbol_part, qty_str = symbol_qty.split("_", 1)
            symbol = symbol_part
            try:
                override_qty = float(qty_str)
            except ValueError:
                override_qty = None
        else:
            symbol = symbol_qty
            override_qty = None

        logger.info(f"Parsed data: {data}")
        logger.info(f"Received alert '{alert_name}' for {symbol}: {message}")
        logger.info(f"[SYMBOL_QTY] Using override_qty={override_qty} for all order placements")

        parsed = parse_signal(message)

        position_state[symbol] = {
            "step": 0,
            "tps": parsed["take_profits"],
            "direction": parsed["direction"],
            "filled_orders": set(),
            "entry_price": parsed["entry_price"],
            "qty_distribution": [0.7, 0.1, 0.1, 0.1]
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
        logger.info(f"[ORDER SUBMIT] Market order: symbol={symbol}, direction={direction}, price={entry}, qty={market_qty}, sl={sl}")

        retries = 3
        for attempt in range(retries):
            response = place_order(
                symbol=symbol,
                side=direction,
                price=entry,
                qty=market_qty,
                order_type="MARKET",
                sl=sl,
                private=True
            )

            # Place initial TP order to close 70% at TP1
            tp_qty = round(market_qty * 0.7, 6)
            logger.info(f"[TP SL SETUP] Setting TP1 at {tp1} for 70% of market position")
            place_tp_sl_order(symbol=symbol, tp_price=tp1)
            logger.info(f"[TP1 READY] TP SL order placed for {symbol} at price {tp1}")
            logger.info(f"[TP LOGGED] Setting initial TP, waiting to log P&L when filled")

            if response and response.get("code", -1) == 0:
                logger.info(f"[LOSS TRACKING] Awaiting TP or SL to update net P&L for {symbol}")
                break
            error_logger.error(f"[ORDER FAILURE] Attempt {attempt + 1}/{retries} - symbol={symbol}, direction={direction}, response={response}")
            time.sleep(1)

        # Limit orders
        logger.info(f"[ORDER SUBMIT] Limit order 1: symbol={symbol}, direction={direction}, price={zone_start}, qty={override_qty or 10}, sl={sl}")
        place_order(symbol=symbol, side=direction, price=zone_start, qty=override_qty or 10, order_type="LIMIT", sl=sl)

        logger.info(f"[ORDER SUBMIT] Limit order 2: symbol={symbol}, direction={direction}, price={zone_middle}, qty={override_qty or 10}, sl={sl}")
        place_order(symbol=symbol, side=direction, price=zone_middle, qty=override_qty or 10, order_type="LIMIT", sl=sl)

        bottom_qty = (override_qty * 2 if override_qty else 20)
        logger.info(f"[ORDER SUBMIT] Limit order 3: symbol={symbol}, direction={direction}, price={zone_bottom}, qty={bottom_qty}, sl={sl}")
        place_order(symbol=symbol, side=direction, price=zone_bottom, qty=bottom_qty, order_type="LIMIT", sl=sl)

        return jsonify({
            "status": "parsed",
            "symbol": symbol,
            "direction": direction,
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.exception("Error in webhook handler")
        return jsonify({"status": "error", "message": str(e)}), 500
