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

#LOSS_LOG_FILE = "daily_loss_log.json"

def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")

# def read_loss_log():
#     if not os.path.exists(LOSS_LOG_FILE):
#         return {}
#     try:
#         with open(LOSS_LOG_FILE, 'r') as f:
#             return json.load(f)
#     except json.JSONDecodeError:
#         return {}

# def write_loss_log(log):
#     with open(LOSS_LOG_FILE, 'w') as f:
#         json.dump(log, f)

# def update_loss(amount):
#     log = read_loss_log()
#     today = get_today()
#     if today not in log:
#         log[today] = {"profit": 0, "loss": 0}
#     log[today]["loss"] += amount
#     write_loss_log(log)

# def update_profit(amount):
#     log = read_loss_log()
#     today = get_today()
#     if today not in log:
#         log[today] = {"profit": 0, "loss": 0}
#     log[today]["profit"] += amount
#     write_loss_log(log)

# def get_today_loss():
#     log = read_loss_log()
#     today = get_today()
#     return log.get(today, {}).get("loss", 0)
#
# def get_today_net_loss():
#     log = read_loss_log()
#     today = get_today()
#     profit = log.get(today, {}).get("profit", 0)
#     loss = log.get(today, {}).get("loss", 0)
#     return max(loss - profit, 0)

def generate_get_sign_api(nonce, timestamp, method, data):
    query_params = ""
    body = ""
    if data:
        if method.lower() == "get":
            data = {k: v for k, v in data.items() if v is not None}
            query_params = '&'.join([f"{k}={v}" for k, v in sorted(data.items())])
            query_params = re.sub(r'[^a-zA-Z0-9]', '', query_params)
        if method.lower() == "post":
            # body = str(data).replace(" ", "")
            body = str(data)

    digest_input = nonce + timestamp + API_KEY + query_params + body
    # print(f"digest_input={digest_input}")
    digest = hashlib.sha256(digest_input.encode()).hexdigest()
    # print(f"digest={digest}")

    sign_input = digest + API_SECRET
    # print(f"sign_input={sign_input}")
    sign = hashlib.sha256(sign_input.encode()).hexdigest()

    return sign


def modify_tp_sl_order(symbol, tp_price, sl_price, position_id, tp_qty, qty):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "positionId": position_id,
        "tpPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET",
        "slPrice": str(sl_price),
        "slTriggerType": "MARKET_PRICE",
        "slOrderType": "MARKET",
        "tpQty": str(tp_qty),
        "slQty": str(qty)
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
        response = requests.post(f"{BASE_URL}/api/v1/futures/tpsl/position/modify_order", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"[TP/SL MODIFY SUCCESS] {str(body_json)}")
        logger.info(f"[TP/SL MODIFY SUCCESS] {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"[TP/SL MODIFY FAILED] {e}")
        if e.response is not None:
            logger.error(f"[TP/SL MODIFY FAILED] Response: {e.response.text}")
        return None

def place_tp_sl_order(symbol, tp_price, sl_price, position_id, tp_qty, qty):
    timestamp = str(int(time.time() * 1000))
    nonce = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')

    order_data = {
        "symbol": symbol,
        "positionId": position_id,
        "tpPrice": str(tp_price),
        "tpTriggerType": "MARKET_PRICE",
        "tpOrderType": "MARKET",
        "slPrice": str(sl_price),
        "slTriggerType": "MARKET_PRICE",
        "slOrderType": "MARKET",
        "tpQty": str(tp_qty),
        "slQty": str(qty)
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
        "clientId": timestamp,
    }

    if reduce_only:
        order_data["reduceOnly"] = True
        order_data["tradeSide"] = "CLOSE"
    if tp:
        order_data["tpPrice"] = str(tp)
        order_data["tpOrderType"] = "MARKET"
        order_data["tpStopType"] = "MARK_PRICE"
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
    logger.info(f"[tps]: {tps}")
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

def cancel_all_new_orders(symbol):
    try:
        # Step 1: Prepare authentication and GET headers (query param-based request)
        method = "get"
        data = {"symbol": symbol}
        random_bytes = secrets.token_bytes(32)
        nonce = base64.b64encode(random_bytes).decode('utf-8')
        timestamp = str(int(time.time() * 1000))
        signature = generate_get_sign_api(nonce, timestamp,  method, data)

        headers = {
            "api-key": API_KEY,
            "sign": signature,
            "nonce": nonce,
            "timestamp": timestamp,
            "language": "en-US",
            "Content-Type": "application/json"
        }

        # Step 2: GET pending orders
        response = requests.get(
            f"{BASE_URL}/api/v1/futures/trade/get_pending_orders",
            headers=headers,
            params=data
        )
        response.raise_for_status()
        orders = response.json().get("data", {}).get("list", [])
        new_orders = [o["orderId"] for o in orders if o.get("orderStatus") == "NEW"]

        if not new_orders:
            logger.info(f"No NEW orders found for {symbol}")
            return

        # Step 3: Cancel the orders
        cancel_payload = {
            "symbol": symbol,
            "orderIds": new_orders
        }

        cancel_nonce = base64.b64encode(secrets.token_bytes(32)).decode()
        cancel_ts = str(int(time.time() * 1000))
        cancel_body = json.dumps(cancel_payload, separators=(',', ':'))
        cancel_digest = hashlib.sha256((cancel_nonce + cancel_ts + API_KEY + cancel_body).encode()).hexdigest()
        cancel_sign = hashlib.sha256((cancel_digest + API_SECRET).encode()).hexdigest()

        cancel_headers = {
            "api-key": API_KEY,
            "sign": cancel_sign,
            "nonce": cancel_nonce,
            "timestamp": cancel_ts,
            "Content-Type": "application/json"
        }

        cancel_response = requests.post(
            f"{BASE_URL}/api/v1/futures/trade/cancel_orders",
            headers=cancel_headers,
            data=cancel_body
        )
        cancel_response.raise_for_status()
        logger.info(f"[ORDER CANCEL SUCCESS] {symbol}: {new_orders}")

    except Exception as e:
        logger.error(f"[CANCEL ORDERS FAILED] {symbol}: {str(e)}")

