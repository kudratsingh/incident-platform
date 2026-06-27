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
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models.enums import JobStatus
from app.models.job import Job
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession


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
            "creation. Waiting jobs (held by the DAG) are excluded."
        ),
        target=0.95,
        window_hours=24,
        runbook_id="rb-slo-dispatch-latency",
        latency_threshold_seconds=30.0,
    ),
]


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
    Counts only jobs that have left PENDING (started or terminally failed before
    starting) so jobs still queued don't drag the metric.
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

    stmt = select(total_expr, failed_expr).where(
        Job.created_at >= since,
        # Only count jobs that aren't still WAITING in the DAG.
        Job.status != JobStatus.WAITING,
        Job.status != JobStatus.PENDING,
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
        Job.created_at >= since,
        Job.status != JobStatus.WAITING,
        Job.status != JobStatus.PENDING,
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
