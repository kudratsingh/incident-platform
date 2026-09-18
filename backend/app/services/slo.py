"""
Service Level Objectives + error-budget tracking, computed from the jobs table.

budget_remaining_pct = (1 - (failed/total) / (1 - target)) * 100; burn_rate = failure_rate
/ (1 - target), so 14.4× is the fast-burn threshold and matches infra/cloudwatch.tf.
`run_evaluation` is the scheduled entry point (`_slo_evaluation_loop`): one critical Alert
per fast-burning objective, deduped by `Alert.dedup_key`. Cancelled jobs and declared lab
fixtures are in neither half of any fraction (WO-R2-132).
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
from app.utils.post_commit import run_post_commit
from sqlalchemy import TextClause, case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)

# Burn rate at which we page rather than record. Must match the two fast-burn
# alarms in `infra/cloudwatch.tf` or dashboard and alert disagree.
FAST_BURN_THRESHOLD = 14.4


@dataclass(frozen=True, slots=True)
class SLODefinition:
    """One promise the platform makes, and the window it is measured over."""

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


# The dispatch-latency denominator: statuses meaning the job left PENDING and so has a
# dispatch outcome. An allowlist, not the `!= WAITING AND != PENDING` pair it replaces
# (finding 2) — that pair admitted CANCELLED rows, which never left PENDING, so a saga
# rollback or a `cascade_cancel_blocked_children` fan-out (R2-09) burnt budget while
# nothing was slow. Allowlisting also keeps a status added later out until someone decides.
_DISPATCHED_STATUSES = (
    JobStatus.RUNNING,
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.DEAD_LETTER,
)


# Payload keys a lab writer stamps on a row it INSERTed in a terminal state, and the whole
# of WO-R2-132: `eval_fixture` from `scripts/seed_eval_fixtures.py`, `seeded_fixture` from
# the chaos hooks (`reset_eval_state` DELETEs exactly those rows). Such a row is evidence
# about the seeder, not the platform — and before this exclusion the eval world was a
# 14.4x fast burn by construction (4 of its 5 terminal jobs dead-lettered, 20% against a
# 99% target), so the only fix was switching the evaluator off. Unconditional, with no
# setting: nothing outside the lab writes these markers.
_LAB_FIXTURE_PAYLOAD_MARKERS = ("eval_fixture", "seeded_fixture")


def _not_a_lab_fixture(session: AsyncSession) -> TextClause:
    """`True` for every row that is NOT declared lab scaffolding.

    Dialect-branched because the predicate has no portable spelling: JSONB containment on
    Postgres (safe against a hostile `{"eval_fixture": "banana"}`), `json_extract(...) = 1`
    on SQLite. `COALESCE` per arm is load-bearing — `payload` is nullable, and NULL would
    drop every payload-less job out of the denominator.
    """
    if session.get_bind().dialect.name == "postgresql":
        arms = [
            f"""COALESCE(jobs.payload @> '{{"{marker}": true}}'::jsonb, false)"""
            for marker in _LAB_FIXTURE_PAYLOAD_MARKERS
        ]
    else:
        arms = [
            f"COALESCE(json_extract(jobs.payload, '$.{marker}'), 0) = 1"
            for marker in _LAB_FIXTURE_PAYLOAD_MARKERS
        ]
    return text("NOT (" + " OR ".join(arms) + ")")


def _dispatched_in_window(
    session: AsyncSession, since: datetime
) -> tuple[Any, ...]:
    """The dispatch-latency denominator, as WHERE clauses.

    One definition for both paths — `_compute_latency_slo` (SQL) and
    `_compute_latency_slo_python` (the unit suite). Written twice it would drift untested.
    """
    return (
        Job.created_at >= since,
        Job.status.in_(_DISPATCHED_STATUSES),
        _not_a_lab_fixture(session),
    )


@dataclass(frozen=True, slots=True)
class SLOState:
    """How one objective is doing now: its counts, the budget left and the burn rate."""

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
    """Measure every declared objective over its own window."""
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
    """Share of settled jobs in the window that completed rather than died.
    Cancellations and seeded lab rows are in neither half of the fraction."""
    since = datetime.now(UTC) - timedelta(hours=slo.window_hours)
    total_expr = func.count().label("total")
    failed_expr = func.sum(
        case((Job.status == JobStatus.DEAD_LETTER, 1), else_=0)
    ).label("failed")

    stmt = select(total_expr, failed_expr).where(
        Job.created_at >= since,
        Job.status.in_([JobStatus.COMPLETED, JobStatus.DEAD_LETTER]),
        _not_a_lab_fixture(session),
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

    Counts only `_DISPATCHED_STATUSES`, so queued and cancelled jobs stay out of the
    denominator; within that set `started_at IS NULL` is a genuine dispatch miss. Declared
    lab fixtures are out too (`_LAB_FIXTURE_PAYLOAD_MARKERS`).
    """
    assert slo.latency_threshold_seconds is not None
    since = datetime.now(UTC) - timedelta(hours=slo.window_hours)

    # Latency in seconds, with the Python fallback below where `extract` is unsupported.
    latency_s = func.extract("epoch", Job.started_at - Job.created_at)

    total_expr = func.count().label("total")
    failed_expr = func.sum(
        case(
            (Job.started_at.is_(None), 1),
            (latency_s > slo.latency_threshold_seconds, 1),
            else_=0,
        )
    ).label("failed")

    stmt = select(total_expr, failed_expr).where(
        *_dispatched_in_window(session, since)
    )
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
        *_dispatched_in_window(session, since)
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
    """The de-duplication identity of one fast-burn alert: a sustained burn is one condition.

    The key carries a time bucket (`floor(now / window)`) rather than a lookup against
    `fired_at`, because `worker_loop` runs in every replica and check-then-act would let two
    both insert; the unique constraint on `(tenant_id, dedup_key)` settles it in the DB.
    """
    bucket = int(now.timestamp() // window_seconds)
    return f"slo:{slo_id}:fast_burn:{bucket}"


def is_fast_burning(state: SLOState) -> bool:
    """Whether this objective is burning fast enough to be worth waking someone.

    `_state` reports an idle objective as healthy at burn 0.0, so an empty platform stays
    quiet; an unreachable `target == 1.0` yields `inf`, correctly above the threshold.
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

    Its own session per objective, so one objective's write failure or dedup conflict cannot
    roll back or poison another's. The tenant is the platform tenant because `compute_all`
    has no tenant filter — pinning a platform-wide burn on one customer would be a lie.
    """
    dedup_key = _fast_burn_dedup_key(
        state.definition.id, dedup_window_seconds, datetime.now(UTC)
    )
    try:
        async with session_factory() as session:
            async with session.begin():
                service = AlertService(AlertRepository(session))
                alert = await service.create_alert(
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
            # This loop owns `session.begin()`, so it owns the drain: the
            # webhook is queued, not POSTed inside the transaction (WO-R2-70).
            # After the block on purpose — a dedup conflict unwinds it, so a
            # suppressed alert is never delivered.
            await run_post_commit(session)
            return alert
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

    Returns the ids of the alerts created. Separate sessions for read and alert, so an
    outbound webhook call cannot pin the `jobs` read's connection.
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
