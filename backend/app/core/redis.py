"""
Redis client setup.

Two pools, deliberately separate.

**The default pool** (`get_redis_pool`, 20 connections) backs everything that
borrows a connection, does its work and gives it straight back: the rate
limiter, the per-tenant quota, `check_backpressure`, the job cache, the
priority queue, the worker loops.  Every caller holds a connection for
milliseconds, so 20 slots go a very long way.

**The SSE pool** (`get_sse_redis_pool`, `SSE_REDIS_MAX_CONNECTIONS`) backs the
one thing that does not: Pub/Sub.  A subscription owns its connection for as
long as it is subscribed, which for `GET /jobs/{id}/stream` is the life of the
stream.  Sharing the default pool meant a viewer and a rate-limit check
competed for the same 20 slots, and the viewer always won because it never
let go — so a modest wall of parked dashboards made the rate limiter fail
open, `check_backpressure` 500, and admin stats error (WO-R2-11).  Splitting
the pools makes that impossible by construction: whatever streaming does to
its own pool, the request path keeps its 20 slots.

The split alone would only move the ceiling, so it is not the whole fix — see
`workers/progress_broker.py`, where one Pub/Sub connection is shared by every
open stream in the process.  The dedicated pool is the blast-radius guarantee;
the broker is what keeps the connection count off the viewer count.
"""

from collections.abc import AsyncGenerator

from app.config import get_settings
from redis.asyncio import ConnectionPool, Redis

DEFAULT_MAX_CONNECTIONS = 20

_pool: ConnectionPool | None = None
_sse_pool: ConnectionPool | None = None


def get_redis_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(
            str(get_settings().redis_url),
            max_connections=DEFAULT_MAX_CONNECTIONS,
            decode_responses=True,
        )
    return _pool


def get_redis_client() -> Redis:
    """Return a Redis client backed by the shared pool (no I/O, just a handle)."""
    return Redis(connection_pool=get_redis_pool())


def get_sse_redis_pool() -> ConnectionPool:
    """The streaming path's own pool — never the one the request path uses.

    Bounded by `SSE_REDIS_MAX_CONNECTIONS`.  Exhausting it degrades SSE and
    nothing else, which is the entire reason it exists.
    """
    global _sse_pool
    if _sse_pool is None:
        settings = get_settings()
        _sse_pool = ConnectionPool.from_url(
            str(settings.redis_url),
            max_connections=settings.sse_redis_max_connections,
            decode_responses=True,
        )
    return _sse_pool


def get_sse_redis_client() -> Redis:
    """Return a Redis client backed by the SSE pool (no I/O, just a handle)."""
    return Redis(connection_pool=get_sse_redis_pool())


async def close_redis_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def close_sse_redis_pool() -> None:
    global _sse_pool
    if _sse_pool is not None:
        await _sse_pool.aclose()
        _sse_pool = None


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_redis() -> AsyncGenerator[Redis, None]:
    """Yields a Redis client for use in FastAPI route handlers."""
    client = get_redis_client()
    try:
        yield client
    finally:
        # Pool manages the underlying connection; nothing to close here.
        pass
