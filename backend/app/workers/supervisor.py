"""
Supervision for the worker task, and the liveness signal the deep health
check reads.

ADR 0009 gave every *consumer* a supervisor. The task that hosts all of them
— `worker_loop`, started from the API lifespan — had none: it was launched
with a bare `asyncio.create_task`, and nothing ever looked at the returned
task again. No done-callback, no restart, no liveness signal. And because
there is no separate worker deployable yet (`docs/ARCHITECTURE.md`), that one
task *is* all job processing in the whole platform.

Two escapes reach it. All 17 loops catch `Exception` around their bodies, so
the surviving paths are:

  * the deferred imports that sit *before* a loop's `while True`
    (`_promote_dlq_replay_loop`, `_digest_loop`, `_idempotency_reaper_loop`) —
    an import regression raises outside every guard, and `asyncio.gather`
    propagates it out of `worker_loop`;
  * a `CancelledError` reaching `_supervise_consumer`, which re-raises it by
    design; `worker_loop` then unwinds and the task ends *cancelled*.

Either way the process kept serving HTTP with zero jobs being dispatched, and
nothing said so: `ConsumerLag`, which both backlog alarms read, is emitted by
`_metrics_loop` — a loop inside the dead worker — and it is deliberately not
emitted when the lag is unknown. A dead worker therefore produces *absent*
datapoints rather than bad ones, and both alarms treat missing data as
`notBreaching` (`infra/cloudwatch.tf` says so outright, and hands this case
to worker supervision). Worker death silenced exactly the metrics that would
have detected it.

**Restart policy.** Mirrors ADR 0009's consumer supervisor, one level up. The
first restart is immediate, because a one-off crash should cost no processing
time; consecutive failures then back off 1s → 30s. A run that lasts
`_STABLE_RUN_SECONDS` resets the ladder, so an outage next week does not
inherit today's backoff. Restarts are unbounded and capped rather than
budgeted: the same "self-heal without a redeploy" posture the Kafka producer
and the consumer supervisors already take. A worker that cannot stay up is
not hidden by that — it is *reported*, below, and the platform recycles the
task.

**Liveness.** `worker_status()` is what `GET /api/v1/health` reads, and it
answers from three sources, cheapest first:

  1. the supervisor's state (`not_started` / `running` / `restarting` /
     `stopped`);
  2. the worker task object itself — `task.done()` is the truth, and it is
     true the instant the worker dies, with no window where a stale recorded
     state reads healthy;
  3. two heartbeats, each with a staleness bound, which are the backstop for
     what the first two cannot see.

The two heartbeats are kept on separate timestamps on purpose:

  * `heartbeat()` — refreshed by this module's own watchdog while the worker
    task is alive. Its silence means the *supervisor* stopped being
    scheduled, which is the one failure `task.done()` cannot report, because
    the thing that would report it is what died.
  * `worker_tick()` — called from `_promote_delayed_loop` in dispatcher.py,
    which turns every 0.5s. Its silence means `worker_loop`'s gather is alive
    but its loops are wedged — on an exhausted connection pool, or a call
    that never returns.

Sharing one timestamp would have made the second signal useless: the watchdog
would keep refreshing it on behalf of loops that had stopped turning, which
is precisely the case worth catching. The tick bound is only enforced once a
tick has actually been observed (`_tick_seen`), so a build without the
dispatcher-side call degrades to the supervisor-only signal instead of
reporting every task unhealthy.

Note what this changes about the deep health check: it no longer means "this
process can reach Postgres and Redis" but "this process can reach Postgres
and Redis *and* is processing jobs". Since ECS and the ALB both probe
`/api/v1/health`, a worker that will not stay up now recycles the task after
the ALB's 3 × 30s unhealthy window — which is why the backoff ceiling (30s)
is set well inside that window: a worker that can recover does so in-process,
long before the platform intervenes.
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
# Latched by the first `worker_tick()` this process ever sees, and never
# cleared. It is what keeps the tick bound from being enforced against a build
# whose dispatcher-side call is absent: an unarmed bound reports nothing,
# where an always-on one would 503 every task in the fleet over a deleted
# line. A health check must not be able to fail the thing it measures.
_tick_seen = False


def heartbeat() -> None:
    """Record that the worker is alive, now.

    Safe to call from anywhere on the event loop — it is one assignment and
    never blocks, so a hot loop can call it every tick.
    """
    _health.last_alive = time.monotonic()


def worker_tick() -> None:
    """Record that a worker loop just came around again.

    Called from `_promote_delayed_loop` (dispatcher.py), which turns every
    `POLL_INTERVAL` (0.5s) and touches both Redis and Postgres on the way. It
    is the answer to a question `heartbeat()` cannot ask: the supervisor's
    watchdog proves the *supervisor* is being scheduled, and `task.done()`
    proves the worker has not ended — neither notices a `worker_loop` whose
    gather is alive but whose every loop is wedged, on an exhausted connection
    pool or a call that never returns.

    One loop is a proxy for the rest, and a deliberately conservative one:
    a wedge that stops this loop for a full minute has almost certainly
    stopped its siblings, and the two dependency probes on the same health
    endpoint already cover the Redis- and Postgres-down cases that could stall
    it for benign reasons.
    """
    global _tick_seen

    _tick_seen = True
    _health.last_tick = time.monotonic()


def worker_status() -> WorkerStatus:
    """Worker liveness for the deep health check.

    Synchronous and I/O-free: the ALB and the ECS container check both call
    the endpoint that calls this, every 30s, and a health check that can
    block is a health check that can lie.
    """
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

    # Enforced only once a tick has actually been seen, so this degrades to
    # the supervisor-only signal rather than to a permanent 503 if the
    # dispatcher-side call ever goes away. `_spawn` clears it on every
    # restart, which keeps a *previous* incarnation's tick from either
    # vouching for the new one or failing it before it has turned once.
    last_tick = _health.last_tick
    if last_tick is not None and now - last_tick > stale_after:
        detail["reason"] = f"no worker loop tick for over {stale_after}s"
        return WorkerStatus(healthy=False, detail=detail)

    return WorkerStatus(healthy=True, detail=detail)


def start(factory: WorkerFactory) -> asyncio.Task[None]:
    """Start the worker and the supervisor that owns it.

    `factory` builds a fresh worker coroutine per attempt — the supervisor
    cannot re-await a coroutine it has already run. Returns the *supervisor*
    task; cancelling it (or calling `stop()`) shuts the worker down.

    The first worker task is created here, synchronously, so there is no
    window in which the state says `running` while no task exists yet.
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
    """Cancel the supervisor and the worker it owns.

    Never raises. The lifespan calls this before `stop_producer()` and the
    Redis pool closes, and a shutdown step that can throw is a shutdown step
    that can strand every later one — which is exactly how a crashed worker
    used to leave the producer and both pools open.
    """
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
    # Restart the tick clock for the new incarnation. Seeded to *now* once
    # this process has seen a tick at all, so a worker that comes back up
    # wedged and never turns is caught by the staleness bound instead of
    # coasting on a `None` that reads as "not enforced".
    _health.last_tick = time.monotonic() if _tick_seen else None
    return task


def _on_worker_done(task: asyncio.Task[None]) -> None:
    """Log every way the worker task can end.

    This is the callback the finding named as missing. The supervisor also
    observes the outcome, but the callback fires even if the supervisor is
    torn down in the same tick — so a worker death can never be a silent
    "Task exception was never retrieved" at garbage-collection time.
    """
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
    """The supervisor is the thing nothing else supervises — if it dies, say
    so. Liveness still degrades on its own (the watchdog stops ticking and
    the heartbeat goes stale), but a silent supervisor death would make that
    much harder to read in the logs."""
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
    """Cancel the worker and wait for it to unwind.

    The wait matters: `worker_loop`'s cancellation path stops the consumers
    and drains in-flight jobs, and dropping that wait would trade a clean
    shutdown for double-executed jobs on the next boot.
    """
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

    `asyncio.wait` rather than `await child`: awaiting a task makes our own
    cancellation cancel it too, which would make "the worker was killed" and
    "we are shutting down" the same event. `asyncio.wait` never touches what
    it waits on, so the two stay distinguishable — a worker cancelled from
    the inside is restarted, and only a cancellation aimed at *us* ends
    supervision.
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
