from flask import Flask, request, jsonify
import hmac, hashlib, time, requests, re, os, json
from datetime import datetime

app = Flask(__name__)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BASE_URL = 'https://api.bitunix.com'

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
    direction = 'sell' if 'short' in signal_type else 'buy'

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


def generate_signature(params, secret):
    sorted_params = ''.join([str(params[k]) for k in sorted(params)])
    first_hash = hashlib.sha256(sorted_params.encode()).hexdigest()
    return hashlib.sha256((first_hash + secret).encode()).hexdigest()


def place_order(symbol, side, price, quantity, stop_loss, tp):
    timestamp = time.strftime('%Y%m%d%H%M%S')
    nonce = hashlib.md5(str(time.time()).encode()).hexdigest()[:32]

    params = {
        "apiKey": API_KEY,
        "nonce": nonce,
        "timestamp": timestamp
    }

    body = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "MARKET",
        "price": price,
        "quantity": quantity,
        "leverage": LEVERAGE,
        "stopLossPrice": stop_loss,
        "takeProfitPrice": tp
    }

    sign = generate_signature({**params, **body}, API_SECRET)
    params['sign'] = sign

    headers = {'Content-Type': 'application/json'}
    response = requests.post(f"{BASE_URL}/api/v1/order", headers=headers, json=body, params=params)
    return response.json()


def update_stop_loss_to_entry(symbol, order_id, new_stop_price):
    timestamp = time.strftime('%Y%m%d%H%M%S')
    nonce = hashlib.md5(str(time.time()).encode()).hexdigest()[:32]

    params = {
        "apiKey": API_KEY,
        "nonce": nonce,
        "timestamp": timestamp
    }

    body = {
        "orderId": order_id,
        "stopLossPrice": new_stop_price
    }

    sign = generate_signature({**params, **body}, API_SECRET)
    params['sign'] = sign

    headers = {'Content-Type': 'application/json'}
    response = requests.post(f"{BASE_URL}/api/v1/order/update", headers=headers, json=body, params=params)
    return response.json()


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
            return jsonify({"status": "blocked", "message": "Max daily loss reached"}), 403

            print("Raw data received:", request.data, flush=True)
            print("Raw data received:", request.get_json(), flush=True)
            data = request.get_json()

        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "Invalid JSON body"}), 400

        message = data.get("message")
        if not message:
            return jsonify({"status": "error", "message": "Missing 'message' in payload"}), 400

        print("Raw data received:", request.data, flush=True)
        print("Parsed JSON:", request.get_json(), flush=True)
        symbol = data.get("symbol", "BTCUSDT").upper()
        parsed = parse_signal(message)

        direction = parsed["direction"]
        acc_entries = calculate_zone_entries(parsed["accumulation_zone"])
        quantities = calculate_quantities(acc_entries, direction)

        tp1 = parsed["take_profits"][0]
        tp2 = parsed["take_profits"][1] if len(parsed["take_profits"]) > 1 else tp1
        stop_loss = parsed["stop_loss"]
        entry_price = parsed["entry_price"]

        results = []
        for i in range(3):
            qty = quantities[i]
            entry = acc_entries[i]

            #res = place_order(symbol, direction, entry, qty, stop_loss, tp1)
            res = ""
            results.append(res)

            if res.get("status") == "success" and res.get("data"):
                order_id = res["data"].get("orderId")
                if order_id:
                    update_result = update_stop_loss_to_entry(symbol, order_id, entry_price)
                    print(f"Stop loss updated for order {order_id}: {update_result}")
                    print(f"Schedule remaining 30% to TP2 at {tp2}")

            # Assume a worst-case loss for daily limit estimation
            potential_loss = POSITION_SIZE if i < 2 else 2 * POSITION_SIZE
            update_loss(potential_loss)

        return jsonify({"status": "success", "orders": results})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
