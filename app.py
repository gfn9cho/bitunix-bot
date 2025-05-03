from flask import Flask, request, jsonify
import hmac, hashlib, time, requests, re, os, json, logging
from datetime import datetime

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BASE_URL = 'https://fapi.bitunix.com'

POSITION_SIZE = 10  # dollars per entry
LEVERAGE = 20
MAX_DAILY_LOSS = 100  # Max loss in USD per day

LOSS_LOG_FILE = "daily_loss_log.json"

def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")

def read_loss_log():
    if not os.path.exists(LOSS_LOG_FILE):
        return {}
    with open(LOSS_LOG_FILE, 'r') as f:
        return json.load(f)

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
    return log.get(get_today(), 0)

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

def generate_signature(secret, timestamp, nonce, body_json):
    pre_sign = f"{timestamp}{nonce}{body_json}"
    return hmac.new(secret.encode('utf-8'), pre_sign.encode('utf-8'), hashlib.sha256).hexdigest()

def place_order(symbol, side, price, qty, stop_loss, tp1):
    nonce = str(int(time.time() * 1000))
    timestamp = nonce

    body = {
        "symbol": symbol,
        "side": side,
        "price": str(price),
        "qty": str(qty),
        "orderType": "LIMIT",
        "effect": "GTC",
        "reduceOnly": False,
        "tpPrice": str(tp1),
        "tpStopType": "MARK_PRICE",
        "tpOrderType": "LIMIT",
        "tpOrderPrice": str(tp1),
        "slPrice": str(stop_loss),
        "slStopType": "MARK_PRICE",
        "slOrderType": "LIMIT",
        "slOrderPrice": str(stop_loss)
    }

    body_json = json.dumps(body, separators=(',', ':'))
    signature = generate_signature(API_SECRET, timestamp, nonce, body_json)

    headers = {
        "api-key": API_KEY,
        "sign": signature,
        "nonce": nonce,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(f"{BASE_URL}/api/v1/futures/trade/place_order", headers=headers, data=body_json)
        response.raise_for_status()
        logger.info(f"Order placed: {body}")
        return response.json()
    except Exception as e:
        logger.error(f"Error placing order for entry {price}: {str(e)}")
        return {"status": "error", "message": str(e)}

def calculate_zone_entries(acc_zone):
    top, bottom = acc_zone
    mid = (top + bottom) / 2
    return [top, mid, bottom]

def calculate_quantities(prices, direction):
    multipliers = [10, 10, 20]  # $ amounts
    return [round(m / p, 6) for m, p in zip(multipliers, prices)]

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if get_today_loss() >= MAX_DAILY_LOSS:
            logger.warning("Max daily loss reached. Blocking trades.")
            return jsonify({"status": "blocked", "message": "Max daily loss reached"}), 403

        data = request.get_json()
        message = data.get("message")
        symbol = data.get("symbol", "BTCUSDT").upper()
        logger.info(f"Received signal for {symbol}: {message}")

        parsed = parse_signal(message)

        direction = parsed["direction"]
        acc_entries = calculate_zone_entries(parsed["accumulation_zone"])
        quantities = calculate_quantities(acc_entries, direction)

        tp1 = parsed["take_profits"][0]
        stop_loss = parsed["stop_loss"]

        results = []
        for i in range(3):
            qty = quantities[i]
            entry = acc_entries[i]

            res = place_order(symbol, direction, entry, qty, stop_loss, tp1)
            results.append(res)

            potential_loss = POSITION_SIZE if i < 2 else 2 * POSITION_SIZE
            update_loss(potential_loss)

        return jsonify({"status": "success", "orders": results})

    except Exception as e:
        logger.exception("Error in webhook handler")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
