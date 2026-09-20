"""
Seed the platform with realistic data for the incident-commander agent's live eval suite.

Populates: Redis lag for 8 consumer groups; `deploy_markers` (6 deploys, one annotated for the
deploy-correlation scenario); `alerts` (active + resolved across kafka/dlq/api/db, so the
alert-storm scenario sees >= 3 active); 4 dead-letter jobs with `job_triages` rows; failed jobs
sharing trace_ids plus audit rows sharing the request_id so `get_trace` returns a graph; and a
three-node DAG (parent → seed → child) for `get_dag_state`.

Idempotent: every id is `uuid5(NAMESPACE, name)`, so a re-run finds every row present. The script
prints each pinned id under `EVAL FIXTURE PINS`.

Usage: `make seed-eval-fixtures`. Env vars (optional): `DATABASE_URL`, `REDIS_URL`,
`SEED_TENANT_SLUG`, `EVAL_PINS_PATH` (where `write_pins_json` lands the manifest).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

# Allow running from project root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
# ...and this dir, so the sibling `eval_safety` resolves either import path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_safety  # type: ignore[import-not-found]  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.tenant_scope import platform_session_factory  # noqa: E402
from app.lab.dlq_failure_stories import (  # noqa: E402
    CSV_BAD_ROW,
    PARTNER_RATE_LIMITED,
    SMTP_UNREACHABLE,
    UPSTREAM_TIMEOUT,
    DlqFailureStory,
)
from app.models.alert import Alert  # noqa: E402
from app.models.audit import (  # noqa: E402
    PRINCIPAL_TYPE_USER,
    AuditLog,
)
from app.models.deploy_marker import DeployMarker  # noqa: E402
from app.models.enums import (  # noqa: E402
    JobStatus,
    JobType,
    RemediationHint,
    UserRole,
)
from app.models.job import Job  # noqa: E402
from app.models.job_dependency import JobDependency  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.models.triage import JobTriage  # noqa: E402
from app.models.user import User  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# Deterministic ID space — uuid5(namespace, name), so scenarios can pin ids.
# ---------------------------------------------------------------------------

_NAMESPACE = uuid.UUID("aaaaaaaa-e7a1-4000-8000-000000000000")


def stable(name: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, name)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform",
)
_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
_TENANT_SLUG = os.getenv("SEED_TENANT_SLUG", "default")


class SeedError(RuntimeError):
    """A seed precondition. `Exception`, not `SystemExit`, so the API boot guard
    catches it (WO-R2-69)."""

# Mirror of app.mcp.tools.consumer_lag._CONSUMER_LAG_KEY_PREFIX, kept here so it can't drift.
_LAG_KEY_PREFIX = "kafka:consumer_lag:"
# Written WITHOUT expiry, like every other fixture here (R2-17): a 24h TTL let the keys vanish
# after a day of uptime, failing six scenarios as agent errors. Durability is the reset's job.

# `remediate_stale_cache_success` expects this key to hold recognisably-stale contents, then
# invalidates it via `invalidate_cache_key` and watches the delete (FIX_PLAN #19).
_HOT_SET_KEY = "cache:jobs:worker-dispatcher:hot_set"
# A JSON array of stable job IDs — obviously fake "hot" state.
_HOT_SET_TTL_SECONDS = 24 * 3600

# ---------------------------------------------------------------------------
# Fixture data — spec matches the incident-commander readiness brief
# ---------------------------------------------------------------------------

_CONSUMER_LAGS: dict[str, int] = {
    "billing-consumer": 15_000,
    "orders-consumer": 7_500,
    "notifications-consumer": 500,
    "analytics-consumer": 50_000,
    "payments-consumer": 30_000,
    "shipping-consumer": 100_000,
    "healthy-consumer": 0,
    # worker-dispatcher is owned by the metrics loop — don't overwrite it.
}


# How long a seeded job waited to be dispatched, and the lifecycle columns that follow (WO-R2-69).
# `update_status` stamps `started_at` on PENDING→RUNNING and `completed_at` on every terminal
# write; seeded rows had neither. The dispatch-latency SLO reads `started_at IS NULL` on a
# dispatched job as a miss, so seven fixture rows sat permanently in the denominator — most of it
# on a quiet lab stack. And `list_jobs(sort=DEAD_LETTERED_AT)` orders on
# `coalesce(completed_at, created_at)` (WO-R2-53), so for the graded fixture set "when did it die"
# degraded to "when was it submitted". 4 s is well inside the 30-second objective.
_DISPATCH_LATENCY_SECONDS = 4


def _lifecycle(
    now: datetime, created_offset: timedelta, run_seconds: int | None
) -> tuple[datetime, datetime | None, datetime | None]:
    """(created_at, started_at, completed_at) for one seeded job.

    Shared by the seed and the re-baseline. `run_seconds is None` keeps both NULL — never
    dispatched.
    """
    created_at = now - created_offset
    if run_seconds is None:
        return created_at, None, None
    started_at = created_at + timedelta(seconds=_DISPATCH_LATENCY_SECONDS)
    return created_at, started_at, started_at + timedelta(seconds=run_seconds)


def _dag_specs() -> list[dict[str, object]]:
    """The three-node DAG: parent (completed) → seed (waiting) → child.

    Explicit offsets so the parent is orderable: pinning all three at `now` gave it a
    start before its own creation."""
    return [
        {
            "name": "dag-parent-job",
            "status": JobStatus.COMPLETED.value,
            "created_offset": timedelta(minutes=6),
            "run_seconds": 50,
        },
        {
            "name": "dag-seed-job",
            "status": JobStatus.WAITING.value,
            "created_offset": timedelta(minutes=5),
            "run_seconds": None,
        },
        {
            "name": "dag-child-job",
            "status": JobStatus.WAITING.value,
            "created_offset": timedelta(minutes=5),
            "run_seconds": None,
        },
    ]


def _deploy_rows() -> list[dict[str, object]]:
    """The seeded deploy history. Always `tenant_id=None` (WO-R2-69).

    A deploy marker is platform-wide by construction — the RLS policy is written around
    `tenant_id IS NULL` and every other producer passes None. Stamping a concrete tenant made
    the history invisible to `get_deploy_history`, which the correlation scenarios read.
    """
    now = datetime.now(UTC)
    return [
        {
            "id": stable("deploy-v0.4.0"),
            "tenant_id": None,
            "version": "v0.4.0",
            "revision": "a1940af",
            "image_tag": "v0.4.0",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=22),
            "notes": None,
        },
        {
            "id": stable("deploy-v0.4.1"),
            "tenant_id": None,
            "version": "v0.4.1",
            "revision": "b7c3e51",
            "image_tag": "v0.4.1",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=18),
            "notes": None,
        },
        {
            "id": stable("deploy-v0.4.2-billing-hotfix"),
            "tenant_id": None,
            "version": "v0.4.2",
            "revision": "c9f4d02",
            "image_tag": "v0.4.2",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=6),
            # The correlation hint scenarios probe against.
            "notes": "correlated with billing failures",
        },
        {
            "id": stable("deploy-v0.4.3"),
            "tenant_id": None,
            "version": "v0.4.3",
            "revision": "d1e5a83",
            "image_tag": "v0.4.3",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=2),
            "notes": None,
        },
        {
            "id": stable("deploy-v0.4.3-staging"),
            "tenant_id": None,
            "version": "v0.4.3",
            "revision": "d1e5a83",
            "image_tag": "v0.4.3",
            "environment": "staging",
            "deployed_at": now - timedelta(hours=3),
            "notes": "staging soak before prod",
        },
        {
            "id": stable("deploy-v0.3.9"),
            "tenant_id": None,
            "version": "v0.3.9",
            "revision": "e0f6b14",
            "image_tag": "v0.3.9",
            "environment": "prod",
            "deployed_at": now - timedelta(days=2),
            "notes": None,
        },
    ]


def _alert_rows(tenant_id: uuid.UUID) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    return [
        {
            "id": stable("alert-kafka-active"),
            "tenant_id": tenant_id,
            "severity": "critical",
            "source": "kafka",
            "title": "billing-consumer lag exceeds 10k",
            "description": "Sustained lag on billing-consumer for 20+ minutes.",
            "fired_at": now - timedelta(minutes=25),
            "resolved_at": None,
        },
        {
            "id": stable("alert-dlq-active"),
            "tenant_id": tenant_id,
            "severity": "warning",
            "source": "dlq",
            "title": "DLQ backlog growing",
            "description": "3 send_email jobs dead-lettered in last hour.",
            "fired_at": now - timedelta(minutes=15),
            "resolved_at": None,
        },
        {
            "id": stable("alert-api-active"),
            "tenant_id": tenant_id,
            "severity": "critical",
            "source": "api",
            "title": "5xx rate above threshold",
            "description": "API 5xx rate 3.2% over last 10 minutes.",
            "fired_at": now - timedelta(minutes=8),
            "resolved_at": None,
        },
        {
            "id": stable("alert-kafka-resolved"),
            "tenant_id": tenant_id,
            "severity": "warning",
            "source": "kafka",
            "title": "orders-consumer transient lag",
            "description": "Brief lag spike, recovered.",
            "fired_at": now - timedelta(days=1, hours=4),
            "resolved_at": now - timedelta(days=1, hours=3),
        },
        {
            "id": stable("alert-db-resolved"),
            "tenant_id": tenant_id,
            "severity": "info",
            "source": "db",
            "title": "Postgres connection saturation",
            "description": "Connection pool briefly saturated during backup.",
            "fired_at": now - timedelta(days=2),
            "resolved_at": now - timedelta(days=2, hours=-1),
        },
    ]


def _triage_from(story: DlqFailureStory) -> dict[str, object]:
    """The `job_triages` row that goes with a story.

    `list_dlq_messages` returns it inline, so it is as agent-visible as the error text
    (WO-R2-146)."""
    assert story.triage is not None, f"{story.key} has no triage row"
    return {
        "root_cause_category": story.triage.root_cause_category,
        "summary": story.triage.summary,
        "suggested_fix": story.triage.suggested_fix,
        "is_retryable": story.triage.is_retryable,
        "confidence": story.triage.confidence,
    }


def _dlq_specs() -> list[dict[str, object]]:
    """Each spec becomes one Job + one JobTriage row, one per `remediation_hint` category
    the agent branches on:

      * upstream timeout  → replay_safe        (blip, replay as-is)
      * SMTP / rate limit → wait_and_replay    (dep refusing, retry later)
      * csv bad-data      → human_required     (persistent bug, escalate)

    Error strings and triage rows come from `app.lab.dlq_failure_stories`, named by
    `story_key`; the ids, job types, retry counts and order are pinned by scenario YAML and
    canned fixtures. WO-R2-146 moved two: the id
    `dlq-job-schema-violation` still means "the replay-safe one" but its story is an upstream
    timeout, and `dlq-job-process-payment` is the rate-limited case rather than a bare 30s
    timeout reading as "retry now".
    """
    return [
        {
            "job_id": stable("dlq-job-schema-violation"),
            "run_seconds": 2,
            "triage_id": stable("dlq-triage-schema-violation"),
            "type": JobType.BULK_API_SYNC.value,
            "remediation_hint": RemediationHint.REPLAY_SAFE.value,
            "story_key": UPSTREAM_TIMEOUT.key,
            "error_message": UPSTREAM_TIMEOUT.error_message,
            "retry_count": 3,
            "created_offset": timedelta(minutes=8),
            "triage": _triage_from(UPSTREAM_TIMEOUT),
        },
        {
            "job_id": stable("dlq-job-send-email"),
            "run_seconds": 12,
            "triage_id": stable("dlq-triage-send-email"),
            "type": JobType.BULK_API_SYNC.value,
            "remediation_hint": RemediationHint.WAIT_AND_REPLAY.value,
            "story_key": SMTP_UNREACHABLE.key,
            "error_message": SMTP_UNREACHABLE.error_message,
            "retry_count": 3,
            "created_offset": timedelta(minutes=40),
            "triage": _triage_from(SMTP_UNREACHABLE),
        },
        {
            "job_id": stable("dlq-job-process-payment"),
            "run_seconds": 30,
            "triage_id": stable("dlq-triage-process-payment"),
            "type": JobType.BULK_API_SYNC.value,
            "remediation_hint": RemediationHint.WAIT_AND_REPLAY.value,
            "story_key": PARTNER_RATE_LIMITED.key,
            "error_message": PARTNER_RATE_LIMITED.error_message,
            "retry_count": 3,
            "created_offset": timedelta(minutes=25),
            "triage": _triage_from(PARTNER_RATE_LIMITED),
        },
        {
            "job_id": stable("dlq-job-csv-parse"),
            "run_seconds": 18,
            "triage_id": stable("dlq-triage-csv-parse"),
            "type": JobType.CSV_UPLOAD.value,
            "remediation_hint": RemediationHint.HUMAN_REQUIRED.value,
            "story_key": CSV_BAD_ROW.key,
            "error_message": CSV_BAD_ROW.error_message,
            "retry_count": 3,
            "created_offset": timedelta(minutes=12),
            "triage": _triage_from(CSV_BAD_ROW),
        },
    ]


def _failed_trace_specs() -> list[dict[str, object]]:
    return [
        {
            "job_id": stable("failed-job-trace-a"),
            "run_seconds": 95,
            "trace_id": str(stable("failed-trace-a")),
            "type": JobType.REPORT_GEN.value,
            "error_message": "OOM during PDF generation (200MB report)",
            "created_offset": timedelta(minutes=45),
        },
        {
            "job_id": stable("failed-job-trace-b"),
            "run_seconds": 6,
            "trace_id": str(stable("failed-trace-b")),
            "type": JobType.DOC_ANALYSIS.value,
            "error_message": "pdf extraction failed: file is corrupted",
            "created_offset": timedelta(minutes=30),
        },
    ]


# ---------------------------------------------------------------------------
# Seeding — each function is idempotent via check-then-insert
# ---------------------------------------------------------------------------


async def _ensure_tenant(session: AsyncSession, slug: str) -> Tenant:
    """The tenant every fixture hangs off. Raises `SeedError` when absent.

    A normal exception, not `SystemExit` (WO-R2-69): `SystemExit` derives from
    `BaseException`, so the API's boot-time `except Exception` seed guard could not catch it
    and a missing `SEED_TENANT_SLUG` crash-looped the process. `main()` still turns this into
    a non-zero exit, so the CLI behaviour is unchanged.
    """
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if tenant is None:
        raise SeedError(
            f"tenant slug {slug!r} not found. Run `alembic upgrade head` first."
        )
    return tenant


async def _ensure_seed_user(
    session: AsyncSession, tenant: Tenant
) -> User:
    """DLQ + failed-trace jobs need a user_id. Use a dedicated seed
    account so scenario rows are easy to filter out during debugging."""
    user_id = stable("seed-user")
    existing = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(
        id=user_id,
        tenant_id=tenant.id,
        email=f"eval-fixtures@{tenant.slug}.local",
        hashed_password=hash_password("not-a-real-account"),
        role=UserRole.USER.value,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def _seed_consumer_lag(redis: aioredis.Redis) -> None:
    """Write the consumer-lag fixtures durably; no TTL (`_LAG_KEY_PREFIX`).

    `worker-dispatcher` is absent from `_CONSUMER_LAGS`: the loop owns it."""
    for group, lag in _CONSUMER_LAGS.items():
        await redis.set(f"{_LAG_KEY_PREFIX}{group}", str(lag))


def hot_set_payload() -> str:
    """The exact value `_seed_hot_set` writes.

    Public so the reset can tell an intact key from a drifted one without restating the id
    set — hardcoded names drifted once and left phantom UUIDs (D-14), and after WO-R3-310
    a second reader needs the same answer.
    """
    import json as _json

    # From `_dlq_specs()`, so every member names a real seeded job.
    return _json.dumps([str(spec["job_id"]) for spec in _dlq_specs()[:3]])


async def _seed_hot_set(redis: aioredis.Redis) -> None:
    """Populate the `remediate_stale_cache_success` scenario's fixture
    (FIX_PLAN #19). Value is stable + recognisably fake so an operator
    inspecting Redis doesn't confuse it with production cache."""
    await redis.set(_HOT_SET_KEY, hot_set_payload(), ex=_HOT_SET_TTL_SECONDS)


async def _reset_dlq_state(session: AsyncSession) -> int:
    """Re-baseline stable() DLQ jobs to their advertised state.

    Live scenarios mutate these rows (status, retry_count on replay, hint), so the second run
    of a scenario otherwise mis-grades (FIX_PLAN #7). Only `_dlq_specs()` stable() ids are
    touched. `error_message` is one of the four restored columns — a replay overwrites it, and
    WO-R2-146 changed these texts, so a stale one puts the old contradiction in front of the
    agent. The `job_triages` row is restored with it.

    Returns the number of fixtures reset (job row, triage row or both counts once)."""
    now = datetime.now(UTC)
    reset_count = 0
    for spec in _dlq_specs():
        job_id = spec["job_id"]
        existing = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if existing is None:
            continue  # never seeded; nothing to reset
        needs_reset = (
            existing.status != JobStatus.DEAD_LETTER.value
            or existing.retry_count != spec["retry_count"]
            or existing.remediation_hint != spec.get("remediation_hint")
            or existing.error_message != spec["error_message"]
        )
        triage_reset = await _reset_triage_state(session, spec)
        if not needs_reset:
            if triage_reset:
                reset_count += 1
            continue
        existing.status = JobStatus.DEAD_LETTER.value
        existing.retry_count = cast("int", spec["retry_count"])
        existing.remediation_hint = cast(
            "str | None", spec.get("remediation_hint")
        )
        existing.error_message = cast("str", spec["error_message"])
        existing.updated_at = now
        reset_count += 1
    return reset_count


async def _reset_triage_state(
    session: AsyncSession, spec: dict[str, object]
) -> bool:
    """Re-baseline one fixture's `job_triages` row. True if it changed.

    `_seed_dlq` inserts one only when none exists, so a stack seeded before a text change
    keeps the old wording forever — and `list_dlq_messages` returns the triage inline, so
    WO-R2-146's contradiction comes back one field lower. Content only."""
    triage_id = spec["triage_id"]
    existing = (
        await session.execute(
            select(JobTriage).where(JobTriage.id == triage_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        return False  # never seeded; `_seed_dlq` inserts it
    want = cast("dict[str, Any]", spec["triage"])
    fields = (
        "root_cause_category",
        "summary",
        "suggested_fix",
        "is_retryable",
        "confidence",
    )
    if all(getattr(existing, f) == want[f] for f in fields):
        return False
    for field in fields:
        setattr(existing, field, want[field])
    return True


# Drift a fixture timestamp may accumulate before a reset re-anchors it. Non-zero so
# back-to-back resets stay no-ops, and far below the tightest eval window (`since_hours=1`).
_REBASELINE_TOLERANCE = timedelta(seconds=60)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise DB-returned datetimes for drift arithmetic. Postgres
    (timestamptz) returns aware values; the SQLite unit harness returns
    naive ones that are UTC by construction."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _drifted(actual: datetime | None, target: datetime | None) -> bool:
    """Whether a stored timestamp sits more than `_REBASELINE_TOLERANCE`
    from its now-relative target (or differs in NULL-ness)."""
    actual = _as_utc(actual)
    if actual is None or target is None:
        return actual is not target
    return abs(actual - target) > _REBASELINE_TOLERANCE


async def _rebaseline_timestamps(session: AsyncSession) -> int:
    """Re-anchor every time-anchored fixture column to its spec offset from *now*, so a reset
    leaves the seeded world as fresh as it was at first boot (BUILD_PLAN 2.5).

    The seeder computes offsets at seed time and check-then-insert never updates them, so two
    days in, `search_traces(since_hours=1)` found nothing and every age-sensitive scenario
    graded an apparently healthy system. Shift, not flatten: each row keeps its own offset.

    Scope is every seeded field an eval can time-assert on: the whole `jobs` lifecycle
    (`created_at`/`updated_at`/`started_at`/`completed_at`, derived together by `_lifecycle`
    for the DLQ, failed-trace and DAG specs), `alerts.fired_at`/`resolved_at`, and
    `deploy_markers.deployed_at`. Excluded on purpose: `audit_logs` (ground truth, ADR 0012
    amendment) and `job_triages` (no timestamp reaches a tool output). Only stable() ids are
    addressed. Rows already within `_REBASELINE_TOLERANCE` of target are skipped; returns the
    number shifted."""
    now = datetime.now(UTC)
    shifted = 0

    for deploy_spec in _deploy_rows():
        marker = (
            await session.execute(
                select(DeployMarker).where(DeployMarker.id == deploy_spec["id"])
            )
        ).scalar_one_or_none()
        if marker is None:
            continue
        deployed_target = cast("datetime", deploy_spec["deployed_at"])
        if _drifted(marker.deployed_at, deployed_target):
            marker.deployed_at = deployed_target
            shifted += 1

    # The tenant argument only lands on inserted rows; ids are stable.
    for alert_spec in _alert_rows(uuid.uuid4()):
        alert = (
            await session.execute(select(Alert).where(Alert.id == alert_spec["id"]))
        ).scalar_one_or_none()
        if alert is None:
            continue
        fired_target = cast("datetime", alert_spec["fired_at"])
        resolved_target = cast("datetime | None", alert_spec["resolved_at"])
        if _drifted(alert.fired_at, fired_target) or _drifted(
            alert.resolved_at, resolved_target
        ):
            alert.fired_at = fired_target
            alert.resolved_at = resolved_target
            shifted += 1

    # The whole lifecycle, not just created_at (WO-R2-69): re-anchoring `created_at` alone
    # left `started_at`/`completed_at` where the previous run stamped them, so rows started
    # before they were created and dispatch latency came out negative.
    job_targets: dict[uuid.UUID, tuple[datetime, datetime | None, datetime | None]] = {}
    for job_spec in (*_dlq_specs(), *_failed_trace_specs()):
        job_targets[cast("uuid.UUID", job_spec["job_id"])] = _lifecycle(
            now,
            cast("timedelta", job_spec["created_offset"]),
            cast("int", job_spec["run_seconds"]),
        )
    for dag_spec in _dag_specs():
        # The two children drain to completed after first boot. Reset never
        # restores their waiting status (ADR 0029), so re-anchoring their
        # NULL lifecycle from the seed spec would make completed rows incoherent.
        if dag_spec["run_seconds"] is None:
            continue
        job_targets[stable(cast("str", dag_spec["name"]))] = _lifecycle(
            now,
            cast("timedelta", dag_spec["created_offset"]),
            cast("int | None", dag_spec["run_seconds"]),
        )

    for job_id, (created_target, started_target, completed_target) in (
        job_targets.items()
    ):
        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            continue
        updated_target = completed_target or created_target
        if (
            _drifted(job.created_at, created_target)
            or _drifted(job.updated_at, updated_target)
            or _drifted(job.started_at, started_target)
            or _drifted(job.completed_at, completed_target)
        ):
            job.created_at = created_target
            job.updated_at = updated_target
            job.started_at = started_target
            job.completed_at = completed_target
            shifted += 1

    return shifted


async def _seed_deploys(session: AsyncSession) -> None:
    """Insert the deploy history, and repair any row a previous seed tenant-stamped.

    Check-then-insert would skip pre-WO-R2-69 rows forever. Stable ids only.
    """
    for spec in _deploy_rows():
        existing = (
            await session.execute(
                select(DeployMarker).where(DeployMarker.id == spec["id"])
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.tenant_id is not None:
                existing.tenant_id = None
            continue
        session.add(DeployMarker(**spec))


async def _seed_alerts(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    for spec in _alert_rows(tenant_id):
        existing = (
            await session.execute(
                select(Alert).where(Alert.id == spec["id"])
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(Alert(**spec))


async def _seed_dlq(session: AsyncSession, tenant: Tenant, user: User) -> None:
    now = datetime.now(UTC)
    for spec in _dlq_specs():
        job_id = spec["job_id"]
        existing = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        created_at, started_at, completed_at = _lifecycle(
            now,
            cast("timedelta", spec["created_offset"]),
            cast("int", spec["run_seconds"]),
        )
        session.add(
            Job(
                id=job_id,
                tenant_id=tenant.id,
                user_id=user.id,
                type=spec["type"],
                status=JobStatus.DEAD_LETTER.value,
                payload={"eval_fixture": True},
                retry_count=spec["retry_count"],
                error_message=spec["error_message"],
                remediation_hint=spec.get("remediation_hint"),
                trace_id=str(stable(f"dlq-trace-{spec['job_id']}")),
                created_at=created_at,
                updated_at=completed_at or created_at,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    await session.flush()

    for spec in _dlq_specs():
        triage_id = spec["triage_id"]
        existing_t = (
            await session.execute(
                select(JobTriage).where(JobTriage.id == triage_id)
            )
        ).scalar_one_or_none()
        if existing_t is not None:
            continue
        # Cast to a concrete mapping so mypy can index it.
        t = cast("dict[str, Any]", spec["triage"])
        session.add(
            JobTriage(
                id=triage_id,
                tenant_id=tenant.id,
                job_id=spec["job_id"],
                root_cause_category=t["root_cause_category"],
                summary=t["summary"],
                suggested_fix=t["suggested_fix"],
                is_retryable=t["is_retryable"],
                confidence=t["confidence"],
                model_used="seed-fixture",
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        )


async def _seed_failed_traces(
    session: AsyncSession, tenant: Tenant, user: User
) -> None:
    now = datetime.now(UTC)
    for spec in _failed_trace_specs():
        job_id = spec["job_id"]
        existing = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        created_at, started_at, completed_at = _lifecycle(
            now,
            cast("timedelta", spec["created_offset"]),
            cast("int", spec["run_seconds"]),
        )
        session.add(
            Job(
                id=job_id,
                tenant_id=tenant.id,
                user_id=user.id,
                type=spec["type"],
                status=JobStatus.FAILED.value,
                payload={"eval_fixture": True},
                retry_count=2,
                error_message=spec["error_message"],
                trace_id=spec["trace_id"],
                created_at=created_at,
                updated_at=completed_at or created_at,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
        # One audit row sharing the trace_id via request_id, so `get_trace` returns a graph.
        audit_id = stable(f"failed-trace-audit-{spec['job_id']}")
        existing_a = (
            await session.execute(
                select(AuditLog).where(AuditLog.id == audit_id)
            )
        ).scalar_one_or_none()
        if existing_a is None:
            session.add(
                AuditLog(
                    id=audit_id,
                    tenant_id=tenant.id,
                    user_id=user.id,
                    principal_type=PRINCIPAL_TYPE_USER,
                    principal_id=user.id,
                    job_id=spec["job_id"],
                    action="job.failed",
                    resource_type="job",
                    resource_id=str(spec["job_id"]),
                    request_id=spec["trace_id"],
                    extra_data={"error_message": spec["error_message"]},
                )
            )


async def _seed_dag(
    session: AsyncSession, tenant: Tenant, user: User
) -> None:
    """Three-node DAG: parent (completed) → seed (waiting) → child
    (waiting). `get_dag_state(seed_id)` returns both edges + all three
    node statuses."""
    parent_id = stable("dag-parent-job")
    seed_id = stable("dag-seed-job")
    child_id = stable("dag-child-job")

    # Jobs
    now = datetime.now(UTC)
    for spec in _dag_specs():
        job_id = stable(cast("str", spec["name"]))
        existing = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        created_at, started_at, completed_at = _lifecycle(
            now,
            cast("timedelta", spec["created_offset"]),
            cast("int | None", spec["run_seconds"]),
        )
        session.add(
            Job(
                id=job_id,
                tenant_id=tenant.id,
                user_id=user.id,
                type=JobType.BULK_API_SYNC.value,
                status=spec["status"],
                payload={"eval_fixture": True, "role": job_id.hex[:6]},
                retry_count=0,
                error_message=None,
                trace_id=str(stable(f"dag-trace-{job_id}")),
                created_at=created_at,
                updated_at=completed_at or created_at,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
    await session.flush()

    # Edges: seed depends on parent, child depends on seed.
    for child, parent in ((seed_id, parent_id), (child_id, seed_id)):
        existing_dep = (
            await session.execute(
                select(JobDependency).where(
                    JobDependency.job_id == child,
                    JobDependency.depends_on_job_id == parent,
                )
            )
        ).scalar_one_or_none()
        if existing_dep is not None:
            continue
        session.add(JobDependency(job_id=child, depends_on_job_id=parent))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _print_pins() -> None:
    print()
    print("=" * 68)
    print("EVAL FIXTURE PINS — copy into scenarios that pin specific IDs")
    print("=" * 68)
    print()
    print(f"  seed_user_id            {stable('seed-user')}")
    print()
    print("  # Consumer groups (Redis):")
    for g, lag in _CONSUMER_LAGS.items():
        print(f"    {g:<24} lag ≈ {lag}")
    print()
    print("  # Deploy markers (deploy_markers table):")
    for spec in _deploy_rows():
        note = f"    <- {spec['notes']}" if spec['notes'] else ""
        print(f"    {spec['version']:<10} {spec['environment']:<8}{note}")
    print()
    print("  # Alerts (id → status):")
    for spec in _alert_rows(uuid.uuid4()):
        state = "resolved" if spec["resolved_at"] else "active"
        print(f"    {str(spec['id']):<40} {state:<10} {spec['source']}")
    print()
    print("  # DLQ jobs (list_dlq_messages):")
    for spec in _dlq_specs():
        print(f"    {str(spec['job_id']):<40} {spec['type']}")
    print()
    print("  # Failed jobs with traces (search_traces / get_trace):")
    for spec in _failed_trace_specs():
        print(f"    trace_id: {spec['trace_id']}")
    print()
    print("  # DAG (get_dag_state):")
    print(f"    dag_seed_job_id: {stable('dag-seed-job')}")
    print(f"    dag_parent_id:   {stable('dag-parent-job')}")
    print(f"    dag_child_id:    {stable('dag-child-job')}")
    print()


async def seed(
    database_url: str = _DB_URL,
    redis_url: str = _REDIS_URL,
    tenant_slug: str = _TENANT_SLUG,
    reset: bool = False,
    allow_target_mismatch: bool = False,
) -> dict[str, int]:
    """Programmatic entry point, and the lifespan's `SEED_EVAL_FIXTURES=true` path. No stdout.

    Gated on the target like every writing script here (WO-R2-19): refuses on
    `ENVIRONMENT=production`, and on a `database_url`/`redis_url` that is not the one
    `settings` names unless `allow_target_mismatch=True`.

    `reset=True` re-baselines the mutable state a live run drifts (FIX_PLAN #7): DLQ job
    status/retry_count/hint/error_message, every time-anchored column
    (`_rebaseline_timestamps`, BUILD_PLAN 2.5), the consumer-lag keys, and
    `cache:jobs:worker-dispatcher:hot_set` (FIX_PLAN #19). Returns
    `{'dlq_reset': N, 'timestamps_rebaselined': N}`."""
    eval_safety.assert_safe_target(
        script="seed_eval_fixtures.py",
        database_url=database_url,
        redis_url=redis_url,
        allow_target_mismatch=allow_target_mismatch,
    )

    engine = create_async_engine(database_url, echo=False)
    # Platform (cross-tenant) scope: this script sets no `app.tenant_id`, which ADR 0026
    # refuses, and runs as the non-owner `incident_app` role with no BYPASSRLS.
    factory = platform_session_factory(engine)
    redis = aioredis.from_url(redis_url, decode_responses=True)
    dlq_reset = 0
    timestamps_rebaselined = 0

    try:
        async with factory() as session:
            async with session.begin():
                tenant = await _ensure_tenant(session, tenant_slug)
                user = await _ensure_seed_user(session, tenant)
                await _seed_deploys(session)
                await _seed_alerts(session, tenant.id)
                await _seed_dlq(session, tenant, user)
                await _seed_failed_traces(session, tenant, user)
                await _seed_dag(session, tenant, user)
                if reset:
                    dlq_reset = await _reset_dlq_state(session)
                    # After the DLQ restore stamps updated_at=now — the re-anchor wins.
                    timestamps_rebaselined = await _rebaseline_timestamps(
                        session
                    )

        await _seed_consumer_lag(redis)
        await _seed_hot_set(redis)
    finally:
        await redis.aclose()
        await engine.dispose()

    return {
        "dlq_reset": dlq_reset,
        "timestamps_rebaselined": timestamps_rebaselined,
    }


def collect_pins() -> dict[str, object]:
    """Structured pin manifest — the JSON shape written to disk by
    `write_pins_json`. Scenarios that pin specific IDs load this
    instead of scraping stdout."""
    return {
        "seed_user_id": str(stable("seed-user")),
        "consumer_groups": {
            group: {"expected_lag": lag} for group, lag in _CONSUMER_LAGS.items()
        },
        "deploys": [
            {
                "version": spec["version"],
                "environment": spec["environment"],
                "notes": spec["notes"],
            }
            for spec in _deploy_rows()
        ],
        "alerts": [
            {
                "id": str(spec["id"]),
                "source": spec["source"],
                "severity": spec["severity"],
                "state": "resolved" if spec["resolved_at"] else "active",
            }
            for spec in _alert_rows(uuid.uuid4())
        ],
        "dlq_jobs": [
            {
                "id": str(spec["job_id"]),
                "type": spec["type"],
                "remediation_hint": spec.get("remediation_hint"),
            }
            for spec in _dlq_specs()
        ],
        "failed_traces": [
            spec["trace_id"] for spec in _failed_trace_specs()
        ],
        "dag": {
            "seed_job_id": str(stable("dag-seed-job")),
            "parent_job_id": str(stable("dag-parent-job")),
            "child_job_id": str(stable("dag-child-job")),
        },
    }


_PINS_BASENAME = "eval-fixtures-pins.json"


def default_pins_path() -> str:
    """Where the pin manifest lands when the caller doesn't say.

    `EVAL_PINS_PATH` if set, else `<tempfile.gettempdir()>/eval-fixtures-pins.json`. The old
    `/app/eval-fixtures-pins.json` default was unwritable in every released image (COPYed as
    root, run as `appuser`), and the EACCES was logged as a *seed* failure.
    """
    import tempfile

    return os.getenv("EVAL_PINS_PATH") or os.path.join(
        tempfile.gettempdir(), _PINS_BASENAME
    )


def write_pins_json(path: str | None = None) -> str:
    """Write the pin manifest to `path` (default `default_pins_path()`); returns the path.

    Raises `OSError` if unwritable; callers read that as "manifest missing", not a seed
    failure."""
    import json
    import pathlib

    p = pathlib.Path(path or default_pins_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(collect_pins(), indent=2, sort_keys=True))
    return str(p)


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Seed the platform with realistic eval fixtures. "
            "Idempotent by default; --reset also restores mutable state "
            "that live scenarios drift (FIX_PLAN #7, #19)."
        )
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Re-baseline mutable fixture state: DLQ job status / "
            "retry_count / remediation_hint; fixture timestamps "
            "(created_at / fired_at / deployed_at re-anchored to their "
            "now-relative seed offsets); consumer-lag Redis keys; the "
            "cache:jobs:worker-dispatcher:hot_set key. Safe to run "
            "against a fresh compose stack (no-op) or between scenarios."
        ),
    )
    parser.add_argument(
        "--i-know-what-im-doing",
        dest="allow_target_mismatch",
        action="store_true",
        help=(
            "Proceed even though DATABASE_URL/REDIS_URL are not the "
            "configured ones. Does not override the production check."
        ),
    )
    args = parser.parse_args()

    eval_safety.refuse_unsafe_target(
        script="seed_eval_fixtures.py",
        database_url=_DB_URL,
        redis_url=_REDIS_URL,
        allow_target_mismatch=args.allow_target_mismatch,
    )
    print(eval_safety.describe_target(_DB_URL, _REDIS_URL))

    try:
        summary = await seed(
            reset=args.reset, allow_target_mismatch=args.allow_target_mismatch
        )
    except SeedError as exc:
        # CLI contract unchanged — stderr, exit 1. Only the exception type moved,
        # so the API's boot guard can catch it (WO-R2-69).
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    try:
        pins_path = write_pins_json()
    except OSError as exc:
        pins_path = f"(could not write pins file: {exc})"
    _print_pins()
    print(f"pins manifest: {pins_path}")
    if args.reset:
        print(
            f"reset summary: dlq_reset={summary['dlq_reset']} "
            f"timestamps_rebaselined={summary['timestamps_rebaselined']}"
        )
    print("Done. All fixtures are idempotent — re-runs are safe.")


if __name__ == "__main__":
    asyncio.run(main())
