"""
Sliding-window rate limiter backed by Redis.

Algorithm
---------
Each request increments a counter stored in a Redis key scoped to
``(identifier, window_start_second)``.  The key expires automatically after
the window passes, so no cleanup is needed.

Usage (as a FastAPI dependency)
--------------------------------
    from app.utils.rate_limit import rate_limiter

    @router.post("/login")
    async def login(
        request: Request,
        _: None = Depends(rate_limiter(limit=10, window=60)),
    ): ...
"""

import time
from collections.abc import Callable, Coroutine
from typing import Any

from app.core.exceptions import RateLimitError
from app.core.logging import get_logger
from app.core.redis import get_redis
from fastapi import Depends, Request
from redis.asyncio import Redis

logger = get_logger(__name__)


def _client_key(request: Request) -> str:
    """Derive a stable per-client identifier from the request.

    Trust model (finding E2-05): the only trusted proxy in production is
    the ALB, which APPENDS the connecting client's IP as the LAST
    X-Forwarded-For hop. Everything to the left of that hop is
    caller-supplied and forgeable — keying on the leftmost entry let a
    client mint a fresh rate bucket per request just by rotating the
    header, so we key on the rightmost hop. request.client.host cannot be
    the primary identity: scripts/entrypoint.sh runs uvicorn without
    --proxy-headers, so in production it is the ALB node IP, shared by
    every client. If a CDN/WAF layer is ever added in front of the ALB,
    the rightmost hop becomes that layer's IP and this needs a
    trusted-hop-count knob — do not build the knob before the topology
    exists.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Drop empty entries so a degenerate header of only commas or
        # whitespace falls through to the direct peer address.
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


async def _check(
    redis: Redis,
    key: str,
    limit: int,
    window: int,
) -> None:
    """Increment the sliding-window counter and raise if over limit."""
    window_start = int(time.time()) // window
    redis_key = f"rate:{key}:{window_start}"

    count = await redis.incr(redis_key)
    if count == 1:
        # Set TTL on first increment so the key auto-expires
        await redis.expire(redis_key, window * 2)

    if count > limit:
        raise RateLimitError(
            f"Rate limit exceeded: {limit} requests per {window}s.",
            details={"limit": limit, "window_seconds": window},
        )


def rate_limiter(
    limit: int = 60,
    window: int = 60,
    key_prefix: str = "",
) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    Return a FastAPI dependency that enforces a sliding-window rate limit.

    Args:
        limit:      Maximum number of requests allowed in the window.
        window:     Window size in seconds.
        key_prefix: Optional prefix to namespace limits per endpoint.
    """
    async def dependency(
        request: Request,
        redis: Redis = Depends(get_redis),
    ) -> None:
        client = _client_key(request)
        key = f"{key_prefix}:{client}" if key_prefix else client
        try:
            await _check(redis, key, limit, window)
        except RateLimitError:
            raise
        except Exception:
            # Redis unavailable — fail open so legitimate traffic is not blocked
            logger.warning("rate_limit_check_failed", extra={"key": key})

    return dependency
