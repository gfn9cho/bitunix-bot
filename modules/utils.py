import os
import json
import re
from datetime import datetime

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

def calculate_zone_entries(acc_zone):
    top, bottom = acc_zone
    mid = (top + bottom) / 2
    return [top, mid, bottom]

def calculate_quantities(prices, direction):
    multipliers = [10, 10, 20]  # $ amounts
    return [round(m / p, 6) for m, p in zip(multipliers, prices)]
