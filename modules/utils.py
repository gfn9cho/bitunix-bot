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

def modify_tp_sl_order(symbol, tp_price, sl_price):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "tpTriggerPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET"
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
        response = requests.post(f"{BASE_URL}/api/v1/futures/position/modify_tp_sl", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[TP/SL MODIFY SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[TP/SL MODIFY FAILED] {e}")
        if e.response is not None:
            logger.error(f"[TP/SL MODIFY FAILED] Response: {e.response.text}")
        return None


def place_tp_sl_order(symbol, tp_price):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "tpTriggerPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET"
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
        response = requests.post(f"{BASE_URL}/api/v1/futures/position/place_tp_sl", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[TP/SL ORDER SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[TP/SL ORDER FAILED] {e}")
        if e.response is not None:
            logger.error(f"[TP/SL ORDER FAILED] Response: {e.response.text}")
        return None

def place_order(symbol, side, price, qty, order_type="LIMIT", leverage=20, tp=None, sl=None, private=True, reduce_only=False):
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
        "clientId": timestamp
    }

    if reduce_only:
        order_data["reduceOnly"] = True
        order_data["tradeSide"] = "CLOSE"

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
        response = requests.post(f"{BASE_URL}/api/v1/futures/trade/place_order", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[ORDER SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[ORDER FAILED] {e}")
        if e.response is not None:
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
