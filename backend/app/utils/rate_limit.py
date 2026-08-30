"""
Fixed-window rate limiter backed by Redis.

Algorithm
---------
Each request increments a counter stored in a Redis key scoped to
``(identifier, window_start_second)``.  The key expires automatically after
the window passes, so no cleanup is needed.

**This is a fixed window, not a sliding one.** The bucket is chosen by
``int(time.time()) // window``, so it resets on absolute window
boundaries rather than moving with the caller. The guarantee this
limiter actually provides is therefore:

    at most ``limit`` requests per window, and at most ``2 * limit``
    across any instant that straddles a window boundary

— a caller who sends ``limit`` requests just before the boundary and
``limit`` more just after has sent ``2 * limit`` in a moment while
staying inside the rule. Three docstrings here, ``docs/REDIS.md`` and
``docs/ARCHITECTURE.md`` all described this as a *sliding* window, which
promised a bound the code has never enforced (WO-R2-30).

The naming is corrected rather than the algorithm because ``2 * limit``
is a real bound, not an absence of one, and every caller's ceiling is
chosen with headroom well past a factor of two. A true sliding window
needs a sorted set and a MULTI/EXEC round trip per request; that is a
worthwhile change on its own merits, but it is a behaviour change for
every existing caller and it is not what makes the unlimited surfaces
in this order safe. **Size ceilings against ``2 * limit``,** not
``limit``.

Usage (as a FastAPI dependency, keyed on client IP)
---------------------------------------------------
    from app.utils.rate_limit import rate_limiter

    @router.post("/login")
    async def login(
        request: Request,
        _: None = Depends(rate_limiter(limit=10, window=60)),
    ): ...

Usage (inline, keyed on an identity the handler has already resolved)
----------------------------------------------------------------------
    from app.utils.rate_limit import check_identity_rate_limit

    await check_identity_rate_limit(
        redis, identity=principal.id, limit=120, window=60, bucket="mcp"
    )
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


def rate_limiter(
    limit: int = 60,
    window: int = 60,
    key_prefix: str = "",
) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    Return a FastAPI dependency that enforces a fixed-window rate limit,
    keyed on the client IP (see `_client_key` for the trust model).

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


async def check_identity_rate_limit(
    redis: Redis,
    *,
    identity: object,
    limit: int,
    window: int,
    bucket: str,
) -> None:
    """Rate-limit on an identity the caller has already authenticated.

    The `rate_limiter()` dependency above keys on client IP, which is the
    right scope for anonymous traffic and the wrong one for everything in
    this module's other two callers:

      * the **MCP surface**, where every request carries a service-account
        bearer token. Keying on IP would put every principal behind one
        ECS task's egress address into a single bucket — one noisy agent
        would throttle the others, and an agent that reconnects from a
        new address would get a fresh allowance. `CLAUDE.md` has claimed
        "rate-limited per principal" since the MCP server shipped; before
        WO-R2-30 there was no rate limiting on that surface at all.
      * the **paid admin endpoints**, where the thing worth bounding is
        spend attributable to an admin token, not to an address.

    `identity` is stringified, so a `uuid.UUID` principal id, a user id or
    a service-account id all work. `bucket` namespaces the counter so a
    principal's MCP allowance and its digest allowance are independent.

    Fail-open on a Redis error, matching `rate_limiter`, the backpressure
    check and the per-tenant quota check: no signal is not a reason to
    reject ([ADR 0005](../../../docs/ADR/0005-llm-features-fail-open.md)).
    A `RateLimitError` is re-raised — that is a decision, not a failure.

    Remember the window is **fixed**, so the enforced ceiling is
    `2 * limit` across a boundary instant (module docstring). Callers
    size against that.
    """
    key = f"{bucket}:{identity}"
    try:
        await _check(redis, key, limit, window)
    except RateLimitError:
        raise
    except Exception:
        logger.warning("rate_limit_check_failed", extra={"key": key})
