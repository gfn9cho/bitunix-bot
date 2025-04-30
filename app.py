from flask import Flask, request, jsonify
import hmac, hashlib, time, requests, re

app = Flask(__name__)

import os
API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
BASE_URL = 'https://api.bitunix.com'

POSITION_SIZE = 10  # dollars per entry
LEVERAGE = 20


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


def calculate_zone_entries(acc_zone):
    top, bottom = acc_zone
    mid = (top + bottom) / 2
    return [top, mid, bottom]


def calculate_quantities(prices, direction):
    # $10 at top, $10 at mid, $20 at bottom (in asset amount based on price)
    multipliers = [10, 10, 20]
    return [round(m / p, 6) for m, p in zip(multipliers, prices)]


@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        message = data.get("message")
        symbol = data.get("symbol", "BTCUSDT").upper()
        parsed = parse_signal(message)

        direction = parsed["direction"]
        acc_entries = calculate_zone_entries(parsed["accumulation_zone"])
        quantities = calculate_quantities(acc_entries, direction)

        tp1 = parsed["take_profits"][0]
        stop_loss = parsed["stop_loss"]

        results = []
        for i in range(3):
            tp_partial = tp1  # Could be made dynamic
            qty = quantities[i]
            res = place_order(symbol, direction, acc_entries[i], qty, stop_loss, tp_partial)
            results.append(res)

        return jsonify({"status": "success", "orders": results})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

