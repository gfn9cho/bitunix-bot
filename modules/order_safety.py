import asyncio
import httpx
from price_feed import get_latest_mark_price
from modules.logger_config import logger
from modules.utils import submit_modified_tp_sl_order_async


async def is_valid_sl_price(direction: str, sl_price: float, mark_price: float) -> bool:
    return sl_price > mark_price if direction == "BUY" else sl_price < mark_price


async def is_valid_tp_price(direction: str, tp_price: float, mark_price: float) -> bool:
    return tp_price < mark_price if direction == "BUY" else tp_price > mark_price


async def safe_submit_sl_update(symbol: str, direction: str, sl_payload: dict, sl_price: float, retries: int = 3, retry_delay: int = 2) -> bool:
    for attempt in range(retries):
        try:
            mark_price = await get_latest_mark_price(symbol)
            if not mark_price:
                raise ValueError("Mark price unavailable")

            if await is_valid_sl_price(direction, sl_price, mark_price):
                logger.info(f"[SL ✅] Submitting SL {sl_price} (mark: {mark_price}) for {symbol} {direction}")
                await submit_modified_tp_sl_order_async(sl_payload)
                return True
            else:
                logger.warning(f"[SL ❌] SL {sl_price} invalid vs mark {mark_price} on {symbol}. Attempt {attempt+1}/{retries}")
                await asyncio.sleep(retry_delay)

        except Exception as e:
            logger.error(f"[SL ERROR] Retry {attempt+1} for {symbol} {direction}: {e}")
            await asyncio.sleep(retry_delay)

    logger.error(f"[SL FAILED] Giving up SL update for {symbol} {direction} after {retries} retries.")
    return False


async def safe_submit_tp_update(symbol: str, direction: str, tp_payload: dict, tp_price: float, retries: int = 3, retry_delay: int = 2) -> bool:
    for attempt in range(retries):
        try:
            mark_price = await get_latest_mark_price(symbol)
            if not mark_price:
                raise ValueError("Mark price unavailable")

            if await is_valid_tp_price(direction, tp_price, mark_price):
                logger.info(f"[TP ✅] Submitting TP {tp_price} (mark: {mark_price}) for {symbol} {direction}")
                await submit_modified_tp_sl_order_async(tp_payload)
                return True
            else:
                logger.warning(f"[TP ❌] TP {tp_price} invalid vs mark {mark_price} on {symbol}. Attempt {attempt+1}/{retries}")
                await asyncio.sleep(retry_delay)

        except Exception as e:
            logger.error(f"[TP ERROR] Retry {attempt+1} for {symbol} {direction}: {e}")
            await asyncio.sleep(retry_delay)

    logger.error(f"[TP FAILED] Giving up TP update for {symbol} {direction} after {retries} retries.")
    return False
