import asyncio
from datetime import datetime, timedelta

import httpx

from modules.config import BASE_URL
from modules.logger_config import logger
from modules.loss_tracking import log_false_signal

# from modules.market_filters import get_funding_rate, get_open_interest, get_open_interest_trend

BITUNIX_BASE_URL = "https://fapi.bitunix.com"

# Interval mapping
INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15,
    "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440
}


# --- Time utilities ---
def get_previous_bar_close(current_time: datetime, interval: str) -> datetime:
    mins = INTERVAL_MINUTES.get(interval.lower(), 1)
    current_time = current_time.replace(second=0, microsecond=0)
    total_minutes = current_time.hour * 60 + current_time.minute
    floored_minutes = (total_minutes // mins) * mins
    floored_hour = floored_minutes // 60
    floored_minute = floored_minutes % 60
    floored_time = current_time.replace(hour=floored_hour, minute=floored_minute)
    return floored_time - timedelta(minutes=mins)


def get_next_bar_close(current_time: datetime, interval: str) -> datetime:
    mins = INTERVAL_MINUTES.get(interval.lower(), 1)
    current_time = current_time.replace(second=0, microsecond=0)
    total_minutes = current_time.hour * 60 + current_time.minute
    next_bar_minutes = ((total_minutes // mins) + 1) * mins
    next_hour = next_bar_minutes // 60
    next_minute = next_bar_minutes % 60
    return current_time.replace(hour=next_hour % 24, minute=next_minute)


def get_bar_start_for_close(close_time: datetime, interval_min: int) -> int:
    bar_start = close_time - timedelta(minutes=interval_min)
    return int(bar_start.timestamp() * 1000)


# --- Candle fetchers ---
async def get_previous_candle_close_price(symbol: str, interval: str, reference_time: datetime,
                                          max_retries: int = 3) -> float:
    actual_interval = "1m" if interval == "3m" else interval
    url = f"{BITUNIX_BASE_URL}/api/v1/futures/market/kline"
    expected_ts = int(get_previous_bar_close(reference_time, actual_interval).timestamp() * 1000)

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                params = {
                    "symbol": symbol.upper(),
                    "interval": actual_interval.lower(),
                    "limit": 2,
                    "type": "MARK_PRICE"
                }
                response = await client.get(url, params=params)
                response.raise_for_status()
                candles = response.json().get("data", [])
                for candle in candles:
                    if int(candle.get("time", 0)) == expected_ts:
                        logger.info(f"[MATCHED BUY CANDLE]: {candle}")
                        return float(candle.get("close"))
                    elif attempt == 2:
                        mark_price = get_latest_mark_price(symbol)
                        return mark_price if mark_price else \
                            logger.warning(
                                f"[BUY CANDLE NOT FOUND] Expected {expected_ts}, Got {[int(c['time']) for c in candles]}")
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[BUY CANDLE FETCH ERROR] Attempt {attempt + 1}: {e}")
            await asyncio.sleep(1)

    raise RuntimeError(f"Failed to fetch matching BUY candle close price for {symbol} after {max_retries} retries")


async def get_latest_close_price_current(symbol: str, interval: str, expected_ts: int, max_retries: int = 3) -> float:
    actual_interval = "1m" if interval == "3m" else interval
    url = f"{BITUNIX_BASE_URL}/api/v1/futures/market/kline"
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                params = {
                    "symbol": symbol.upper(),
                    "interval": actual_interval.lower(),
                    "limit": 1
                }
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json().get("data", [])
                if not data:
                    raise ValueError("No candle data returned")
                candle = data[0]
                candle_ts = int(candle.get("time", 0))
                if candle_ts == expected_ts:
                    logger.info(f"[LATEST CANDLE MATCHED]: {candle}")
                    return float(candle.get("close"))
                elif attempt == 2:
                    mark_price = get_latest_mark_price(symbol)
                    return mark_price if mark_price else \
                        logger.warning(f"[CANDLE MISMATCH] Expected {expected_ts}, Got {candle_ts}. Retrying...")
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"[SELL CANDLE FETCH ERROR] Attempt {attempt + 1}: {e}")
            await asyncio.sleep(1)

    raise RuntimeError(f"Failed to fetch matching SELL candle close price for {symbol} after {max_retries} retries")


# --- Signal Validator ---
async def is_false_signal(symbol: str, entry_price: float, direction: str, interval: str,
                          signal_time: datetime, buffer_pct: float = 0.005) -> bool:
    if direction == "SELL":
        bar_close_time = get_next_bar_close(signal_time, interval)
        wait_seconds = (bar_close_time - datetime.utcnow()).total_seconds()
        if wait_seconds > 0:
            logger.info(f"Waiting {wait_seconds:.2f} seconds for bar to close...")
            await asyncio.sleep(wait_seconds)
        actual_interval = "1m" if interval == "3m" else interval
        interval_min = INTERVAL_MINUTES.get(actual_interval.lower(), 1)
        expected_ts = get_bar_start_for_close(bar_close_time, interval_min)
        close_price = await get_latest_close_price_current(symbol, interval, expected_ts)
    else:  # BUY
        close_price = await get_previous_candle_close_price(symbol, interval, signal_time)

    buffer = close_price * buffer_pct
    if direction == "BUY" and entry_price <= (close_price + buffer):
        logger.info(
            f"[VALID SIGNAL CHECK]: BUY | entry_price: {entry_price} close_price: {close_price} buffer: {buffer}")
        return False
    if direction == "SELL" and entry_price >= close_price:
        logger.info(f"[VALID SIGNAL CHECK]: SELL | entry_price: {entry_price} close_price: {close_price}")
        return False

    return True


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


async def validate_and_process_signal(symbol: str, entry_price: float, direction: str, interval: str,
                                      signal_time: datetime, callback):
    try:
        # Step 1: Check for false signal
        is_false = await is_false_signal(symbol, entry_price, direction, interval, signal_time)
        if is_false:
            logger.warning(f"❌ False signal ignored: {symbol} {direction} at {entry_price}")
            log_false_signal(symbol, direction, entry_price, interval, "false_signal", signal_time)
            # delete_position_state(symbol, direction, True, None)
            return
        # Step 2: Check for volume spike
        # if await is_volume_spike(symbol, interval):
        #     logger.warning(f"[VOLUME SPIKE] Signal rejected due to abnormal volume on {symbol}")
        #     return
        #
        # # Step 3: Check funding rate
        # funding = await get_funding_rate(symbol)
        # if direction == "BUY" and funding > 0.001:
        #     logger.warning(f"[FUNDING] Skipping BUY due to positive funding: {funding}")
        #     return
        # if direction == "SELL" and funding < -0.001:
        #     logger.warning(f"[FUNDING] Skipping SELL due to negative funding: {funding}")
        #     return
        #
        # # Step 4: Check open interest
        # if not await is_open_interest_supportive(symbol, direction, interval):
        #     logger.warning(f"[OI FILTER] Skipping signal: OI not supportive for {symbol} {direction}")
        #     return
        logger.info(f"✅ Valid signal confirmed: {symbol} {direction} at {entry_price}")

        # step 5: Process trades
        await callback()
    except Exception as e:
        logger.error(f"[SIGNAL VALIDATION ERROR] {symbol} {direction}: {e}")


# print("SOLUSDT 1M close:", get_latest_close_price("SOLUSDT", "1m"))
# false_sginal = is_false_signal("SOLUSDT", 176.90, "BUY", "1m", datetime.datetime.utcnow())
# logger.info(f"{false_sginal}")
