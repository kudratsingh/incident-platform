"""
Redis client setup: two pools, deliberately separate.

`get_redis_pool` (20 connections) serves every caller that borrows and returns a
connection in milliseconds. `get_sse_redis_pool` (`SSE_REDIS_MAX_CONNECTIONS`)
serves Pub/Sub, which holds one for the life of a stream — sharing the default
pool let parked dashboards starve the rate limiter and `check_backpressure`
(WO-R2-11). The pool is the blast-radius guarantee; `workers/progress_broker.py`
sharing one Pub/Sub connection is what keeps the count off the viewer count.
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
    """The streaming path's own pool (`SSE_REDIS_MAX_CONNECTIONS`); exhausting it hits only SSE."""
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
