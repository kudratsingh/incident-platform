"""Contract tests for the middleware's retained fire-and-forget metric tasks.

BaseHTTPMiddleware.dispatch is awkward to drive without a full app, so these
exercise the module-level set + done-callback contract directly. That is the
part RUF006 is about: a bare `asyncio.create_task(...)` keeps no strong
reference, so the loop may garbage-collect the task mid-flight, and any
scheduling error it raises is swallowed silently.
"""

import asyncio
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest
from app.core import middleware


@pytest.fixture(autouse=True)
def _clean_task_set() -> Iterator[None]:
    middleware._metric_tasks.clear()
    yield
    middleware._metric_tasks.clear()


def _schedule(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Schedule exactly the way RequestContextMiddleware.dispatch does."""
    task = asyncio.create_task(coro)
    middleware._metric_tasks.add(task)
    task.add_done_callback(middleware._on_metric_task_done)
    return task


async def test_pending_task_is_retained_and_discarded_when_done() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _emit() -> None:
        started.set()
        await release.wait()

    task = _schedule(_emit())
    await started.wait()

    # Strong reference held while pending — this is what stops the loop from
    # collecting the task before emit_gauge finishes.
    assert task in middleware._metric_tasks

    release.set()
    await task
    await asyncio.sleep(0)  # let the done-callback run

    assert task not in middleware._metric_tasks
    assert len(middleware._metric_tasks) == 0


async def test_failing_task_logs_warning_without_propagating(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _emit_boom() -> None:
        raise RuntimeError("cloudwatch unreachable")

    with caplog.at_level("WARNING"):
        task = _schedule(_emit_boom())
        # Awaiting the *set* rather than the task: the failure must surface in
        # the log, not in whoever scheduled it.
        await asyncio.wait([task])
        await asyncio.sleep(0)

    assert task.done()
    assert task not in middleware._metric_tasks
    assert any("latency metric emit failed" in r.getMessage() for r in caplog.records)


async def test_cancelled_task_is_discarded_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()

    async def _emit_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    with caplog.at_level("WARNING"):
        task = _schedule(_emit_forever())
        await started.wait()
        task.cancel()
        await asyncio.wait([task])
        await asyncio.sleep(0)

    # task.exception() would raise CancelledError, so the callback must guard.
    assert task not in middleware._metric_tasks
    assert not any("latency metric emit failed" in r.getMessage() for r in caplog.records)
