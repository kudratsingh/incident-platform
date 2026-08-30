"""Worker-task supervision and its liveness surface on the deep health check
(WO-R2-10).

These tests drive the **real** `app.main.lifespan` with every boot dependency
patched out, because the defect only exists there: `worker_loop` is started
with a bare `asyncio.create_task` and then nobody ever looks at the task
again.  Nothing else in the suite runs the lifespan (`httpx.ASGITransport`
skips startup/shutdown), which is exactly why a dead worker had no test that
could see it.

The tests deliberately assert on *behaviour* rather than on the supervisor's
API, so they are red against the unsupervised code for the right reason — a
worker that stays dead, a health check that stays green, a shutdown that
skips its cleanup — rather than on an import error.  The one exception is
`test_a_stale_heartbeat_marks_the_worker_unhealthy`, which reaches for the
new module directly (locally imported, so collection still works without it).
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.main import create_app, lifespan
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Boot harness
# ---------------------------------------------------------------------------


class _FakeConn:
    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _FakeEngine:
    """Stands in for `app.dependencies._engine` so the health check's
    `SELECT 1` succeeds and the only interesting dimension is the worker."""

    def connect(self) -> Any:
        @asynccontextmanager
        async def _cm() -> AsyncGenerator[_FakeConn, None]:
            yield _FakeConn()

        return _cm()


class _Boot:
    """The patched-out boot dependencies a test wants to assert on."""

    def __init__(self) -> None:
        self.stop_producer = AsyncMock()
        self.close_redis_pool = AsyncMock()
        self.close_sse_redis_pool = AsyncMock()
        self.shutdown_error: BaseException | None = None


def _patch_boot(monkeypatch: pytest.MonkeyPatch, worker: Callable[..., Any]) -> _Boot:
    boot = _Boot()
    redis = AsyncMock()
    monkeypatch.setattr(
        "app.core.migration_check.assert_migrations_current", AsyncMock()
    )
    monkeypatch.setattr("app.core.rls_check.assert_rls_posture", AsyncMock())
    monkeypatch.setattr("app.dependencies.get_session_factory", lambda: MagicMock())
    monkeypatch.setattr("app.dependencies._engine", _FakeEngine())
    monkeypatch.setattr("app.workers.kafka_producer.start_producer", AsyncMock())
    monkeypatch.setattr("app.workers.kafka_producer.stop_producer", boot.stop_producer)
    monkeypatch.setattr("app.core.metrics.start_metrics_emitter", AsyncMock())
    monkeypatch.setattr("app.core.metrics.stop_metrics_emitter", AsyncMock())
    monkeypatch.setattr("app.workers.dispatcher.worker_loop", worker)
    monkeypatch.setattr("app.main.get_redis_client", MagicMock(return_value=redis))
    monkeypatch.setattr("app.core.redis.get_redis_client", MagicMock(return_value=redis))
    monkeypatch.setattr("app.main.close_redis_pool", boot.close_redis_pool)
    monkeypatch.setattr("app.main.close_sse_redis_pool", boot.close_sse_redis_pool)
    monkeypatch.setattr("app.main.reset_broker", MagicMock())
    return boot


@asynccontextmanager
async def _booted(
    monkeypatch: pytest.MonkeyPatch, worker: Callable[..., Any]
) -> AsyncGenerator[tuple[AsyncClient, _Boot], None]:
    """Run the real lifespan around a client bound to the same app.

    A failing *shutdown* is recorded on `boot.shutdown_error` rather than
    raised, so each test fails on its own claim instead of on the unrelated
    (and separately tested) re-raise at `await worker_task`.
    """
    boot = _patch_boot(monkeypatch, worker)
    app = create_app()
    started = lifespan(app)
    await started.__aenter__()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client, boot
    finally:
        try:
            await started.__aexit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 — see docstring
            boot.shutdown_error = exc


async def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll `predicate` until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Worker doubles
# ---------------------------------------------------------------------------


class _CrashingWorker:
    """A worker_loop that dies on every attempt — the boot-time import
    regression / leaked exception case."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        raise RuntimeError("worker boom")


class _SelfCancellingWorker:
    """First attempt takes a CancelledError from the inside (the escape the
    verifier narrowed: a cancellation reaching `_supervise_consumer` unwinds
    the whole gather); later attempts run forever."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        if self.calls == 1:
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
        await asyncio.Event().wait()


class _HealthyWorker:
    """Runs until cancelled, like the real worker_loop."""

    def __init__(self) -> None:
        self.calls = 0
        self.cancelled = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


# ---------------------------------------------------------------------------
# (a) the death is observed and logged
# ---------------------------------------------------------------------------


async def test_a_dead_worker_task_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _CrashingWorker()
    with caplog.at_level("ERROR"):
        async with _booted(monkeypatch, worker):
            observed = await _wait_for(
                lambda: any(
                    "worker task" in record.getMessage() for record in caplog.records
                )
            )
    assert observed, (
        "the worker task died and nothing logged it — "
        f"records: {[r.getMessage() for r in caplog.records]}"
    )
    assert any(
        getattr(record, "error_type", None) == "RuntimeError"
        for record in caplog.records
    ), "the log line does not carry the exception that killed the worker"


# ---------------------------------------------------------------------------
# (c) the worker is restarted
# ---------------------------------------------------------------------------


async def test_c_a_crashed_worker_is_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _CrashingWorker()
    async with _booted(monkeypatch, worker):
        restarted = await _wait_for(lambda: worker.calls >= 2)
    assert restarted, (
        f"worker_loop was entered {worker.calls}x — a crashed worker is never "
        "restarted, so the process serves traffic with zero job processing"
    )


async def test_c_a_cancelled_worker_is_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _SelfCancellingWorker()
    async with _booted(monkeypatch, worker):
        restarted = await _wait_for(lambda: worker.calls >= 2)
    assert restarted, (
        f"worker_loop was entered {worker.calls}x — a CancelledError leaking "
        "out of a consumer supervisor kills the worker for good"
    )


async def test_an_orderly_shutdown_does_not_restart_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart guard: supervision must not resurrect a worker that was
    cancelled *by shutdown* (the mistake ADR 0009 calls out for consumers)."""
    worker = _HealthyWorker()
    async with _booted(monkeypatch, worker):
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
    assert worker.calls == 1
    assert worker.cancelled == 1, "shutdown must still cancel the worker task"


# ---------------------------------------------------------------------------
# (b) the deep health check sees it
# ---------------------------------------------------------------------------


async def test_b_deep_health_check_reports_a_dead_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _worker_is_reported_dead(client: AsyncClient) -> bool:
        response = await client.get("/api/v1/health")
        seen["status"] = response.status_code
        seen["body"] = response.json()
        return response.status_code == 503 and seen["body"].get("worker") == "error"

    worker = _CrashingWorker()
    async with _booted(monkeypatch, worker) as (client, _boot):
        deadline = time.monotonic() + 2.0
        unhealthy = False
        while time.monotonic() < deadline:
            if await _worker_is_reported_dead(client):
                unhealthy = True
                break
            await asyncio.sleep(0.02)

    assert unhealthy, (
        "the deep health check that governs ECS/ALB restarts stayed green "
        f"with a dead worker — last saw {seen}"
    )


async def test_b_deep_health_check_stays_green_with_a_live_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _HealthyWorker()
    async with _booted(monkeypatch, worker) as (client, _boot):
        await asyncio.sleep(0.05)
        response = await client.get("/api/v1/health")
        body = response.json()

    assert response.status_code == 200, body
    assert body["worker"] == "ok", body
    assert body["db"] == "ok" and body["redis"] == "ok", body


async def test_a_stale_heartbeat_marks_the_worker_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker task that is still *alive* but has stopped heartbeating (a
    wedged event loop, or a supervisor that died and stopped ticking) is
    unhealthy once the staleness bound passes."""
    from app.workers import supervisor

    worker = _HealthyWorker()
    async with _booted(monkeypatch, worker) as (client, _boot):
        await asyncio.sleep(0.05)
        assert (await client.get("/api/v1/health")).status_code == 200
        supervisor._health.last_alive = time.monotonic() - 3600
        response = await client.get("/api/v1/health")
        body = response.json()

    assert response.status_code == 503, body
    assert body["worker"] == "error", body


async def test_a_stale_worker_tick_marks_the_worker_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second signal: the task is alive and the supervisor is still
    ticking, but the worker's own loops have stopped turning."""
    from app.workers import supervisor

    worker = _HealthyWorker()
    async with _booted(monkeypatch, worker) as (client, _boot):
        await asyncio.sleep(0.05)
        # Arm the bound the way the dispatcher does, then let it go stale
        # while the supervisor's own heartbeat stays fresh.
        supervisor.worker_tick()
        assert (await client.get("/api/v1/health")).status_code == 200
        supervisor._health.last_tick = time.monotonic() - 3600
        supervisor.heartbeat()
        response = await client.get("/api/v1/health")
        body = response.json()

    assert response.status_code == 503, body
    assert body["worker"] == "error", body
    assert "tick" in body["worker_detail"]["reason"], body


async def test_the_promote_loop_reports_a_worker_tick() -> None:
    """The dispatcher-side half of the contract: the loop chosen to carry
    liveness actually calls it, on every pass and before its own work."""
    from app.workers import dispatcher, supervisor

    supervisor._health.last_tick = None
    ticks = 0

    async def _one_pass(*_args: Any, **_kwargs: Any) -> None:
        nonlocal ticks
        ticks += 1

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dispatcher, "_promote_delayed_once", _one_pass)
        patch.setattr(dispatcher, "POLL_INTERVAL", 0.01)
        task = asyncio.create_task(
            dispatcher._promote_delayed_loop(MagicMock(), AsyncMock())
        )
        await _wait_for(lambda: ticks >= 2, timeout=1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert ticks >= 2, "the promote loop never ran"
    assert supervisor._health.last_tick is not None, (
        "_promote_delayed_loop turned without reporting a worker tick — the "
        "health check's 'loops are alive' signal has no source"
    )


# ---------------------------------------------------------------------------
# Shutdown must not be aborted by the worker's stored exception
# ---------------------------------------------------------------------------


async def test_shutdown_closes_producer_and_pools_when_the_worker_died(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`await worker_task` on a task that already stored an exception re-raises
    it, which used to abort the rest of the lifespan — leaving the Kafka
    producer and both Redis pools open on every shutdown after a worker
    crash."""
    worker = _CrashingWorker()
    boot = _patch_boot(monkeypatch, worker)
    app = create_app()
    async with lifespan(app):
        await asyncio.sleep(0.05)

    boot.stop_producer.assert_awaited_once()
    boot.close_redis_pool.assert_awaited_once()
    boot.close_sse_redis_pool.assert_awaited_once()
