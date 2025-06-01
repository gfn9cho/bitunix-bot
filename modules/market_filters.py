import asyncio

import httpx

from modules.config import BASE_URL
from modules.logger_config import logger


async def get_funding_rate(symbol: str) -> float:
    url = f"{BASE_URL}/api/v1/futures/market/funding_rate"
    params = {"symbol": symbol.upper()}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json().get("data")
            if not data or "fundingRate" not in data:
                raise ValueError("No funding rate found for symbol")
            funding_rate = float(data["fundingRate"])
            logger.info(f"[FUNDING RATE] {symbol}: {funding_rate}")
            return funding_rate
    except Exception as e:
        logger.error(f"[FUNDING RATE ERROR] Failed to fetch for {symbol}: {e}")
        return 0.0


async def get_open_interest(symbol: str) -> float:
    url = f"{BASE_URL}/api/v1/futures/market/open-interest"
    params = {"symbol": symbol.upper()}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json().get("data")
            if not data or "openInterest" not in data:
                raise ValueError("No open interest found for symbol")
            open_interest = float(data["openInterest"])
            logger.info(f"[OPEN INTEREST] {symbol}: {open_interest}")
            return open_interest
    except Exception as e:
        logger.error(f"[OPEN INTEREST ERROR] Failed to fetch for {symbol}: {e}")
        return 0.0


async def get_open_interest_trend(symbol: str, interval: str = "5m", lookback: int = 5) -> list[float]:
    url = f"{BASE_URL}/api/v1/futures/market/open-interest-history"
    params = {
        "symbol": symbol.upper(),
        "interval": interval.lower(),
        "limit": lookback
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json().get("data")
            if not data or not isinstance(data, list):
                raise ValueError("Invalid OI trend data received")
            trend = [float(entry["openInterestValue"]) for entry in data]
            logger.info(f"[OI TREND] {symbol} {interval}: {trend}")
            return trend
    except Exception as e:
        logger.error(f"[OI TREND ERROR] Failed to fetch trend for {symbol}: {e}")
        return []


async def is_open_interest_increasing(symbol: str, interval: str = "5m", lookback: int = 5) -> bool:
    trend = await get_open_interest_trend(symbol, interval, lookback)
    if len(trend) < 2:
        logger.warning(f"[OI TREND CHECK] Not enough data for {symbol}")
        return False
    is_increasing = all(earlier <= later for earlier, later in zip(trend, trend[1:]))
    logger.info(f"[OI TREND CHECK] Increasing for {symbol}: {is_increasing}")
    return is_increasing


async def get_price_trend(symbol: str, interval: str = "5m", lookback: int = 5) -> list[float]:
    url = f"{BASE_URL}/api/v1/futures/market/kline"
    params = {
        "symbol": symbol.upper(),
        "interval": interval.lower(),
        "limit": lookback
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json().get("data")
            if not data or not isinstance(data, list):
                raise ValueError("Invalid price data received")
            trend = [float(entry["close"]) for entry in data]
            logger.info(f"[PRICE TREND] {symbol} {interval}: {trend}")
            return trend
    except Exception as e:
        logger.error(f"[PRICE TREND ERROR] Failed to fetch trend for {symbol}: {e}")
        return []


async def classify_market_bias(symbol: str, interval: str = "5m", lookback: int = 5) -> str:
    oi_trend = await get_open_interest_trend(symbol, interval, lookback)
    price_trend = await get_price_trend(symbol, interval, lookback)

    if len(oi_trend) < 2 or len(price_trend) < 2:
        return "unknown"

    oi_up = all(earlier <= later for earlier, later in zip(oi_trend, oi_trend[1:]))
    price_up = all(earlier <= later for earlier, later in zip(price_trend, price_trend[1:]))
    price_down = all(earlier >= later for earlier, later in zip(price_trend, price_trend[1:]))

    if oi_up and price_up:
        return "long"
    elif oi_up and price_down:
        return "short"
    else:
        return "neutral"


async def get_volume_trend(symbol: str, interval: str = "5m", lookback: int = 5) -> list[float]:
    url = f"{BASE_URL}/api/v1/futures/market/kline"
    params = {
        "symbol": symbol.upper(),
        "interval": interval.lower(),
        "limit": lookback
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json().get("data", [])
            return [float(candle["volume"]) for candle in data if "volume" in candle]
    except Exception as e:
        logger.error(f"[VOLUME TREND ERROR] Failed to fetch for {symbol}: {e}")
        return []


async def get_high_conviction_score(symbol: str, direction: str, interval: str = "5m") -> dict:
    try:
        actual_interval = "1m" if interval == "3m" else interval
        url = f"{BASE_URL}/api/v1/futures/market/kline"
        params = {"symbol": symbol.upper(), "interval": actual_interval, "limit": 5}

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            kline = sorted(resp.json().get("data", []), key=lambda x: x["time"])

        if not kline:
            raise ValueError("Empty kline data")
        prices = [float(candle["close"]) for candle in kline]  # close prices
        volumes = [float(candle["baseVol"]) for candle in kline]  # volumes

        price_up = prices[-1] > prices[0]
        price_down = prices[-1] < prices[0]
        avg_volume = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 0
        volume_spike_ratio = volumes[-1] / avg_volume if avg_volume else 0
        volume_spike = volume_spike_ratio > 2

        # Fetch funding rate
        funding_url = f"{BASE_URL}/api/v1/futures/market/funding_rate"
        async with httpx.AsyncClient(timeout=5.0) as client:
            funding_resp = await client.get(funding_url, params={"symbol": symbol.upper()})
            funding_resp.raise_for_status()
            funding = float(funding_resp.json().get("data", {}).get("fundingRate", 0))

        funding_check = (funding > 0 and direction == "BUY") or (funding < 0 and direction == "SELL")

        # Score components: funding + price + volume
        score = 0.0
        score += 0.4 if funding_check else 0.0
        score += 0.3 if (price_up and direction == "BUY") or (price_down and direction == "SELL") else 0.0
        if volume_spike:
            if (direction == "BUY" and price_up) or (direction == "SELL" and not price_up):
                score += 0.3

        return {
            "score": round(score, 2),
            "funding_rate": funding,
            "price_trend": prices,
            "volume_trend": volumes,
            "volume_spike_ratio": volumes[-1] / avg_volume if avg_volume else 0.0
        }

    except Exception as e:
        logger.error(f"[HIGH CONVICTION ERROR] {symbol}: {e}")
        return {
            "score": 0.0,
            "funding_rate": 0.0,
            "price_trend": [],
            "volume_trend": [],
            "volume_spike_ratio": 0.0
        }
