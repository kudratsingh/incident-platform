"""The `chaos:pause:<loop>` mechanism, and the closed set of loops it names.

Counterpart to `kafka_consumer.kill_key_for` / `_check_chaos_kill`, which do
the same job one layer over: a Redis flag with a TTL, read at the top of every
iteration, that stops one unit of background work without touching the process
around it. That mechanism pauses a **Kafka consumer group**; this one pauses a
**background loop** in `dispatcher.worker_loop`. The two are deliberately
separate, and the split is the whole reason this module exists rather than a
fourth argument on `kill_consumer`:

  * a consumer group is addressed by its group id, an open string — any group
    id at all is a legal argument, and the check lives in `BaseKafkaConsumer`,
    so one code path covers every group that will ever exist;
  * a background loop is a named coroutine in one module. There is no id to
    pass and no shared base class to put the check in, so each loop carries
    its own check and the set of loops is therefore *closed*. `ControlLoopName`
    is that closed set, and it is the safety boundary: a member exists only if
    the matching loop really reads its key, which
    `tests/unit/test_pause_control_loop.py` asserts against
    `dispatcher.worker_loop` by walking the AST rather than by trusting this
    docstring.

Where the check sits inside a loop matters more than it looks:

  * **After the liveness tick, never before it.** `_promote_delayed_loop` calls
    `supervisor.worker_tick()`, which is the heartbeat the deep health check
    reads for *all* the loops (`workers/supervisor.py`). A pause that skipped
    it would report the whole worker wedged — a process-wide signal for a
    single-loop fault, which is both a lie and a blast radius nobody asked
    for.
  * **Inside the outbox relay's leader gate, not in front of it.** The relay is
    single-writer via a Postgres advisory lock (ADR 0020). Checking before the
    gate would make a paused replica stop contending for leadership, handing
    it to another replica; the pause would still hold there, because the key is
    global, but leadership would have moved for a reason unrelated to
    leadership. Checking inside keeps the gate's behaviour identical, paused or
    not.
  * **Before the work, after the sleep.** Three loops (`_digest_loop`,
    `_slo_evaluation_loop`, `_idempotency_reaper_loop`) sleep at the top of
    their body rather than the bottom. The check goes where the work is, so the
    pause is read at the moment it decides something.

Failure posture is **fail open**, matching `_check_chaos_kill`: an unreachable
Redis reads as "not paused" and the loop keeps working. The opposite choice
would let a Redis blip stall the outbox relay in production, which is a real
outage in exchange for a lab convenience. The supervisor's kill-window check
(`_check_chaos_kill_strict`) fails closed for the opposite reason — it is
deciding whether to *resurrect* something chaos stopped, and there "unknown"
must not read as "cleared". Nothing here resurrects anything: the key's own
TTL ends the pause.

Nothing in this module runs unless `CHAOS_ENABLED=true` (ADR 0008 gate 1). The
flag is read in-process on every call and short-circuits before any Redis
round-trip, so a production deployment pays one boolean per tick per loop and
never a network hop.
"""

from enum import StrEnum

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class ControlLoopName(StrEnum):
    """The background loops `pause_control_loop` may pause.

    Closed on purpose (see the module docstring). Every member names a loop
    registered by `dispatcher.worker_loop` that reads
    `pause_key_for(<member>)` on each iteration; `LOOP_FUNCTIONS` below maps
    each one to the coroutine that does the reading.

    The three Kafka consumer groups an earlier draft of this enum carried —
    `dependency_resolver`, `saga_coordinator`, `read_model` — are deliberately
    absent. They are not loops, they are consumer groups, and
    `kill_consumer('<group id>')` has stopped any consumer group since Wave 1.
    A second mechanism for the same thing would mean two keys, two checks and
    two ways for a teardown to miss one.
    """

    OUTBOX_RELAY = "outbox_relay"
    DELAYED_RETRY_PROMOTE = "delayed_retry_promote"
    DLQ_REPLAY_PROMOTE = "dlq_replay_promote"
    RESUME_UNBLOCKED_WAITING = "resume_unblocked_waiting"
    STALE_PENDING_BACKSTOP = "stale_pending_backstop"
    STALE_RUNNING_SWEEP = "stale_running_sweep"
    LEASE_RENEWAL = "lease_renewal"
    SLO_EVALUATION = "slo_evaluation"
    METRICS = "metrics"
    DIGEST = "digest"
    IDEMPOTENCY_REAPER = "idempotency_reaper"


#: Each member, against the `dispatcher` coroutine that reads its key.
#:
#: This mapping is the enum's proof of honesty, and it is why the enum can be
#: trusted as a safety boundary: `test_pause_control_loop.py` parses
#: `workers/dispatcher.py`, collects the loop coroutines `worker_loop` actually
#: starts, and asserts a bijection with the values here — so a twelfth loop
#: cannot ship unpausable and un-enumerated, and a member cannot outlive the
#: loop it names.
LOOP_FUNCTIONS: dict[ControlLoopName, str] = {
    ControlLoopName.OUTBOX_RELAY: "_outbox_relay_loop",
    ControlLoopName.DELAYED_RETRY_PROMOTE: "_promote_delayed_loop",
    ControlLoopName.DLQ_REPLAY_PROMOTE: "_promote_dlq_replay_loop",
    ControlLoopName.RESUME_UNBLOCKED_WAITING: "_resume_unblocked_waiting_loop",
    ControlLoopName.STALE_PENDING_BACKSTOP: "_requeue_stale_pending_loop",
    ControlLoopName.STALE_RUNNING_SWEEP: "_stale_running_sweep_loop",
    ControlLoopName.LEASE_RENEWAL: "_renew_running_leases_loop",
    ControlLoopName.SLO_EVALUATION: "_slo_evaluation_loop",
    ControlLoopName.METRICS: "_metrics_loop",
    ControlLoopName.DIGEST: "_digest_loop",
    ControlLoopName.IDEMPOTENCY_REAPER: "_idempotency_reaper_loop",
}


#: Nominal seconds between iterations of each loop, or `None` where the loop
#: reads its own interval from settings every pass (`digest`,
#: `slo_evaluation` — the tool resolves those at call time).
#:
#: Here rather than imported from `dispatcher`, because `dispatcher` imports
#: this module and the cycle would be real. Mirrored constants with a tripwire
#: test are the house convention for exactly this case (see
#: `reset_eval_state.py`'s literal key mirrors);
#: `test_pause_control_loop.py::test_the_mirrored_tick_intervals_match_the_dispatcher`
#: imports both sides and fails if one moves.
#:
#: What it is for: a pause takes effect on the loop's NEXT tick, so the
#: interval is the difference between a pause the caller can observe and one
#: that expires unnoticed. `pause_control_loop` returns it for the loop it was
#: asked about rather than leaving the caller to guess — an hourly loop and a
#: twice-a-second loop take the same argument and behave nothing alike.
TICK_INTERVAL_SECONDS: dict[ControlLoopName, float | None] = {
    ControlLoopName.OUTBOX_RELAY: 1.0,
    ControlLoopName.DELAYED_RETRY_PROMOTE: 0.5,
    ControlLoopName.DLQ_REPLAY_PROMOTE: 0.5,
    ControlLoopName.RESUME_UNBLOCKED_WAITING: 10.0,
    ControlLoopName.STALE_PENDING_BACKSTOP: 60.0,
    ControlLoopName.STALE_RUNNING_SWEEP: 60.0,
    ControlLoopName.LEASE_RENEWAL: 20.0,
    ControlLoopName.SLO_EVALUATION: None,
    ControlLoopName.METRICS: 60.0,
    ControlLoopName.DIGEST: None,
    ControlLoopName.IDEMPOTENCY_REAPER: 3600.0,
}


def tick_interval_seconds(loop_name: ControlLoopName) -> float | None:
    """Seconds between iterations of one loop, as configured right now.

    `None` means the loop is not currently iterating at all: today that is
    `slo_evaluation` with `SLO_EVALUATION_INTERVAL_SECONDS=0`, which is how the
    demo stack runs it. A pause on a loop that is not iterating changes
    nothing, and the caller is better told that than left to infer it.
    """
    mirrored = TICK_INTERVAL_SECONDS[loop_name]
    if mirrored is not None:
        return mirrored
    settings = get_settings()
    if loop_name is ControlLoopName.DIGEST:
        # `_digest_loop` clamps its own sleep the same way.
        return float(max(60, settings.llm_digest_interval_hours * 3600))
    if loop_name is ControlLoopName.SLO_EVALUATION:
        interval = settings.slo_evaluation_interval_seconds
        return float(interval) if interval > 0 else None
    return None


def pause_key_for(loop_name: ControlLoopName | str) -> str:
    """Redis key the `pause_control_loop` tool sets for one loop.

    Under `chaos:*` like every other key the framework writes, which is what
    makes `scripts/reset_eval_state.py::_CHAOS_KEY_PATTERNS` complete without
    a new pattern — asserted, not assumed, by
    `test_eval_reset.py::test_every_chaos_key_helper_lives_under_the_chaos_namespace`.
    """
    value = loop_name.value if isinstance(loop_name, ControlLoopName) else loop_name
    return f"chaos:pause:{value}"


async def loop_is_paused(loop_name: ControlLoopName) -> bool:
    """Whether this loop should skip its work this iteration.

    Best-effort by design — see the module docstring on fail-open. The Redis
    import is deferred for the same reason `_check_chaos_kill` defers it: unit
    paths that exercise a loop must not have to stand up a Redis client for a
    check that is switched off anyway.
    """
    if not get_settings().chaos_enabled:
        return False
    try:
        from app.core.redis import get_redis_client

        client = get_redis_client()
        paused = await client.get(pause_key_for(loop_name)) is not None
    except Exception:
        return False
    if paused:
        # Debug, not info: the shortest tick here is 0.5s, and a paused loop
        # is a lab state an operator has deliberately created. The evidence
        # that matters lives in the loop's own absent work, not in this line.
        logger.debug(
            "control loop tick skipped — paused",
            extra={"loop": loop_name.value},
        )
    return paused


__all__ = [
    "LOOP_FUNCTIONS",
    "TICK_INTERVAL_SECONDS",
    "ControlLoopName",
    "loop_is_paused",
    "pause_key_for",
    "tick_interval_seconds",
]
