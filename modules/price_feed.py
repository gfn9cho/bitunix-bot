import requests
import datetime
import asyncio
from modules.logger_config import logger
from modules.loss_tracking import log_false_signal
from modules.postgres_state_manager import delete_position_state

BITUNIX_BASE_URL = "https://fapi.bitunix.com"


def get_latest_close_price(symbol: str, interval: str) -> float:
    url = f"{BITUNIX_BASE_URL}/api/v1/futures/market/kline"
    params = {
        "symbol": symbol.upper(),
        "interval": interval.upper(),  # Bitunix uses '1M', '3M', etc.
        "limit": 1
    }
    try:
        response = requests.get(url, params=params)
        logger.info(f"{response}")
        logger.info(f"{response.json()}")
        response.raise_for_status()
        candles = response.json().get("data", [])
        if not candles:
            raise ValueError("No candle data returned")

        # Bitunix format: [timestamp, open, high, low, close, volume, turnover]
        latest_candle = candles[0]
        logger.info(f"[LATEST CANDLE]: {latest_candle}")
        close_price = float(latest_candle.get("close"))
        return close_price
    except Exception as e:
        raise RuntimeError(f"Failed to fetch close price for {symbol} from Bitunix: {e}")


async def is_false_signal(symbol: str, entry_price: float, direction: str, interval: str,
                          signal_time: datetime.datetime) -> bool:
    bar_close_time = get_next_bar_close(signal_time, interval)
    wait_seconds = (bar_close_time - datetime.datetime.utcnow()).total_seconds()
    if wait_seconds > 0:
        logger.info(f"Waiting {wait_seconds:.2f} seconds for bar to close...")
        await asyncio.sleep(wait_seconds)

    close_price = get_latest_close_price(symbol, interval)
    if direction == "BUY" and entry_price > close_price:
        return True
    if direction == "SELL" and entry_price < close_price:
        return True
    return False


async def validate_and_process_signal(symbol: str, entry_price: float, direction: str, interval: str, signal_time: datetime.datetime, callback):
    try:
        is_false = await is_false_signal(symbol, entry_price, direction, interval, signal_time)
        if is_false:
            logger.warning(f"❌ False signal ignored: {symbol} {direction} at {entry_price}")
            log_false_signal(symbol, direction, entry_price, interval, "false_signal", signal_time)
            delete_position_state(symbol, direction)
            return

        logger.info(f"✅ Valid signal confirmed: {symbol} {direction} at {entry_price}")
        await callback()
    except Exception as e:
        logger.error(f"[SIGNAL VALIDATION ERROR] {symbol} {direction}: {e}")


def get_next_bar_close(current_time: datetime.datetime, interval: str) -> datetime.datetime:
    minute_intervals = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15,
        "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440
    }
    mins = minute_intervals.get(interval.lower(), 1)
    rounded_minute = (current_time.minute // mins + 1) * mins
    return current_time.replace(second=0, microsecond=0) + datetime.timedelta(
        minutes=(rounded_minute - current_time.minute - 0.05))

# print("SOLUSDT 1M close:", get_latest_close_price("SOLUSDT", "1m"))
# false_sginal = is_false_signal("SOLUSDT", 176.90, "BUY", "1m", datetime.datetime.utcnow())
# logger.info(f"{false_sginal}")
