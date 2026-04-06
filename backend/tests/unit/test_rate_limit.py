"""Unit tests for the sliding-window rate limiter."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import RateLimitError
from app.utils.rate_limit import _check, _client_key

# ---------------------------------------------------------------------------
# _client_key
# ---------------------------------------------------------------------------


def _make_request(ip: str = "1.2.3.4", forwarded: str | None = None) -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = ip
    req.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    return req


def test_client_key_uses_direct_ip() -> None:
    req = _make_request(ip="10.0.0.1")
    assert _client_key(req) == "10.0.0.1"


def test_client_key_prefers_forwarded_for() -> None:
    req = _make_request(ip="10.0.0.1", forwarded="203.0.113.5, 10.0.0.1")
    assert _client_key(req) == "203.0.113.5"


# ---------------------------------------------------------------------------
# _check
# ---------------------------------------------------------------------------


async def test_check_allows_under_limit() -> None:
    redis = AsyncMock()
    redis.incr.return_value = 1
    # Should not raise
    await _check(redis, "test-key", limit=10, window=60)
    redis.expire.assert_awaited_once()


async def test_check_allows_at_limit() -> None:
    redis = AsyncMock()
    redis.incr.return_value = 10
    await _check(redis, "test-key", limit=10, window=60)


async def test_check_raises_over_limit() -> None:
    redis = AsyncMock()
    redis.incr.return_value = 11
    with pytest.raises(RateLimitError):
        await _check(redis, "test-key", limit=10, window=60)


async def test_check_sets_ttl_on_first_request() -> None:
    redis = AsyncMock()
    redis.incr.return_value = 1
    await _check(redis, "test-key", limit=10, window=60)
    redis.expire.assert_awaited_once()


async def test_check_skips_ttl_on_subsequent_requests() -> None:
    redis = AsyncMock()
    redis.incr.return_value = 5  # not the first request
    await _check(redis, "test-key", limit=10, window=60)
    redis.expire.assert_not_awaited()
