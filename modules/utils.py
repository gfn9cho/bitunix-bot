import os
import json
import re
from datetime import datetime
from modules.logger_config import logger
from modules.config import LOSS_LOG_FILE


import os
#LOSS_LOG_FILE = os.getenv("LOSS_LOG_FILE", "/var/data/daily_loss_log.json")
import time
import hmac
import hashlib
import requests
import json
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger

import time
import hmac
import hashlib
import requests
import json
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger

import time
import hmac
import hashlib
import requests
import json
from modules.config import API_KEY, API_SECRET, BASE_URL
from modules.logger_config import logger
import random
import base64
import secrets

def place_order(symbol, side, price, qty, order_type="LIMIT", leverage=20, tp=None, sl=None):
    timestamp = str(int(time.time() * 1000))
    random_bytes = secrets.token_bytes(32)
    nonce = base64.b64encode(random_bytes).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "qty": str(qty),
        "price": str(price),
        "side": side.upper(),  # BUY or SELL
        "orderType": order_type.upper(),  # LIMIT or MARKET
        "tradeSide": "OPEN",
        "effect": "GTC",
        "clientId": timestamp
    }

    # Optional TP/SL fields
    if tp:
        order_data.update({
            "tpPrice": str(tp),
            "tpStopType": "MARK_PRICE",
            "tpOrderType": "MARKET"
        })
    if sl:
        order_data.update({
            "slPrice": str(sl),
            "slStopType": "MARK_PRICE",
            "slOrderType": "MARKET"
        })

    # Remove any None fields
    order_data = {k: v for k, v in order_data.items() if v is not None}
    body_json = json.dumps(order_data, separators=(',', ':'))

    # Log the payload
    logger.info(f"[ORDER DATA] {order_data}")

    #pre_sign = f"{timestamp}{nonce}{body_json}"
    #signature = hmac.new(API_SECRET.encode('utf-8'), pre_sign.encode('utf-8'), hashlib.sha256).hexdigest()
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
        response = requests.post(f"{BASE_URL}/api/v1/futures/trade/place_order", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[ORDER SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[ORDER FAILED] {e}")
        if e.response is not None:
            logger.error(f"[ORDER FAILED] Response: {e.response.text}")
        return None



def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")

def read_loss_log():
    if not os.path.exists(LOSS_LOG_FILE):
        return {}
    try:
        with open(LOSS_LOG_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.warning(f"{LOSS_LOG_FILE} is empty or invalid. Resetting.")
        with open(LOSS_LOG_FILE, "w") as wf:
            json.dump({}, wf)
    return {}


def write_loss_log(log):
    with open(LOSS_LOG_FILE, 'w') as f:
        json.dump(log, f)

def update_loss(amount):
    log = read_loss_log()
    today = get_today()
    log[today] = log.get(today, 0) + amount
    write_loss_log(log)

def get_today_loss():
    log = read_loss_log()
    today = get_today()
    today_loss = log.get(today, 0)
    logger.info(f"[LOSS CHECK] Path: {LOSS_LOG_FILE}, Log: {log}, Today's Loss: {today_loss}")
    return today_loss


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
