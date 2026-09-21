"""
Fixed-window rate limiter backed by Redis: a counter per
``(identifier, window_start_second)``, expiring with the window.

**Fixed window, not sliding.** The bucket is ``int(time.time()) // window``, so the
real guarantee is at most ``limit`` per window and at most ``2 * limit`` across a
boundary instant. The docs called it sliding and promised a bound the code never
enforced (WO-R2-30); the naming was corrected rather than the algorithm, because
``2 * limit`` is a real bound. **Size ceilings against ``2 * limit``.**

`rate_limiter()` is the FastAPI dependency keyed on client IP (`check_client_rate_limit`
is its body, for callers that read their ceiling per request);
`check_identity_rate_limit` is the inline form keyed on an authenticated identity.
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

    Trust model (E2-05): the ALB appends the real client IP as the LAST
    X-Forwarded-For hop, and everything left of it is forgeable, so key on the
    rightmost. `request.client.host` is the ALB node IP in production (uvicorn runs
    without --proxy-headers). A CDN in front of the ALB would need a hop-count knob.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Drop empties so an all-commas header uses the peer.
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
    """Increment the fixed-window counter and raise if over limit."""
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


async def check_client_rate_limit(
    request: Request,
    redis: Redis,
    *,
    limit: int,
    window: int,
    key_prefix: str = "",
) -> None:
    """Fixed-window check keyed on the caller's address (`_client_key` has the
    trust model). `key_prefix` namespaces the bucket. Fail-open on a Redis error
    (ADR 0005); a `RateLimitError` is re-raised.
    """
    client = _client_key(request)
    key = f"{key_prefix}:{client}" if key_prefix else client
    try:
        await _check(redis, key, limit, window)
    except RateLimitError:
        raise
    except Exception:
        # Redis unavailable — fail open so legitimate traffic is not blocked
        logger.warning("rate_limit_check_failed", extra={"key": key})


def rate_limiter(
    limit: int = 60,
    window: int = 60,
    key_prefix: str = "",
) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    A FastAPI dependency enforcing a fixed-window rate limit, keyed on client
    IP (`_client_key` has the trust model). `key_prefix` namespaces the bucket.
    """
    async def dependency(
        request: Request,
        redis: Redis = Depends(get_redis),
    ) -> None:
        await check_client_rate_limit(
            request, redis, limit=limit, window=window, key_prefix=key_prefix
        )

    return dependency


async def check_identity_rate_limit(
    redis: Redis,
    *,
    identity: object,
    limit: int,
    window: int,
    bucket: str,
) -> None:
    """Rate-limit on an identity the caller has already authenticated.

    IP is the wrong scope for the MCP surface (one bucket per ECS egress address,
    and a fresh allowance on reconnect — WO-R2-30) and for the paid admin
    endpoints, where the spend belongs to a token. `bucket` namespaces the counter.
    Fail-open on a Redis error (ADR 0005); a `RateLimitError` is re-raised.
    The window is fixed, so size against `2 * limit`.
    """
    key = f"{bucket}:{identity}"
    try:
        await _check(redis, key, limit, window)
    except RateLimitError:
        raise
    except Exception:
        logger.warning("rate_limit_check_failed", extra={"key": key})
