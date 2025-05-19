import asyncio
import datetime

import httpx
import requests

from modules.logger_config import logger
from modules.loss_tracking import log_false_signal
from modules.config import BASE_URL


async def get_latest_mark_price(symbol: str) -> float:
    url = f"{BASE_URL}/api/v1/futures/market/tickers"
    params = {"symbols": symbol.upper()}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            tickers = response.json().get("data", [])

        if not tickers:
            raise ValueError(f"No ticker found for {symbol}")

        return float(tickers[0]["markPrice"])

    except Exception as e:
        logger.error(f"[MARK PRICE ERROR] Failed to fetch mark price for {symbol}: {e}")
        raise RuntimeError(f"Failed to fetch mark price for {symbol}: {e}")


# def get_latest_close_price(symbol: str, interval: str) -> float:
#     url = f"{BASE_URL}/api/v1/futures/market/kline"
#     params = {
#         "symbol": symbol.upper(),
#         "interval": interval.lower(),  # Bitunix uses '1M', '3M', etc.
#         "limit": 1
#     }
#     try:
#         response = requests.get(url, params=params)
#         logger.info(f"{response}")
#         logger.info(f"{response.json()}")
#         response.raise_for_status()
#         candles = response.json().get("data", [])
#         if not candles:
#             raise ValueError("No candle data returned")
#
#         # Bitunix format: [timestamp, open, high, low, close, volume, turnover]
#         latest_candle = candles[0]
#         logger.info(f"[LATEST CANDLE]: {latest_candle}")
#         close_price = float(latest_candle.get("close"))
#         return close_price
#     except Exception as e:
#         raise RuntimeError(f"Failed to fetch close price for {symbol} from Bitunix: {e}")


def get_previous_bar_close(current_time: datetime, interval: str) -> datetime:
    minute_intervals = {
        "1m": 1, "3m": 3, "5m": 5, "15m": 15,
        "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440
    }
    mins = minute_intervals.get(interval.lower(), 1)
    current_time = current_time.replace(second=0, microsecond=0)
    total_minutes = current_time.hour * 60 + current_time.minute
    floored_minutes = (total_minutes // mins) * mins
    floored_hour = floored_minutes // 60
    floored_minute = floored_minutes % 60
    floored_time = current_time.replace(hour=floored_hour, minute=floored_minute)
    return floored_time


def get_latest_close_price(symbol: str, interval: str, reference_time: datetime = None) -> float:
    url = f"{BASE_URL}/api/v1/futures/market/kline"
    params = {
        "symbol": symbol.upper(),
        "interval": interval.lower(),
        "limit": 2 if reference_time else 1,
        "type": "MARK_PRICE"
    }
    try:
        response = requests.get(url, params=params)
        logger.info(f"{response}")
        data = response.json()
        logger.info(f"{data}")
        response.raise_for_status()
        candles = data.get("data", [])
        if not candles:
            raise ValueError("No candle data returned")
        if reference_time:
            previous_close_time = get_previous_bar_close(reference_time, interval)
            for candle in candles:
                candle_time = datetime.datetime.utcfromtimestamp(int(candle.get("time")) / 1000)
                if candle_time == previous_close_time:
                    logger.info(f"[MATCHED CANDLE]: {candle}")
                    return float(candle.get("close"))
            logger.warning(f"[CANDLE NOT FOUND]: {symbol} Looking for {previous_close_time}, got {[datetime.datetime.utcfromtimestamp(int(c['time']) / 1000) for c in candles]}")

            raise ValueError("Matching candle not found for reference time")

        latest_candle = candles[-1]
        logger.info(f"[LATEST CANDLE]: {latest_candle}")
        return float(latest_candle.get("close"))

    except Exception as e:
        raise RuntimeError(f"Failed to fetch close price for {symbol} from Bitunix: {e}")


async def is_false_signal(symbol: str, entry_price: float, direction: str, interval: str,
                          signal_time: datetime.datetime, buffer_pct: float = 0.02) -> bool:
    # bar_close_time = get_previous_bar_close(signal_time, interval)
    # wait_seconds = (bar_close_time - datetime.datetime.utcnow()).total_seconds()
    # if wait_seconds > 0:
    #     logger.info(f"Waiting {wait_seconds:.2f} seconds for bar to close...")
    #     await asyncio.sleep(wait_seconds)

    close_price = get_latest_close_price(symbol, interval, datetime.datetime.utcnow())
    buffer = entry_price * buffer_pct
    if direction == "BUY" and entry_price > (close_price + buffer):
        return True
    if direction == "SELL" and entry_price < (close_price - buffer):
        return True
    return False


async def validate_and_process_signal(symbol: str, entry_price: float, direction: str, interval: str,
                                      signal_time: datetime.datetime, callback):
    try:
        is_false = await is_false_signal(symbol, entry_price, direction, interval, signal_time)
        if is_false:
            logger.warning(f"❌ False signal ignored: {symbol} {direction} at {entry_price}")
            log_false_signal(symbol, direction, entry_price, interval, "false_signal", signal_time)
            # delete_position_state(symbol, direction, True, None)
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
        minutes=(rounded_minute - current_time.minute))

# print("SOLUSDT 1M close:", get_latest_close_price("SOLUSDT", "1m"))
# false_sginal = is_false_signal("SOLUSDT", 176.90, "BUY", "1m", datetime.datetime.utcnow())
# logger.info(f"{false_sginal}")
