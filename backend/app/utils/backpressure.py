"""
Backpressure check — read the cached dispatcher consumer lag from Redis and
raise BackpressureError (503) if it's above the configured threshold.

The worker's metrics loop refreshes the key every ~60s; the API never queries
Kafka directly. **Fail-open three ways** — absent/expired key, unparseable value,
and a failed read (Redis unreachable) all accept the job. The third was once
unhandled, making this the only Redis touch on `POST /jobs` that failed closed
(docs/REDIS.md, ADR 0005): the signal is advisory, and losing it is no reason to
refuse work.
"""

from app.config import get_settings
from app.core.exceptions import BackpressureError
from app.core.logging import get_logger
from redis.asyncio import Redis

logger = get_logger(__name__)

# Keep in sync with workers/dispatcher.py:BACKPRESSURE_LAG_KEY.
BACKPRESSURE_LAG_KEY = "kafka:consumer_lag:worker-dispatcher"


async def check_backpressure(redis: Redis) -> None:
    """Raise BackpressureError if the dispatcher is too far behind."""
    settings = get_settings()
    threshold = settings.backpressure_lag_threshold
    if threshold <= 0:
        return

    try:
        raw = await redis.get(BACKPRESSURE_LAG_KEY)
    except Exception as exc:
        # Redis unavailable — fail open, like the rate limiter.
        logger.warning(
            "backpressure_check_failed",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:200]},
        )
        return

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
