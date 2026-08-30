"""Unit tests for the Redis job cache."""

import uuid
from unittest.mock import AsyncMock

from app.utils.cache import JobCache

_JOB_ID = uuid.uuid4()
_TENANT_ID = uuid.uuid4()
_JOB_DATA = {"id": str(_JOB_ID), "status": "pending", "type": "csv_upload"}


def test_key_shape_is_pinned() -> None:
    """The exact key string is load-bearing: the `cache:` namespace is what
    makes the key reachable by the MCP `invalidate_cache_key` compensator
    (E2-02) and the tenant segment is what keeps hits inside one tenant
    (E2-01). Pin it here so a rename can't happen silently."""
    assert JobCache._key("abc", "t1") == "cache:job:t1:abc"


async def test_get_returns_none_on_miss() -> None:
    redis = AsyncMock()
    redis.get.return_value = None
    result = await JobCache.get(redis, _JOB_ID, _TENANT_ID)
    assert result is None


async def test_get_returns_dict_on_hit() -> None:
    import json

    redis = AsyncMock()
    redis.get.return_value = json.dumps(_JOB_DATA)
    result = await JobCache.get(redis, _JOB_ID, _TENANT_ID)
    assert result == _JOB_DATA
    # The lookup key is tenant-scoped — cross-tenant hits are impossible.
    redis.get.assert_awaited_once_with(f"cache:job:{_TENANT_ID}:{_JOB_ID}")


async def test_get_returns_none_on_redis_error() -> None:
    redis = AsyncMock()
    redis.get.side_effect = ConnectionError("Redis down")
    result = await JobCache.get(redis, _JOB_ID, _TENANT_ID)
    assert result is None  # fail-safe, not an exception


async def test_set_calls_redis_set_with_ttl() -> None:
    redis = AsyncMock()
    await JobCache.set(redis, _JOB_ID, _TENANT_ID, _JOB_DATA)
    redis.set.assert_awaited_once()
    args, kwargs = redis.set.await_args  # type: ignore[misc]
    assert args[0] == f"cache:job:{_TENANT_ID}:{_JOB_ID}"
    assert "ex" in kwargs
    assert kwargs["ex"] > 0


async def test_set_silently_ignores_redis_error() -> None:
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError("Redis down")
    await JobCache.set(redis, _JOB_ID, _TENANT_ID, _JOB_DATA)  # should not raise


async def test_delete_calls_redis_delete() -> None:
    redis = AsyncMock()
    await JobCache.delete(redis, _JOB_ID, _TENANT_ID)
    redis.delete.assert_awaited_once_with(f"cache:job:{_TENANT_ID}:{_JOB_ID}")


async def test_delete_silently_ignores_redis_error() -> None:
    redis = AsyncMock()
    redis.delete.side_effect = ConnectionError("Redis down")
    await JobCache.delete(redis, _JOB_ID, _TENANT_ID)  # should not raise


async def test_get_discards_a_payload_that_is_not_a_job_dict() -> None:
    """R2-20: `create_stale_cache` writes a JSON *array*. Under the old
    `cache:` allowlist it could land on a live `cache:job:` key, and
    `json.loads` happily returned the list — which `JobResponse.
    model_validate` then rejected, 500-ing `GET /jobs/{id}` for the
    whole TTL. A corrupt entry must read as a miss so the caller
    degrades to a slower DB read, not an error."""
    import json

    redis = AsyncMock()
    redis.get.return_value = json.dumps(["stale-fixture-deadbeef"])
    assert await JobCache.get(redis, _JOB_ID, _TENANT_ID) is None


async def test_get_discards_a_payload_that_is_not_json() -> None:
    """Same degradation for a non-JSON body — already covered by the
    broad `except`, pinned here so the shape check doesn't narrow it."""
    redis = AsyncMock()
    redis.get.return_value = "not json at all"
    assert await JobCache.get(redis, _JOB_ID, _TENANT_ID) is None
