"""The `chaos:pause:<loop>` mechanism, and the closed set of loops it names (ADR 0027).

A background loop has no group id and no shared base class, so each carries its own check and
`ControlLoopName` is a *closed* set — the boundary `tests/unit/test_pause_control_loop.py` proves
against `dispatcher.worker_loop`. Place the check after `supervisor.worker_tick()`, inside the
relay's advisory-lock gate (ADR 0020), and before the work. Fails open; needs `CHAOS_ENABLED=true`.
"""

from enum import StrEnum

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# The docstring below must stay ONE line: Pydantic copies it to `$defs.ControlLoopName.description`
# in `pause_control_loop`'s pinned `inputSchema`. Closed set; `kill_consumer` covers the groups.
class ControlLoopName(StrEnum):
    """One background loop inside the worker process."""

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
#: `test_pause_control_loop.py` asserts a bijection with `dispatcher.py` — no unpausable loop.
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


#: Nominal seconds between iterations, `None` where the loop reads its own interval each pass
#: (`digest`, `slo_evaluation`, and `metrics` since WO-R3-338). Mirrored, not imported, because
#: `dispatcher` imports this module;
#: `test_the_mirrored_tick_intervals_match_the_dispatcher` fails if one side moves.
TICK_INTERVAL_SECONDS: dict[ControlLoopName, float | None] = {
    ControlLoopName.OUTBOX_RELAY: 1.0,
    ControlLoopName.DELAYED_RETRY_PROMOTE: 0.5,
    ControlLoopName.DLQ_REPLAY_PROMOTE: 0.5,
    ControlLoopName.RESUME_UNBLOCKED_WAITING: 10.0,
    ControlLoopName.STALE_PENDING_BACKSTOP: 60.0,
    ControlLoopName.STALE_RUNNING_SWEEP: 60.0,
    ControlLoopName.LEASE_RENEWAL: 20.0,
    ControlLoopName.SLO_EVALUATION: None,
    ControlLoopName.METRICS: None,
    ControlLoopName.DIGEST: None,
    ControlLoopName.IDEMPOTENCY_REAPER: 3600.0,
}


def tick_interval_seconds(loop_name: ControlLoopName) -> float | None:
    """Seconds between iterations of one loop, as configured now. `None` means it is not iterating,
    so a pause changes nothing — today `slo_evaluation` with the interval set to 0."""
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
    if loop_name is ControlLoopName.METRICS:
        # The demo stack runs this at 5 s (O-35). Through the same clamp the loop sleeps
        # on, so "resumes in N ticks" is the wait an operator will actually see.
        from app.core.consumer_lag import metrics_interval_seconds

        return metrics_interval_seconds(settings)
    return None


def pause_key_for(loop_name: ControlLoopName | str) -> str:
    """Redis key `pause_control_loop` sets. Under `chaos:*`, so `_CHAOS_KEY_PATTERNS` needs no new
    pattern (`test_every_chaos_key_helper_lives_under_the_chaos_namespace`)."""
    value = loop_name.value if isinstance(loop_name, ControlLoopName) else loop_name
    return f"chaos:pause:{value}"


async def loop_is_paused(loop_name: ControlLoopName) -> bool:
    """Whether this loop should skip its work this iteration. Fails open, and defers the Redis
    import so unit paths need no client for a check that is off anyway."""
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
