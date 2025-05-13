# redis_client.py
import redis
import os
from urllib.parse import urlparse

redis_url = os.getenv("REDIS_URL")  # Render injects this automatically
if not redis_url:
    raise ValueError("REDIS_URL not set. Make sure Redis add-on is configured.")

url = urlparse(redis_url)
r = redis.Redis(
    host=url.hostname,
    port=url.port,
    password=url.password,
    db=0,
    decode_responses=True
)