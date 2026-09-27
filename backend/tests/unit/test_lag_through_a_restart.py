"""The lag clock keeps ticking through a consumer restart (WO-R3-356, ADR 0040).

The eighth demo take lost 45 s of lag readings: a metrics tick's offset query was in flight on the
consumer object the supervisor was restarting, and it waited out the client's 40-s request timeout.
"""

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.config import Settings
from app.core.consumer_lag import lag_query_timeout_seconds
from app.workers import dispatcher
from app.workers.dispatcher import JobDispatcherConsumer


async def _never_returns(*_a: Any, **_kw: Any) -> Any:
    """An offset query on a client that is being torn down: it does not come back."""
    await asyncio.Event().wait()


def _consumer_with(fake_kafka: Any) -> JobDispatcherConsumer:
    with patch.object(JobDispatcherConsumer, "__init__", lambda self, *_a, **_kw: None):
        c = JobDispatcherConsumer(None, None)  # type: ignore[arg-type]
    c._consumer = fake_kafka
    return c


def _hanging_kafka(*, hang_on: str) -> AsyncMock:
    kafka = AsyncMock()
    kafka.assignment = lambda: {"tp"}
    kafka.end_offsets = AsyncMock(return_value={"tp": 10})
    kafka.committed = AsyncMock(return_value=4)
    setattr(kafka, hang_on, AsyncMock(side_effect=_never_returns))
    return kafka


def _at_interval(seconds: float) -> Any:
    return patch(
        "app.core.consumer_lag.get_settings",
        return_value=Settings(environment="test", metrics_loop_interval_seconds=seconds),
    )


# 1. The restarting flag: no query at all while the supervisor is stopping or starting


async def test_consumer_lag_returns_none_at_once_while_restarting() -> None:
    kafka = _hanging_kafka(hang_on="end_offsets")
    c = _consumer_with(kafka)
    c.restarting = True

    started = time.monotonic()
    # The outer bound only keeps a regression from hanging the suite.
    lag = await asyncio.wait_for(c.consumer_lag(), timeout=1.0)
    elapsed = time.monotonic() - started

    assert lag is None, "a restarting consumer must read as unknown, never as a number"
    assert elapsed < 0.1
    kafka.end_offsets.assert_not_awaited()


async def test_restart_consumer_marks_the_consumer_restarting_around_stop_and_start() -> None:
    seen: list[tuple[str, bool]] = []

    class _Recorder:
        group_id = "worker-dispatcher"
        restarting = False

        async def stop(self) -> None:
            seen.append(("stop", self.restarting))

        async def start(self) -> None:
            seen.append(("start", self.restarting))

    consumer = _Recorder()
    await dispatcher._restart_consumer(consumer)  # type: ignore[arg-type]

    assert seen == [("stop", True), ("start", True)]
    assert consumer.restarting is False, "the flag outlived the restart"


async def test_restart_consumer_holds_the_flag_across_a_failed_start_and_clears_it() -> None:
    """A start() that fails leaves no usable client, so the flag covers the backoff too."""
    flags: list[bool] = []

    class _FailsOnce:
        group_id = "worker-dispatcher"
        restarting = False
        starts = 0

        async def stop(self) -> None:
            flags.append(self.restarting)

        async def start(self) -> None:
            self.starts += 1
            flags.append(self.restarting)
            if self.starts == 1:
                raise ConnectionError("broker unreachable")

    consumer = _FailsOnce()
    with patch("app.workers.dispatcher.asyncio.sleep", new=AsyncMock()):
        await dispatcher._restart_consumer(consumer)  # type: ignore[arg-type]

    assert flags == [True, True, True, True]
    assert consumer.restarting is False


async def test_restart_consumer_clears_the_flag_when_cancelled() -> None:
    class _StopHangs:
        group_id = "worker-dispatcher"
        restarting = False

        async def stop(self) -> None:
            await asyncio.Event().wait()

        async def start(self) -> None:  # pragma: no cover - never reached
            return None

    consumer = _StopHangs()
    task = asyncio.create_task(dispatcher._restart_consumer(consumer))  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    assert consumer.restarting is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert consumer.restarting is False, "a cancelled restart left the lag clock blind"


# 2. The bounded query: a hung client costs one tick, not the client's 40-s request timeout


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(60.0, 2.0), (5.0, 2.0), (3.0, 1.5), (1.0, 0.5), (0.2, 0.5)],
)
def test_lag_query_timeout_is_half_a_tick_capped_at_two_seconds(
    interval: float, expected: float
) -> None:
    settings = Settings(environment="test", metrics_loop_interval_seconds=interval)
    assert lag_query_timeout_seconds(settings) == expected


@pytest.mark.parametrize("hang_on", ["end_offsets", "committed"])
async def test_a_hung_offset_query_returns_none_within_the_bound(hang_on: str) -> None:
    c = _consumer_with(_hanging_kafka(hang_on=hang_on))

    started = time.monotonic()
    with _at_interval(1.0):  # bound = 0.5 s
        lag = await asyncio.wait_for(c.consumer_lag(), timeout=3.0)
    elapsed = time.monotonic() - started

    assert lag is None, "a query that did not answer is unknown, never 0"
    assert 0.4 < elapsed < 1.0


async def test_an_answering_query_still_reports_the_lag() -> None:
    kafka = AsyncMock()
    kafka.assignment = lambda: {"a", "b"}
    kafka.end_offsets = AsyncMock(return_value={"a": 10, "b": 7})
    kafka.committed = AsyncMock(side_effect=lambda tp: {"a": 4, "b": None}[tp])
    c = _consumer_with(kafka)

    with _at_interval(5.0):
        assert await c.consumer_lag() == 6 + 7
