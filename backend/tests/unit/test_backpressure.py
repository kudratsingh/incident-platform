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


async def test_dispatcher_consumer_lag_returns_none_when_not_started() -> None:
    """consumer_lag returns None (not 0) when the consumer hasn't joined
    yet — "unknown" must not be confused with "healthy" (FIX_PLAN #2).
    Metrics loop skips the cache write on None so backpressure fails
    open on absence."""
    from app.workers.dispatcher import JobDispatcherConsumer

    with patch(
        "app.workers.dispatcher.JobDispatcherConsumer.__init__",
        lambda self, *_a, **_kw: None,
    ):
        c = JobDispatcherConsumer(None, None)  # type: ignore[arg-type]
        c._consumer = None  # type: ignore[attr-defined]
        assert await c.consumer_lag() is None


async def test_dispatcher_consumer_lag_returns_none_on_empty_assignment() -> None:
    """A consumer that's started but hasn't been assigned any partitions
    yet also returns None. Distinct from `lag=0` which means "assigned
    partitions, everything consumed"."""
    from app.workers.dispatcher import JobDispatcherConsumer

    with patch(
        "app.workers.dispatcher.JobDispatcherConsumer.__init__",
        lambda self, *_a, **_kw: None,
    ):
        c = JobDispatcherConsumer(None, None)  # type: ignore[arg-type]
        c._consumer = AsyncMock()  # type: ignore[attr-defined]
        c._consumer.assignment = lambda: set()  # empty set == no assignment
        assert await c.consumer_lag() is None


async def test_dispatcher_consumer_lag_returns_none_on_kafka_error() -> None:
    """A Kafka query that raises returns None, never 0. A dead consumer
    can't be allowed to look healthy in the cache."""
    from app.workers.dispatcher import JobDispatcherConsumer

    with patch(
        "app.workers.dispatcher.JobDispatcherConsumer.__init__",
        lambda self, *_a, **_kw: None,
    ):
        c = JobDispatcherConsumer(None, None)  # type: ignore[arg-type]
        c._consumer = AsyncMock()  # type: ignore[attr-defined]
        c._consumer.assignment = lambda: {"tp"}
        c._consumer.end_offsets = AsyncMock(side_effect=RuntimeError("kafka down"))
        assert await c.consumer_lag() is None


async def test_metrics_loop_skips_cache_write_when_lag_is_none() -> None:
    """The metrics loop must not overwrite the lag cache key with a
    fabricated 0 when the consumer reports unknown. The previous key
    TTLs out naturally and backpressure fails open on absence."""
    from unittest.mock import MagicMock

    from app.workers.dispatcher import _metrics_loop

    redis = AsyncMock()
    consumer = MagicMock()
    consumer.in_flight = set()
    consumer.consumer_lag = AsyncMock(return_value=None)  # unknown

    # asyncio.sleep is the first await in the loop — patch it to trigger
    # CancelledError on the second call so the loop exits after one
    # iteration.
    import asyncio as _asyncio

    call_count = {"n": 0}

    async def _sleep_once(_):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise _asyncio.CancelledError

    with (
        patch("app.workers.dispatcher.asyncio.sleep", _sleep_once),
        patch("app.workers.dispatcher.queue.delayed_length", AsyncMock(return_value=0)),
        patch("app.workers.dispatcher.metrics.emit_gauge", AsyncMock()) as gauge,
    ):
        await _metrics_loop(redis, consumer)

    # ConsumerLag gauge NOT emitted when unknown; QueueDepth + InFlightJobs still are.
    emitted = {c.args[0] for c in gauge.await_args_list}
    assert "ConsumerLag" not in emitted
    assert "QueueDepth" in emitted
    # Redis cache never written on the unknown path.
    redis.set.assert_not_awaited()
