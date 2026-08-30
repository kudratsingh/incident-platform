"""
Seed the platform with realistic data for the incident-commander
agent's live eval suite.

Populates:
  1. Redis lag values for 8 Kafka consumer groups
  2. `deploy_markers` — 6 recent deploys, one annotated for the
     deploy-correlation scenario
  3. `alerts` — mix of active + resolved across kafka / dlq / api / db
     sources (guarantees the alert-storm scenario sees total >= 3
     active)
  4. Dead-letter jobs — 3 realistic DLQ entries with populated
     `job_triages` rows
  5. Failed jobs with traces — jobs sharing trace_ids that
     `search_traces` / `get_trace` can drill into, plus audit rows
     that share the request_id so `get_trace` returns a real graph
  6. A DAG rooted at a stable seed job_id — three-node parent →
     seed → child so `get_dag_state` returns nodes+edges

Runs are **idempotent**: every ID is derived from `uuid5(NAMESPACE, name)`
so a re-run finds every row already present and does nothing. This
means the agent's `make eval-live` can be run repeatedly without
churn.

At the end the script prints every pinned ID under `EVAL FIXTURE
PINS` — copy-paste that into scenario definitions when you want to
hardcode-probe a specific one.

Usage (with the compose stack running):

    make seed-eval-fixtures
    # or:
    docker compose exec app python scripts/seed_eval_fixtures.py

Env vars (optional):

    DATABASE_URL     postgres+asyncpg://...   (defaults to compose value)
    REDIS_URL        redis://redis:6379/0     (defaults to compose value)
    SEED_TENANT_SLUG default: default
    EVAL_PINS_PATH   where `write_pins_json` lands the pin manifest
                     (default: <tempdir>/eval-fixtures-pins.json — see
                     `default_pins_path`)
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
# ...and this script's own directory, so the sibling `eval_safety`
# helper resolves however this module was imported (flat by the tests,
# or as `scripts.seed_eval_fixtures` by reset_eval_state).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_safety  # type: ignore[import-not-found]  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402
from app.core.security import hash_password  # noqa: E402
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
    async_sessionmaker,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# Deterministic ID space
#
# uuid5 with a fixed namespace + human-readable name means every run
# generates the same UUID for the same fixture. Scenarios can pin
# specific IDs by pasting them from the report the script prints.
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
    """A seed precondition the caller can act on.

    Deliberately an `Exception` rather than `SystemExit`: this module is
    imported by the API's boot path, which catches `Exception` to log and
    degrade. See `_ensure_tenant` (WO-R2-69).
    """

# Kept in one place so a typo doesn't drift the tool + the seed apart.
# Mirror of app.mcp.tools.consumer_lag._CONSUMER_LAG_KEY_PREFIX.
_LAG_KEY_PREFIX = "kafka:consumer_lag:"
# Written WITHOUT expiry, like every other fixture in this script
# (R2-17). These used to carry a 24h TTL on the theory that "fresh runs
# re-seed anyway" — but the stack outlives the run. After a day of
# uptime without a re-seed the keys simply vanished, and the six live
# scenarios that assert a non-null lag for these groups started failing
# as agent errors rather than as an unmet precondition: one wasted paid
# run per occurrence. Durability is the reset's job, not a TTL's.

# The `remediate_stale_cache_success` eval scenario expects this Redis
# key to exist with recognisably-stale contents, then invalidates it via
# `invalidate_cache_key` and observes the delete. Without the seed the
# scenario can't run live — that gap was FIX_PLAN #19. Reset re-populates
# it so consecutive scenarios each start from the stale-cache condition.
_HOT_SET_KEY = "cache:jobs:worker-dispatcher:hot_set"
# Value is a JSON array of stable job IDs; anyone inspecting it sees
# obviously-fake "hot" state rather than mistaking it for real cache.
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
    # worker-dispatcher is already populated by the metrics loop when
    # the worker is running — don't overwrite it here.
}


# How long a seeded job waited to be dispatched, and the lifecycle columns
# that follow from it (WO-R2-69).
#
# `update_status` stamps `started_at` on the PENDING→RUNNING write and
# `completed_at` on every terminal write, so a `completed`/`failed`/
# `dead_letter` row the platform produced *always* has both. Seeded rows had
# neither, which made them incoherent in two directions at once:
#
#   * The dispatch-latency SLO counts a dispatched job with `started_at IS
#     NULL` as a dispatch miss — that is what NULL means there. Seven fixture
#     rows therefore sat permanently in the denominator as failures, and every
#     reset re-anchored them into the rolling 24h window so they never aged
#     out. On a lab stack with little other traffic that is most of the
#     denominator, i.e. a seeded fast-burn alert about nothing.
#   * `list_jobs(sort=DEAD_LETTERED_AT)` orders on
#     `coalesce(completed_at, created_at)` (WO-R2-53), so for exactly the
#     fixture set the DLQ scenarios are graded against, the "when did it die"
#     sort silently degraded to "when was it submitted".
#
# 4 seconds is deliberately well inside the objective's 30-second threshold:
# the fixtures should be ordinary healthy dispatches, so that a scenario that
# *induces* latency is measuring its own effect and not the seed's.
_DISPATCH_LATENCY_SECONDS = 4


def _lifecycle(
    now: datetime, created_offset: timedelta, run_seconds: int | None
) -> tuple[datetime, datetime | None, datetime | None]:
    """(created_at, started_at, completed_at) for one seeded job.

    One derivation, used by both the seed and the re-baseline, so a reset
    cannot produce a row shape the seed would never have written. Jobs that
    were never dispatched (`run_seconds is None` — the WAITING half of the DAG)
    keep NULL for both, which is what never-dispatched means.
    """
    created_at = now - created_offset
    if run_seconds is None:
        return created_at, None, None
    started_at = created_at + timedelta(seconds=_DISPATCH_LATENCY_SECONDS)
    return created_at, started_at, started_at + timedelta(seconds=run_seconds)


def _dag_specs() -> list[dict[str, object]]:
    """The three-node DAG: parent (completed) → seed (waiting) → child.

    Given explicit offsets so the parent's own lifecycle is orderable. The
    re-baseline used to pin all three at exactly `now`, which — once the
    parent had a real `started_at` — would have meant a job that started
    before it was created, and a negative dispatch latency in every tool that
    subtracts the two.
    """
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

    A deploy marker is platform-wide by construction: the column is nullable
    precisely so it can say "this is not a tenant's row", the RLS policy is
    written around `tenant_id IS NULL`, and every other producer of these rows
    passes None. The seeder alone stamped them with a concrete tenant, which
    made the fixture history invisible to the policy's platform-wide branch
    and visible to exactly one tenant — so `get_deploy_history`, which the
    correlation scenarios read, agreed with the contract on an empty database
    and disagreed with it on a seeded one. There is no parameter to get this
    wrong with any more.
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


def _dlq_specs() -> list[dict[str, object]]:
    """Each spec becomes one Job + one JobTriage row. Each maps to one
    of the three `remediation_hint` categories the agent branches on:

      * schema-violation  → replay_safe        (fix consumer, replay)
      * SMTP / stripe     → wait_and_replay    (dep down, retry later)
      * csv bad-data      → human_required     (persistent bug, escalate)

    Job types use the platform's real enum values but error strings
    name the intended service (send_email, process_payment) so the
    agent's LLM reads a realistic string."""
    return [
        {
            "job_id": stable("dlq-job-schema-violation"),
            "run_seconds": 2,
            "triage_id": stable("dlq-triage-schema-violation"),
            "type": JobType.BULK_API_SYNC.value,
            "remediation_hint": RemediationHint.REPLAY_SAFE.value,
            "error_message": (
                "SchemaValidationError: payload missing required field "
                "'user_id' (received keys: ['tenant_id', 'action', 'ts'])"
            ),
            "retry_count": 3,
            "created_offset": timedelta(minutes=8),
            "triage": {
                "root_cause_category": "schema_violation",
                "summary": (
                    "Producer sent a payload missing user_id — schema "
                    "rejected it three times."
                ),
                "suggested_fix": (
                    "Fix the producer to include user_id, then replay the "
                    "DLQ entry. Backwards-compatible producer fix + replay "
                    "is the standard remediation."
                ),
                "is_retryable": True,
                "confidence": 0.91,
            },
        },
        {
            "job_id": stable("dlq-job-send-email"),
            "run_seconds": 12,
            "triage_id": stable("dlq-triage-send-email"),
            "type": JobType.BULK_API_SYNC.value,
            "remediation_hint": RemediationHint.WAIT_AND_REPLAY.value,
            "error_message": (
                "send_email downstream call failed: "
                "ConnectionRefusedError('smtp.mailer.internal:587')"
            ),
            "retry_count": 3,
            "created_offset": timedelta(minutes=40),
            "triage": {
                "root_cause_category": "downstream_unavailable",
                "summary": "SMTP relay unreachable — connection refused at TCP layer.",
                "suggested_fix": (
                    "Check smtp.mailer.internal ECS task health; recent "
                    "billing hotfix (v0.4.2) may have changed VPC egress "
                    "rules — cross-reference `get_deploy_history`. Replay "
                    "once the dependency recovers."
                ),
                "is_retryable": True,
                "confidence": 0.82,
            },
        },
        {
            "job_id": stable("dlq-job-process-payment"),
            "run_seconds": 30,
            "triage_id": stable("dlq-triage-process-payment"),
            "type": JobType.BULK_API_SYNC.value,
            "remediation_hint": RemediationHint.WAIT_AND_REPLAY.value,
            "error_message": (
                "process_payment call timed out after 30s: "
                "TimeoutError('stripe.api')"
            ),
            "retry_count": 3,
            "created_offset": timedelta(minutes=25),
            "triage": {
                "root_cause_category": "third_party_timeout",
                "summary": "Stripe API exceeded 30s deadline three consecutive attempts.",
                "suggested_fix": (
                    "Verify Stripe status page; if green, raise the per-call "
                    "timeout via the retry policy for payment-type jobs and "
                    "replay the DLQ batch."
                ),
                "is_retryable": True,
                "confidence": 0.71,
            },
        },
        {
            "job_id": stable("dlq-job-csv-parse"),
            "run_seconds": 18,
            "triage_id": stable("dlq-triage-csv-parse"),
            "type": JobType.CSV_UPLOAD.value,
            "remediation_hint": RemediationHint.HUMAN_REQUIRED.value,
            "error_message": (
                "ValueError: invalid literal for int() with base 10: "
                "'not-a-number' at row 15,382"
            ),
            "retry_count": 3,
            "created_offset": timedelta(minutes=12),
            "triage": {
                "root_cause_category": "bad_input",
                "summary": "Non-numeric value in a supposedly-integer CSV column.",
                "suggested_fix": (
                    "Not retryable — data quality issue. Notify the uploader; "
                    "add a validation step in the CSV importer that fails "
                    "the whole upload with a clear error rather than half-"
                    "processing."
                ),
                "is_retryable": False,
                "confidence": 0.94,
            },
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

    A normal exception, not `SystemExit` (WO-R2-69). `SystemExit` derives
    from `BaseException`, so the API's boot-time seed guard — an
    `except Exception` around `await seed()` whose entire purpose is to log
    the failure and let the app start anyway — could not catch it. A missing
    `SEED_TENANT_SLUG`, which is a configuration typo on a lab stack, instead
    unwound through the lifespan and killed the process on every boot: a
    crash-loop whose log line said nothing about tenants, for a fixture set
    the platform is designed to run without.

    The CLI still exits non-zero with this message — `main()` catches it and
    reports it — so the operator-facing behaviour is unchanged. What changes
    is that an embedded caller now gets an exception it is allowed to handle.
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
    """Write the synthetic consumer-lag fixtures durably.

    No TTL — see `_LAG_KEY_PREFIX`. `worker-dispatcher` is deliberately
    absent from `_CONSUMER_LAGS`: the metrics loop owns that key with a
    90s TTL, and a durable fixture written over it would pin a stale lag
    the loop could no longer correct."""
    for group, lag in _CONSUMER_LAGS.items():
        await redis.set(f"{_LAG_KEY_PREFIX}{group}", str(lag))


async def _seed_hot_set(redis: aioredis.Redis) -> None:
    """Populate the `remediate_stale_cache_success` scenario's fixture
    (FIX_PLAN #19). Value is stable + recognisably fake so an operator
    inspecting Redis doesn't confuse it with production cache."""
    import json as _json

    # Derived from `_dlq_specs()` (first three, keeping the
    # schema-violation job first) so every member references a job the
    # seed actually creates. Hardcoded stable() names drifted once when
    # specs were renamed, leaving phantom UUIDs in the cache (D-14).
    payload = _json.dumps(
        [str(spec["job_id"]) for spec in _dlq_specs()[:3]]
    )
    await redis.set(_HOT_SET_KEY, payload, ex=_HOT_SET_TTL_SECONDS)


async def _reset_dlq_state(session: AsyncSession) -> int:
    """Re-baseline stable() DLQ jobs to their advertised state.

    Live eval scenarios mutate these rows (status → RUNNING/REPLAYED,
    retry_count reset to 0 on replay, remediation_hint occasionally
    cleared). Without this reset, the second run of a scenario sees a
    row in the wrong state and mis-grades — that was FIX_PLAN #7. Only
    touches the stable() IDs from `_dlq_specs()`; leaves any non-fixture
    jobs alone.

    Returns the number of rows reset (0 = fixtures already baseline)."""
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
        if not needs_reset:
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


# How much wall-clock drift a fixture timestamp may accumulate before a
# reset re-anchors it. Non-zero so back-to-back resets stay no-ops (the
# idempotency reset_eval_state documents), and far below the tightest
# freshness window an eval asserts on: `since_hours=1` minus the largest
# job offset (45 min) still leaves several minutes of headroom, and real
# staleness is measured in hours-to-days.
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
    """Re-anchor every time-anchored fixture column to its spec offset
    from *now*, so a reset leaves the seeded world exactly as fresh as
    it was at first boot (BUILD_PLAN 2.5 — time re-baselining).

    The seeder computes offsets at seed time and check-then-insert never
    updates them, so two days into a stack's life
    `search_traces(since_hours=1)` found nothing and every age-sensitive
    scenario graded against an apparently healthy system. Shift, don't
    flatten: each row keeps its own spec offset, so the stories the
    offsets encode (staging soak before the prod deploy, the v0.4.2
    hotfix hours before the billing failures) keep their relative
    spacing.

    Scope — every seeded fixture field an eval can time-assert on:

      * `jobs` (DLQ + failed-trace specs): the whole lifecycle —
        `created_at` / `updated_at` / `started_at` / `completed_at` —
        derived together by `_lifecycle` from each spec's
        `created_offset` and `run_seconds`. `created_at` is what
        `search_traces(since_hours=...)` filters on and
        `list_dlq_messages` returns; `completed_at` is what the
        `DEAD_LETTERED_AT` sort orders on and what the dispatch-latency
        SLO measures against `started_at`.
      * `jobs` (DAG trio): the same four columns from `_dag_specs`. The
        parent is `completed` and so carries a start and an end; the two
        `waiting` nodes carry neither, because they have not been
        dispatched. This loop used to pin all three at `now`, which is
        what gave the parent a start earlier than its own creation.
      * `alerts`: `fired_at` / `resolved_at` from `_alert_rows` —
        `list_active_alerts` returns `fired_at`.
      * `deploy_markers`: `deployed_at` from `_deploy_rows` —
        `get_deploy_history` returns `deployed_at`.

    Deliberately excluded: `audit_logs` (ground truth — no reset codepath
    may touch it, ADR 0012 amendment) and `job_triages` (no timestamp
    appears in any tool output). Only stable() IDs are addressed, so
    organic rows created by live traffic are structurally unreachable.

    Rows already within `_REBASELINE_TOLERANCE` of target are skipped.
    Returns the number of rows shifted."""
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

    # The whole lifecycle, not just created_at (WO-R2-69). Re-anchoring
    # `created_at` alone left `started_at`/`completed_at` at whatever
    # wall-clock the previous run stamped, which produced rows that started
    # before they were created — the DAG parent, pinned at `now` by this
    # very loop while its start stayed in the past, was guaranteed to — and
    # a negative dispatch latency in every tool that subtracts the two.
    # Deriving all three from the spec makes the reset restore a shape the
    # seed would have written, rather than a shifted half of one.
    job_targets: dict[uuid.UUID, tuple[datetime, datetime | None, datetime | None]] = {}
    for job_spec in (*_dlq_specs(), *_failed_trace_specs()):
        job_targets[cast("uuid.UUID", job_spec["job_id"])] = _lifecycle(
            now,
            cast("timedelta", job_spec["created_offset"]),
            cast("int", job_spec["run_seconds"]),
        )
    for dag_spec in _dag_specs():
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
    """Insert the deploy history, and repair any row a previous seed
    tenant-stamped.

    The repair matters because this seeder is check-then-insert: a stack
    seeded before WO-R2-69 already holds these six ids with a concrete
    `tenant_id`, and re-seeding would skip straight past them forever. The
    rows are addressed by stable id, so this can only touch fixtures.
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
        # `spec["triage"]` is typed as `object` in the container dict;
        # cast to a concrete mapping so mypy on py3.12 can index it.
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
        # One audit row sharing the trace_id (via request_id) so
        # `get_trace(trace_id)` returns a graph, not just the job.
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
    """Programmatic entry point — usable from tests and the app
    lifespan's `SEED_EVAL_FIXTURES=true` path. No stdout output.

    Gated on the target, like every other script that writes to a
    database here (WO-R2-19): refuses when `ENVIRONMENT=production`, and
    refuses when `database_url`/`redis_url` are not the ones `settings`
    names, unless `allow_target_mismatch=True`. This seeder writes a
    user with a known password and overwrites job rows by stable id, so
    "which database" is exactly as load-bearing a question here as it is
    for the reset.

    `reset=True` re-baselines mutable fixture state that scenarios
    drift over the course of a live eval run (FIX_PLAN #7):
      * stable() DLQ jobs get status/retry_count/hint/error_message
        restored to their spec.
      * every time-anchored fixture column is re-anchored to its
        now-relative seed offset (`_rebaseline_timestamps`, BUILD_PLAN
        2.5) so age-sensitive scenarios see a fresh world after reset.
      * Kafka consumer-lag keys re-written (a scenario that consumed
        or drove up lag has its cached value replaced).
      * `cache:jobs:worker-dispatcher:hot_set` re-populated (FIX_PLAN
        #19 — required for `remediate_stale_cache_success`).

    Returns a small summary dict
    {'dlq_reset': N, 'timestamps_rebaselined': N} for the caller to
    log / include in a reset-protocol audit trail."""
    eval_safety.assert_safe_target(
        script="seed_eval_fixtures.py",
        database_url=database_url,
        redis_url=redis_url,
        allow_target_mismatch=allow_target_mismatch,
    )

    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
                    # After the DLQ restore, which stamps updated_at=now
                    # on drifted rows — the re-anchor has the last word.
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

    Resolution order:

      1. `EVAL_PINS_PATH` — full destination path, for operators (or the
         commander's compose) to point at a mount of their choosing.
      2. `<tempdir>/eval-fixtures-pins.json` — `tempfile.gettempdir()`,
         i.e. `/tmp/eval-fixtures-pins.json` in the shipped image, the
         one location writable under any runtime user.

    The previous default, `/app/eval-fixtures-pins.json`, was unwritable
    in every released image: the Dockerfile COPYs `/app` as root and runs
    as the non-root `appuser`, so the boot-time write died with EACCES —
    on a manifest the old log line then reported as a *seed* failure.
    """
    import tempfile

    return os.getenv("EVAL_PINS_PATH") or os.path.join(
        tempfile.gettempdir(), _PINS_BASENAME
    )


def write_pins_json(path: str | None = None) -> str:
    """Write the pin manifest to `path` (default `default_pins_path()`).
    Returns the path written so the caller can log or print it.

    Raises `OSError` when the destination is genuinely unwritable — the
    caller decides how loud to be. Both callers (the api lifespan and
    `main()` below) treat that as "pin manifest missing", explicitly
    distinct from a seed failure: the fixtures are already committed by
    the time this runs."""
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
        # The CLI contract is unchanged — message on stderr, exit 1. Only the
        # exception type changed, so that the API's boot guard can catch it
        # (WO-R2-69); turning it back into an exit code belongs here, at the
        # one call site that is actually a command line.
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
