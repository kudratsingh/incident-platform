"""Unit tests for the backpressure check."""

from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import BackpressureError
from app.utils.backpressure import check_backpressure


async def test_passes_when_redis_has_no_value() -> None:
    redis = AsyncMock()
    redis.get.return_value = None
    await check_backpressure(redis)  # must not raise


async def test_passes_when_lag_under_threshold() -> None:
    redis = AsyncMock()
    redis.get.return_value = b"10"
    await check_backpressure(redis)


async def test_raises_when_lag_over_threshold() -> None:
    redis = AsyncMock()
    redis.get.return_value = b"5000"
    with pytest.raises(BackpressureError) as exc_info:
        await check_backpressure(redis)
    assert exc_info.value.status_code == 503


async def test_passes_when_threshold_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """threshold=0 disables the check entirely — useful in tests."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("BACKPRESSURE_LAG_THRESHOLD", "0")

    try:
        redis = AsyncMock()
        redis.get.return_value = b"99999"
        await check_backpressure(redis)  # must not raise
    finally:
        get_settings.cache_clear()


async def test_garbage_value_is_treated_as_unknown() -> None:
    redis = AsyncMock()
    redis.get.return_value = b"not-a-number"
    await check_backpressure(redis)  # must not raise — fail-open


async def test_str_value_is_decoded() -> None:
    """Redis can return str depending on decode_responses; accept both."""
    redis = AsyncMock()
    redis.get.return_value = "42"
    await check_backpressure(redis)


async def test_dispatcher_consumer_lag_returns_zero_when_not_started() -> None:
    """consumer_lag must not blow up when the consumer hasn't joined yet."""
    from app.workers.dispatcher import JobDispatcherConsumer

    with patch(
        "app.workers.dispatcher.JobDispatcherConsumer.__init__",
        lambda self, *_a, **_kw: None,
    ):
        c = JobDispatcherConsumer(None, None)  # type: ignore[arg-type]
        c._consumer = None  # type: ignore[attr-defined]
        assert await c.consumer_lag() == 0
