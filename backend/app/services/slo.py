"""
Service Level Objectives + error-budget tracking.

Two demonstrable SLOs derived directly from the jobs table — no external
metrics backend needed for the read path. CloudWatch alarms for the same
objectives are defined in infra/cloudwatch.tf and fire independently.

SLO model
=========
Each SLO has:
  - id          stable identifier used by runbooks and alarms
  - target      e.g. 0.99 (99% success rate, or 99% of dispatches within 30s)
  - window      rolling lookback in hours over which `current` is computed
  - runbook_id  pointer into the runbook system for diagnosis steps

Error budget
============
For a success-rate SLO:
    budget_used     = failed / total
    budget_allowed  = 1 - target
    budget_remaining_pct = (1 - budget_used / budget_allowed) * 100

For a latency SLO, "failed" means dispatches above the p95 threshold:
    budget_used     = (#above_threshold) / total
    budget_remaining_pct = (1 - budget_used / (1 - target)) * 100

Burn rate
=========
Burn rate = current_failure_rate / (1 - target). 1.0× means we'll consume
the entire budget over exactly the SLO window; 14.4× means we'd burn the
30-day budget in 2 hours and is the canonical fast-burn alert threshold.

Evaluation and alerting
=======================
`run_evaluation` is the scheduled entry point: it computes every objective
and creates an `Alert` row — and therefore a webhook delivery — when one is
in fast burn. `_slo_evaluation_loop` in `workers/dispatcher.py` calls it on
an interval.

Before this existed, `compute_all` had exactly one caller (a read-only admin
endpoint), so nothing evaluated the objectives on a schedule and no real
platform condition ever produced an alert. The alert webhook is the incident
commander's production trigger and its only producer was a chaos tool, which
meant the commander could only ever be woken by a human pretending.

De-duplication is by `Alert.dedup_key`, which carries a time bucket, under a
unique constraint — see `_fast_burn_dedup_key`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.models.alert import SEVERITY_CRITICAL, Alert
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID
from app.repositories.alert import AlertRepository
from app.services.alerts import AlertService
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)

# Burn rate at which we page rather than merely record. Matches the two
# fast-burn alarms in `infra/cloudwatch.tf` for these same objectives; the
# CloudWatch side and this one must agree, or an operator reading the
# dashboard and an agent reading the alert reach different conclusions about
# the same platform.
FAST_BURN_THRESHOLD = 14.4


@dataclass(frozen=True, slots=True)
class SLODefinition:
    id: str
    name: str
    description: str
    target: float           # e.g. 0.99 for 99%
    window_hours: int
    runbook_id: str
    # For latency SLOs only.
    latency_threshold_seconds: float | None = None


# ---------------------------------------------------------------------------
# Declarations — the source of truth for what we promise our users.
# ---------------------------------------------------------------------------

SLOS: list[SLODefinition] = [
    SLODefinition(
        id="job_completion_rate",
        name="Job completion rate",
        description=(
            "Share of jobs that reach COMPLETED rather than DEAD_LETTER. "
            "Cancelled jobs are excluded from the denominator."
        ),
        target=0.99,
        window_hours=24,
        runbook_id="rb-slo-job-completion",
    ),
    SLODefinition(
        id="job_dispatch_latency",
        name="Job dispatch latency",
        description=(
            "Share of dispatched jobs that left PENDING within 30 seconds of "
            "creation. Waiting jobs (held by the DAG) and cancelled jobs "
            "(saga rollback, or a stranded dependency parent) are excluded — "
            "a job nobody decided to dispatch has no dispatch latency."
        ),
        target=0.95,
        window_hours=24,
        runbook_id="rb-slo-dispatch-latency",
        latency_threshold_seconds=30.0,
    ),
]


# The denominator of the dispatch-latency objective: statuses that mean the
# job left PENDING and therefore has a dispatch outcome to measure.
#
# An allowlist, not the `!= WAITING AND != PENDING` pair it replaces, and the
# difference is the whole of finding 2. That pair admitted CANCELLED, whose
# rows never left PENDING at all — they were cancelled *while* WAITING — so
# every one of them arrived here with `started_at IS NULL` and was counted as
# a dispatch miss. Two mechanisms produce them in bulk: the saga coordinator
# cancelling the remaining steps of a rolling-back saga, and
# `JobRepository.cascade_cancel_blocked_children` cancelling the WAITING
# descendants of a stranded parent (R2-09). A six-step saga rollback therefore
# burnt six dispatches' worth of error budget, and a wide DAG cascade could
# trip the 14.4× fast-burn alarm on its own, while nothing had been slow.
#
# A cancellation is a deliberate decision not to dispatch. It is neither a
# success nor a failure of dispatch latency, so it belongs in neither half of
# the fraction — the completion-rate objective already takes the same view
# ("Cancelled jobs are excluded from the denominator").
#
# Being an allowlist also means a status added later is excluded until someone
# decides it belongs, which is the safe direction: the failure mode of the old
# form was a new status silently becoming a dispatch failure.
_DISPATCHED_STATUSES = (
    JobStatus.RUNNING,
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.DEAD_LETTER,
)


def _dispatched_in_window(since: datetime) -> tuple[Any, ...]:
    """The dispatch-latency denominator, as WHERE clauses.

    One definition for both computation paths. `_compute_latency_slo` takes
    the SQL path on Postgres and falls back to `_compute_latency_slo_python`
    on engines without `EXTRACT(EPOCH FROM ...)` — which is every engine the
    unit suite runs on. A denominator written out twice would therefore have
    production on one copy and the tests on the other, and could drift
    indefinitely without a single test turning red.
    """
    return (Job.created_at >= since, Job.status.in_(_DISPATCHED_STATUSES))


@dataclass(frozen=True, slots=True)
class SLOState:
    definition: SLODefinition
    total: int
    failed: int
    current: float          # current success share, in [0, 1]
    budget_remaining_pct: float
    burn_rate: float
    healthy: bool


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


async def compute_all(session: AsyncSession) -> list[SLOState]:
    out: list[SLOState] = []
    for slo in SLOS:
        if slo.latency_threshold_seconds is not None:
            out.append(await _compute_latency_slo(session, slo))
        else:
            out.append(await _compute_completion_slo(session, slo))
    return out


def _state(slo: SLODefinition, total: int, failed: int) -> SLOState:
    """Wrap raw numerator/denominator counts into the public SLOState shape."""
    if total == 0:
        # No traffic in the window — assume healthy. Don't show 0% success.
        return SLOState(
            definition=slo,
            total=0,
            failed=0,
            current=1.0,
            budget_remaining_pct=100.0,
            burn_rate=0.0,
            healthy=True,
        )

    failure_rate = failed / total
    current = 1.0 - failure_rate
    budget_allowed = 1.0 - slo.target
    if budget_allowed <= 0:
        # SLO target of 100% — any failure is a breach.
        burn_rate = float("inf") if failed > 0 else 0.0
        budget_remaining_pct = -100.0 if failed > 0 else 100.0
    else:
        burn_rate = failure_rate / budget_allowed
        # Cap at -100% (fully consumed) on the low end; clamp >100% to 100%.
        budget_remaining_pct = max(-100.0, (1.0 - failure_rate / budget_allowed) * 100.0)

    healthy = current >= slo.target
    return SLOState(
        definition=slo,
        total=total,
        failed=failed,
        current=current,
        budget_remaining_pct=budget_remaining_pct,
        burn_rate=burn_rate,
        healthy=healthy,
    )


async def _compute_completion_slo(
    session: AsyncSession, slo: SLODefinition
) -> SLOState:
    since = datetime.now(UTC) - timedelta(hours=slo.window_hours)
    total_expr = func.count().label("total")
    failed_expr = func.sum(
        case((Job.status == JobStatus.DEAD_LETTER, 1), else_=0)
    ).label("failed")

    stmt = select(total_expr, failed_expr).where(
        Job.created_at >= since,
        Job.status.in_([JobStatus.COMPLETED, JobStatus.DEAD_LETTER]),
    )
    row = (await session.execute(stmt)).one()
    total = int(row.total or 0)
    failed = int(row.failed or 0)
    return _state(slo, total, failed)


async def _compute_latency_slo(
    session: AsyncSession, slo: SLODefinition
) -> SLOState:
    """
    Failure = dispatch latency exceeded the threshold OR the job never started.

    Counts only the statuses in `_DISPATCHED_STATUSES` — jobs that have left
    PENDING and so have a dispatch outcome to measure. Jobs still queued do
    not drag the metric down, and neither do cancellations, which never left
    PENDING to begin with (see the comment on that tuple).

    Within that set `started_at IS NULL` is a genuine dispatch miss: the job
    reached a terminal state without anything ever claiming it, which is
    exactly what "we failed to dispatch it" means. Outside that set the same
    NULL means "not dispatched *yet*", or "deliberately never dispatched",
    which is why the denominator and not the numerator is where this is fixed.
    """
    assert slo.latency_threshold_seconds is not None
    since = datetime.now(UTC) - timedelta(hours=slo.window_hours)

    # Latency in seconds: started_at - created_at. Postgres has EXTRACT(EPOCH FROM ...)
    # but SQLAlchemy's func.extract works on both Postgres and SQLite via the
    # `julianday` fallback below. We keep it dialect-portable for the test DB.
    latency_s = func.extract("epoch", Job.started_at - Job.created_at)

    total_expr = func.count().label("total")
    failed_expr = func.sum(
        case(
            (Job.started_at.is_(None), 1),
            (latency_s > slo.latency_threshold_seconds, 1),
            else_=0,
        )
    ).label("failed")

    stmt = select(total_expr, failed_expr).where(*_dispatched_in_window(since))
    try:
        row = (await session.execute(stmt)).one()
    except Exception:
        # SQLite under tests doesn't support `extract('epoch', ...)`. Fall back
        # to a Python-side scan — same logic, smaller scale.
        return await _compute_latency_slo_python(session, slo, since)

    total = int(row.total or 0)
    failed = int(row.failed or 0)
    return _state(slo, total, failed)


async def _compute_latency_slo_python(
    session: AsyncSession, slo: SLODefinition, since: datetime
) -> SLOState:
    """Portable fallback for engines that lack EXTRACT(EPOCH FROM ...)."""
    assert slo.latency_threshold_seconds is not None
    stmt = select(Job.created_at, Job.started_at).where(
        *_dispatched_in_window(since)
    )
    total = 0
    failed = 0
    for created_at, started_at in (await session.execute(stmt)).all():
        total += 1
        if started_at is None:
            failed += 1
            continue
        delta = (started_at - created_at).total_seconds()
        if delta > slo.latency_threshold_seconds:
            failed += 1
    return _state(slo, total, failed)


# ---------------------------------------------------------------------------
# Scheduled evaluation + alerting
# ---------------------------------------------------------------------------


def _fast_burn_dedup_key(slo_id: str, window_seconds: float, now: datetime) -> str:
    """The de-duplication identity of one fast-burn alert.

    A sustained burn is one condition, not one condition per tick. With a 5
    minute evaluation interval and no de-duplication, an hour of burning would
    mint twelve alerts and twelve webhook deliveries for a single incident —
    which is how an alerting channel becomes something people mute.

    The key carries a **time bucket** (`floor(now / window)`) rather than being
    a bare `slo:x:fast_burn` looked up against `fired_at > now - window`. That
    lookup would be a check-then-act race: `worker_loop` runs in every API
    replica, so two replicas evaluating the same tick would both find nothing
    and both insert. Bucketing turns the window into part of the identity, so
    the unique constraint on `(tenant_id, dedup_key)` settles the race in the
    database — one replica inserts, the other gets an IntegrityError and knows
    the alert exists.

    The cost is that windows are wall-clock aligned rather than
    since-last-alert: a burn that starts just before a boundary can alert
    twice in quick succession, once for each bucket. That is a bounded and
    visible cost, where a lost race is an unbounded and invisible one.
    """
    bucket = int(now.timestamp() // window_seconds)
    return f"slo:{slo_id}:fast_burn:{bucket}"


def is_fast_burning(state: SLOState) -> bool:
    """Whether this objective is burning fast enough to be worth waking someone.

    `total == 0` cannot reach here: `_state` reports an idle objective as
    healthy with `burn_rate == 0.0`, so an empty platform stays quiet instead
    of paging about a promise nothing has tested.

    An unreachable target (`target == 1.0`) yields `inf`, which compares above
    the threshold — correct, since any failure at all has exhausted that
    budget.
    """
    return state.burn_rate >= FAST_BURN_THRESHOLD


def _alert_description(state: SLOState) -> str:
    d = state.definition
    return (
        f"{d.name} is burning error budget at {state.burn_rate:.1f}× the "
        f"sustainable rate ({state.failed} failed of {state.total} in the last "
        f"{d.window_hours}h, {state.current:.1%} against a {d.target:.0%} "
        f"target). Budget remaining: {state.budget_remaining_pct:.0f}%. "
        f"Runbook: {d.runbook_id}."
    )


async def _raise_fast_burn_alert(
    session_factory: async_sessionmaker[AsyncSession],
    state: SLOState,
    dedup_window_seconds: float,
) -> Alert | None:
    """Create one fast-burn alert, or None if this window already has one.

    Its own session and transaction, per objective: one objective's alert
    failing to write must not roll back another's, and a de-duplication
    conflict must not poison a transaction anything else is sharing.

    The tenant is the platform tenant because these objectives are computed
    platform-wide — `compute_all` has no tenant filter, and the CloudWatch
    alarms for the same objectives are equally platform-scoped. Attributing a
    platform-wide burn to one customer's tenant would be a lie in whichever
    direction it landed. Per-tenant objectives would be a different (and
    larger) feature: different definitions, different targets, and a fan-out
    of alerts; noted in docs/ROADMAP.md rather than half-built here.
    """
    dedup_key = _fast_burn_dedup_key(
        state.definition.id, dedup_window_seconds, datetime.now(UTC)
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                service = AlertService(AlertRepository(session))
                return await service.create_alert(
                    tenant_id=DEFAULT_TENANT_ID,
                    severity=SEVERITY_CRITICAL,
                    source=f"slo:{state.definition.id}",
                    title=f"SLO fast burn: {state.definition.name}",
                    description=_alert_description(state),
                    extra_data={
                        "slo_id": state.definition.id,
                        "runbook_id": state.definition.runbook_id,
                        "burn_rate": round(state.burn_rate, 3)
                        if state.burn_rate != float("inf")
                        else None,
                        "threshold": FAST_BURN_THRESHOLD,
                        "target": state.definition.target,
                        "current": round(state.current, 6),
                        "budget_remaining_pct": round(
                            state.budget_remaining_pct, 2
                        ),
                        "window_hours": state.definition.window_hours,
                        "total": state.total,
                        "failed": state.failed,
                    },
                    dedup_key=dedup_key,
                )
    except IntegrityError:
        # Another replica (or an earlier tick inside this window) already
        # raised it. That is the de-duplication working, not a failure.
        logger.debug(
            "fast-burn alert already raised for this window",
            extra={"slo_id": state.definition.id, "dedup_key": dedup_key},
        )
        return None


async def run_evaluation(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    """Compute every objective and alert on the ones in fast burn.

    The scheduled counterpart to the admin endpoint's read: same computation,
    but it can act on the answer. Returns the ids of the alerts created, which
    is what the loop logs and what the tests assert on.

    Computation and alerting use separate sessions on purpose. The read is one
    short transaction over `jobs`; holding it open across alert writes — each
    of which makes an outbound webhook call — would pin a connection for the
    webhook timeout on every burn.
    """
    settings = get_settings()
    async with session_factory() as session:
        states = await compute_all(session)

    created: list[uuid.UUID] = []
    for state in states:
        if not is_fast_burning(state):
            continue
        logger.warning(
            "SLO fast burn detected",
            extra={
                "slo_id": state.definition.id,
                "burn_rate": state.burn_rate,
                "total": state.total,
                "failed": state.failed,
            },
        )
        alert = await _raise_fast_burn_alert(
            session_factory, state, settings.slo_alert_dedup_window_seconds
        )
        if alert is not None:
            created.append(alert.id)
    return created
