import os
import json
import re
import time
import hmac
import hashlib
import requests
import base64
import secrets
from datetime import datetime
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger

LOSS_LOG_FILE = "daily_loss_log.json"

def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")

def read_loss_log():
    if not os.path.exists(LOSS_LOG_FILE):
        return {}
    try:
        with open(LOSS_LOG_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def write_loss_log(log):
    with open(LOSS_LOG_FILE, 'w') as f:
        json.dump(log, f)

def update_loss(amount):
    log = read_loss_log()
    today = get_today()
    if today not in log:
        log[today] = {"profit": 0, "loss": 0}
    log[today]["loss"] += amount
    write_loss_log(log)

def update_profit(amount):
    log = read_loss_log()
    today = get_today()
    if today not in log:
        log[today] = {"profit": 0, "loss": 0}
    log[today]["profit"] += amount
    write_loss_log(log)

def get_today_loss():
    log = read_loss_log()
    today = get_today()
    return log.get(today, {}).get("loss", 0)

def get_today_net_loss():
    log = read_loss_log()
    today = get_today()
    profit = log.get(today, {}).get("profit", 0)
    loss = log.get(today, {}).get("loss", 0)
    return max(loss - profit, 0)

def modify_tp_sl_order(symbol, tp_price, sl_price, order_id, qty):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "tpTriggerPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET",
        "slTriggerPrice": str(sl_price),
        "slTriggerType": "MARKET_PRICE",
        "slOrderType": "MARKET",
        "orderId": order_id,
        "vol": str(qty)
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
        response = requests.post(f"{BASE_URL}/api/v1/futures/tpsl/modify_order", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[TP/SL MODIFY SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[TP/SL MODIFY FAILED] {e}")
        if e.response is not None:
            logger.error(f"[TP/SL MODIFY FAILED] Response: {e.response.text}")
        return None

def place_tp_sl_order(symbol, tp_price, position_id, qty):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "positionId": position_id,
        "tpTriggerPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET",
        "vol": str(qty)
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
        response = requests.post(f"{BASE_URL}/api/v1/futures/tpsl/place_order", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[TP/SL ORDER SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[TP/SL ORDER FAILED] {e}")
        if e.response is not None:
            logger.error(f"[TP/SL ORDER FAILED] Response: {e.response.text}")
        return None
