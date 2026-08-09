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


def test_client_key_uses_rightmost_forwarded_hop() -> None:
    """The rightmost XFF hop is the one the trusted proxy (ALB) appended;
    everything to its left is caller-supplied. This test's predecessor
    asserted the leftmost entry — finding E2-05's vulnerability written
    down as a spec."""
    req = _make_request(ip="10.0.0.1", forwarded="203.0.113.5, 10.0.0.1")
    assert _client_key(req) == "10.0.0.1"


def test_client_key_ignores_rotating_forged_prefix() -> None:
    """The E2-05 bypass signature: different client-forged leftmost XFF
    values with the same proxy-appended rightmost hop must land in the SAME
    rate bucket — rotating the forgeable prefix mints no fresh key."""
    first = _make_request(forwarded="6.6.6.6, 198.51.100.7")
    second = _make_request(forwarded="7.7.7.7, 198.51.100.7")
    assert _client_key(first) == "198.51.100.7"
    assert _client_key(second) == "198.51.100.7"


def test_client_key_single_forwarded_entry() -> None:
    req = _make_request(forwarded="198.51.100.7")
    assert _client_key(req) == "198.51.100.7"


def test_client_key_strips_empty_entries_and_whitespace() -> None:
    req = _make_request(forwarded=" , 198.51.100.7, ")
    assert _client_key(req) == "198.51.100.7"


def test_client_key_falls_back_to_direct_ip_on_degenerate_header() -> None:
    req = _make_request(ip="10.0.0.1", forwarded=" ,, ,")
    assert _client_key(req) == "10.0.0.1"


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
