"""The job-creation rate limit is a setting, and its defaults are the literals it
replaced (WO-R3-343): 30 requests per 60s per address, so the 31st is still refused.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.config import Settings, get_settings
from app.core.exceptions import RateLimitError
from app.utils.admission import JOB_CREATE_RATE_BUCKET, job_create_rate_limiter


class _CountingRedis:
    """Counts like Redis does, so the limiter can actually trip."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True


def _request(ip: str = "203.0.113.9") -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = ip
    req.headers = {}
    return req


@pytest.fixture
def fresh_settings():  # type: ignore[no-untyped-def]
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_the_literals_they_replaced() -> None:
    settings = Settings(environment="test")
    assert settings.job_create_rate_limit == 30
    assert settings.job_create_rate_window_seconds == 60


async def test_thirty_first_request_in_a_window_is_refused(
    fresh_settings,  # type: ignore[no-untyped-def]
) -> None:
    """The pin on the default behaviour: 30 through, the 31st raises the 429."""
    dependency = job_create_rate_limiter()
    redis = _CountingRedis()
    request = _request()

    for _ in range(30):
        await dependency(request, redis)

    with pytest.raises(RateLimitError) as refused:
        await dependency(request, redis)
    assert refused.value.details == {"limit": 30, "window_seconds": 60}


async def test_bucket_stays_keyed_on_the_callers_address(
    fresh_settings,  # type: ignore[no-untyped-def]
) -> None:
    redis = _CountingRedis()
    await job_create_rate_limiter()(_request("198.51.100.4"), redis)
    key = next(iter(redis.counters))
    assert key.startswith(f"rate:{JOB_CREATE_RATE_BUCKET}:198.51.100.4:")


async def test_a_raised_ceiling_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
    fresh_settings,  # type: ignore[no-untyped-def]
) -> None:
    """What the demo stack needs: JOB_CREATE_RATE_LIMIT=240 lets the producer
    climb the backlog in seconds instead of 40 of them."""
    monkeypatch.setenv("JOB_CREATE_RATE_LIMIT", "240")
    get_settings.cache_clear()
    dependency = job_create_rate_limiter()
    redis = _CountingRedis()
    request = _request()

    for _ in range(240):
        await dependency(request, redis)

    with pytest.raises(RateLimitError):
        await dependency(request, redis)


async def test_window_setting_sizes_the_bucket_and_its_ttl(
    monkeypatch: pytest.MonkeyPatch,
    fresh_settings,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("JOB_CREATE_RATE_LIMIT", "1")
    monkeypatch.setenv("JOB_CREATE_RATE_WINDOW_SECONDS", "10")
    get_settings.cache_clear()
    redis = _CountingRedis()

    await job_create_rate_limiter()(_request(), redis)
    assert list(redis.expires.values()) == [20]  # two windows, per `_check`

    with pytest.raises(RateLimitError) as refused:
        await job_create_rate_limiter()(_request(), redis)
    assert refused.value.details == {"limit": 1, "window_seconds": 10}


async def test_ceiling_is_read_per_request_not_at_import(
    monkeypatch: pytest.MonkeyPatch,
    fresh_settings,  # type: ignore[no-untyped-def]
) -> None:
    """The dependency is built once when the route module loads, so the reading
    has to happen inside the request or no environment could ever change it."""
    monkeypatch.setenv("JOB_CREATE_RATE_LIMIT", "1")
    get_settings.cache_clear()
    dependency = job_create_rate_limiter()
    redis = _CountingRedis()
    request = _request()

    await dependency(request, redis)
    monkeypatch.setenv("JOB_CREATE_RATE_LIMIT", "3")
    get_settings.cache_clear()

    await dependency(request, redis)
    await dependency(request, redis)
    with pytest.raises(RateLimitError):
        await dependency(request, redis)
