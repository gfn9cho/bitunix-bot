import time
from modules.redis_client import redis as r
from modules.logger_config import logger

# Configurable map of timeframe -> (buffer_seconds, max_signals)
TIMEFRAME_LIMITS = {
    "1m": (5, 2),      # short-term: very strict
    "5m": (10, 3),
    "15m": (60, 3),
    "1h": (120, 3),
    "4h": (300, 2),
    "1d": (600, 1),
}


def _window_start(timestamp: int, interval_sec: int) -> int:
    return timestamp - (timestamp % interval_sec)


async def should_accept_signal(symbol: str, direction: str, timeframe: str) -> bool:
    now = int(time.time())
    limits = TIMEFRAME_LIMITS.get(timeframe)
    if not limits:
        logger.warning(f"[SIGNAL LIMIT] No limits configured for {timeframe}, defaulting to accept")
        return True

    buffer_secs, max_signals = limits
    window_key = _window_start(now, buffer_secs)
    redis_key = f"signal_limiter:{symbol}:{direction}:{timeframe}:{window_key}"

    try:
        count = await r.get(redis_key)
        if count and int(count) >= max_signals:
            logger.warning(f"[SIGNAL LIMIT] {symbol} {direction} exceeded limit {max_signals} in {timeframe}")
            return False

        # Increment count and set expiration
        await r.incr(redis_key)
        await r.expire(redis_key, buffer_secs)
        return True

    except Exception as e:
        logger.error(f"[SIGNAL LIMIT ERROR] Redis failure for {symbol} {direction}: {e}")
        return True  # fail open
