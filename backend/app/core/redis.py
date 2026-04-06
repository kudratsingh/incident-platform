"""
Redis client setup.

A single ConnectionPool is created once at import time and shared across all
coroutines.  Each caller gets a Redis handle backed by the pool — cheap to
create, no per-request TCP overhead.

Workers and HTTP handlers both import `get_redis_pool()` so there is exactly
one pool for the whole process.
"""

from collections.abc import AsyncGenerator

from app.config import get_settings
from redis.asyncio import ConnectionPool, Redis

_pool: ConnectionPool | None = None


def get_redis_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool.from_url(
            str(get_settings().redis_url),
            max_connections=20,
            decode_responses=True,
        )
    return _pool


def get_redis_client() -> Redis:
    """Return a Redis client backed by the shared pool (no I/O, just a handle)."""
    return Redis(connection_pool=get_redis_pool())


async def close_redis_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


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
