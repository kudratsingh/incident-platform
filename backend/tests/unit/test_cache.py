"""Unit tests for the Redis job cache."""

import uuid
from unittest.mock import AsyncMock

from app.utils.cache import JobCache

_JOB_ID = uuid.uuid4()
_JOB_DATA = {"id": str(_JOB_ID), "status": "pending", "type": "csv_upload"}


async def test_get_returns_none_on_miss() -> None:
    redis = AsyncMock()
    redis.get.return_value = None
    result = await JobCache.get(redis, _JOB_ID)
    assert result is None


async def test_get_returns_dict_on_hit() -> None:
    import json

    redis = AsyncMock()
    redis.get.return_value = json.dumps(_JOB_DATA)
    result = await JobCache.get(redis, _JOB_ID)
    assert result == _JOB_DATA


async def test_get_returns_none_on_redis_error() -> None:
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("Redis down")
    result = await JobCache.get(redis, _JOB_ID)
    assert result is None  # fail-safe, not an exception


async def test_set_calls_redis_set_with_ttl() -> None:
    redis = AsyncMock()
    await JobCache.set(redis, _JOB_ID, _JOB_DATA)
    redis.set.assert_awaited_once()
    _, kwargs = redis.set.await_args  # type: ignore[misc]
    assert "ex" in kwargs
    assert kwargs["ex"] > 0


async def test_set_silently_ignores_redis_error() -> None:
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError("Redis down")
    await JobCache.set(redis, _JOB_ID, _JOB_DATA)  # should not raise


async def test_delete_calls_redis_delete() -> None:
    redis = AsyncMock()
    await JobCache.delete(redis, _JOB_ID)
    redis.delete.assert_awaited_once()


async def test_delete_silently_ignores_redis_error() -> None:
    redis = AsyncMock()
    redis.delete.side_effect = ConnectionError("Redis down")
    await JobCache.delete(redis, _JOB_ID)  # should not raise
