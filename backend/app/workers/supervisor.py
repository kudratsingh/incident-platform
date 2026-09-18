"""
Supervision for the worker task, and the liveness signal the deep health check reads. ADR 0009 gave
consumers a supervisor; `worker_loop` had none, so its death was silent — a dead worker stops
emitting `ConsumerLag`, and both alarms treat missing data as `notBreaching`. Restarts are immediate
then 1s → 30s, cleared by a `_STABLE_RUN_SECONDS` run. Liveness reads supervisor state,
`task.done()`, then `heartbeat()` (watchdog) and `worker_tick()` (dispatcher) — two on purpose.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.core import metrics
from app.core.logging import get_logger

logger = get_logger(__name__)

# The first restart is immediate; the ladder starts at the second consecutive
# failure and is capped, matching `_restart_consumer` in dispatcher.py.
_RESTART_BACKOFF_BASE_SECONDS = 1.0
_RESTART_BACKOFF_MAX_SECONDS = 30.0
# A run at least this long is "stable" and clears the backoff ladder.
_STABLE_RUN_SECONDS = 60.0

NOT_STARTED = "not_started"
RUNNING = "running"
RESTARTING = "restarting"
STOPPED = "stopped"

WorkerFactory = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class _WorkerHealth:
    """What the supervisor currently believes about the worker loop."""

    state: str = NOT_STARTED
    last_alive: float | None = None
    # Deliberately a *separate* timestamp from `last_alive`. Sharing one would
    # let the supervisor's watchdog keep refreshing it while every worker loop
    # sat wedged — the exact case the second source exists to catch.
    last_tick: float | None = None
    restarts: int = 0
    last_error: str | None = None


@dataclass
class WorkerStatus:
    """A point-in-time answer for the health check."""

    healthy: bool
    detail: dict[str, Any] = field(default_factory=dict)


_health = _WorkerHealth()
_worker_task: asyncio.Task[None] | None = None
_supervisor_task: asyncio.Task[None] | None = None
# Latched by the first `worker_tick()` and never cleared: an unarmed bound reports nothing, where
# an always-on one would 503 the whole fleet over a deleted dispatcher call.
_tick_seen = False


def heartbeat() -> None:
    """Record that the worker is alive, now. One assignment, never blocks."""
    _health.last_alive = time.monotonic()


def worker_tick() -> None:
    """Record that a worker loop just came around again.

    Called from `_promote_delayed_loop` (dispatcher.py) every 0.5s. It catches the one case
    `heartbeat()` and `task.done()` cannot: a live gather whose every loop is wedged.
    """
    global _tick_seen

    _tick_seen = True
    _health.last_tick = time.monotonic()


def worker_status() -> WorkerStatus:
    """Worker liveness for the deep health check. I/O-free: one that can block can lie."""
    now = time.monotonic()
    last_alive = _health.last_alive
    age = None if last_alive is None else now - last_alive

    detail: dict[str, Any] = {"state": _health.state, "restarts": _health.restarts}
    if age is not None:
        detail["seconds_since_heartbeat"] = round(age, 1)
    if _health.last_tick is not None:
        detail["seconds_since_worker_tick"] = round(now - _health.last_tick, 1)
    if _health.last_error is not None:
        detail["last_error"] = _health.last_error

    if _health.state in (NOT_STARTED, STOPPED):
        return WorkerStatus(healthy=False, detail=detail)

    task = _worker_task
    if task is None or task.done():
        detail["reason"] = "worker task is not running"
        return WorkerStatus(healthy=False, detail=detail)

    stale_after = get_settings().worker_heartbeat_stale_seconds
    if age is None or age > stale_after:
        detail["reason"] = f"no heartbeat for over {stale_after}s"
        return WorkerStatus(healthy=False, detail=detail)

    # Enforced only once a tick has been seen, so a missing dispatcher call degrades to the
    # supervisor-only signal, not a permanent 503. `_spawn` resets it per restart.
    last_tick = _health.last_tick
    if last_tick is not None and now - last_tick > stale_after:
        detail["reason"] = f"no worker loop tick for over {stale_after}s"
        return WorkerStatus(healthy=False, detail=detail)

    return WorkerStatus(healthy=True, detail=detail)


def start(factory: WorkerFactory) -> asyncio.Task[None]:
    """Start the worker and the supervisor that owns it.

    `factory` builds a fresh worker coroutine per attempt. Returns the *supervisor* task;
    cancelling it (or calling `stop()`) shuts the worker down.
    """
    global _supervisor_task

    _health.state = RUNNING
    _health.restarts = 0
    _health.last_error = None
    heartbeat()

    child = _spawn(factory)
    _supervisor_task = asyncio.create_task(
        _supervise(factory, child), name="worker-supervisor"
    )
    _supervisor_task.add_done_callback(_on_supervisor_done)
    return _supervisor_task


async def stop() -> None:
    """Cancel the supervisor and the worker it owns. Never raises — a shutdown step that throws
    strands every later one."""
    global _supervisor_task, _worker_task

    supervisor = _supervisor_task
    if supervisor is not None:
        supervisor.cancel()
        try:
            await supervisor
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                "worker supervisor ended with an exception",
                extra={"error_type": type(exc).__name__, "error": str(exc)[:400]},
            )

    # Belt and braces: if the supervisor died on its own, the worker it
    # spawned may still be running and would otherwise outlive the process's
    # shutdown.
    await _cancel_worker(_worker_task)

    _supervisor_task = None
    _worker_task = None
    _health.state = STOPPED


def _spawn(factory: WorkerFactory) -> asyncio.Task[None]:
    """Create the worker task, with the done-callback attached before anyone
    can await it."""
    global _worker_task

    task = asyncio.create_task(factory(), name="worker-loop")
    task.add_done_callback(_on_worker_done)
    _worker_task = task
    heartbeat()
    # Restart the tick clock, seeded to *now* once a tick has been seen, so a wedged restart is
    # caught.
    _health.last_tick = time.monotonic() if _tick_seen else None
    return task


def _on_worker_done(task: asyncio.Task[None]) -> None:
    """Log every way the worker task can end. Fires even if the supervisor dies in the same tick,
    so a worker death is never a silent "never retrieved"."""
    if task.cancelled():
        # Either shutdown (expected) or a CancelledError that leaked out of a
        # consumer supervisor (not expected, and invisible until now).
        logger.warning("worker task ended: cancelled")
        _health.last_error = "cancelled"
        return

    exc = task.exception()
    if exc is not None:
        _health.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.error(
            "worker task ended with an exception",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:400]},
            exc_info=exc,
        )
        return

    # worker_loop gathers forever; a clean return means something upstream
    # stopped gathering.
    _health.last_error = "returned"
    logger.error("worker task returned unexpectedly")


def _on_supervisor_done(task: asyncio.Task[None]) -> None:
    """Nothing supervises the supervisor, so log its death. Liveness degrades on its own, but
    silently."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "worker supervisor died",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:400]},
            exc_info=exc,
        )


async def _cancel_worker(task: asyncio.Task[None] | None) -> None:
    """Cancel the worker and wait for it to unwind — the wait is what drains in-flight jobs."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error(
            "worker task raised while shutting down",
            extra={"error_type": type(exc).__name__, "error": str(exc)[:400]},
        )


async def _heartbeat_loop() -> None:
    """Refresh the heartbeat while the worker task is alive."""
    interval = max(get_settings().worker_heartbeat_interval_seconds, 0.1)
    while True:
        await asyncio.sleep(interval)
        task = _worker_task
        if task is not None and not task.done():
            heartbeat()


def _backoff_for(attempt: int) -> float:
    """0s for the first restart, then 1s → 30s."""
    if attempt <= 1:
        return 0.0
    ladder: float = _RESTART_BACKOFF_BASE_SECONDS * float(2 ** (attempt - 2))
    return min(ladder, _RESTART_BACKOFF_MAX_SECONDS)


async def _supervise(factory: WorkerFactory, child: asyncio.Task[None]) -> None:
    """Restart the worker until we are told to stop.

    `asyncio.wait`, not `await child`: awaiting would conflate the worker being killed with us
    shutting down, so only a cancellation aimed at *us* ends supervision.
    """
    watchdog = asyncio.create_task(_heartbeat_loop(), name="worker-heartbeat")
    attempt = 0
    try:
        while True:
            started = time.monotonic()
            await asyncio.wait({child})

            ran_for = time.monotonic() - started
            attempt = 0 if ran_for >= _STABLE_RUN_SECONDS else attempt + 1
            delay = _backoff_for(attempt)

            _health.state = RESTARTING
            _health.restarts += 1
            logger.warning(
                "restarting worker task",
                extra={
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "ran_for_seconds": round(ran_for, 1),
                    "restarts": _health.restarts,
                    "last_error": _health.last_error,
                },
            )
            await metrics.emit_count("WorkerRestarts")

            if delay:
                await asyncio.sleep(delay)

            child = _spawn(factory)
            _health.state = RUNNING
    except asyncio.CancelledError:
        # Shutdown. Take the worker with us and wait for its drain.
        await _cancel_worker(child)
        _health.state = STOPPED
        raise
    finally:
        watchdog.cancel()
