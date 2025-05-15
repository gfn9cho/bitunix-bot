# redis_client.py
import redis.asyncio as redis
import os
from urllib.parse import urlparse

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")  # Render injects this automatically
if not redis_url:
    raise ValueError("REDIS_URL not set. Make sure Redis add-on is configured.")

url = urlparse(redis_url)


def get_redis():
    return redis.Redis(
        host=url.hostname,
        port=url.port,
        db=0,  # default
        decode_responses=True,
        password=url.password or None  # handle no-password case
    )
