"""
Backpressure check — read the cached dispatcher consumer lag from Redis and
raise BackpressureError (503) if it's above the configured threshold.

The metrics loop in the worker updates the cache key every ~60s with the
current group lag. The API never queries Kafka directly — that would add
latency to every job submission. A missing/expired cache entry is treated
as "lag unknown, accept the job."
"""

from app.config import get_settings
from app.core.exceptions import BackpressureError
from app.core.logging import get_logger
from redis.asyncio import Redis

logger = get_logger(__name__)

# Keep in sync with workers/dispatcher.py:BACKPRESSURE_LAG_KEY — duplicated as
# a constant here to avoid importing the dispatcher (and its heavy deps) from
# the API request path.
BACKPRESSURE_LAG_KEY = "kafka:consumer_lag:worker-dispatcher"


async def check_backpressure(redis: Redis) -> None:
    """Raise BackpressureError if the dispatcher is too far behind."""
    settings = get_settings()
    threshold = settings.backpressure_lag_threshold
    if threshold <= 0:
        return

    raw = await redis.get(BACKPRESSURE_LAG_KEY)
    if raw is None:
        return  # unknown lag — let the request through

    try:
        lag = int(raw)
    except (TypeError, ValueError):
        return

    if lag > threshold:
        logger.warning(
            "backpressure rejected job submission",
            extra={"lag": lag, "threshold": threshold},
        )
        raise BackpressureError(
            f"Worker is {lag} messages behind (threshold {threshold}); "
            "retry shortly."
        )
