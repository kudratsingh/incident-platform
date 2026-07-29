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

import redis.asyncio as aioredis  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.alert import Alert  # noqa: E402
from app.models.audit import (  # noqa: E402
    PRINCIPAL_TYPE_USER,
    AuditLog,
)
from app.models.deploy_marker import DeployMarker  # noqa: E402
from app.models.enums import JobStatus, JobType, UserRole  # noqa: E402
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

# Kept in one place so a typo doesn't drift the tool + the seed apart.
# Mirror of app.mcp.tools.consumer_lag._CONSUMER_LAG_KEY_PREFIX.
_LAG_KEY_PREFIX = "kafka:consumer_lag:"
# Long TTL so evals don't decay mid-run. Fresh runs re-seed anyway.
_LAG_TTL_SECONDS = 24 * 3600

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


def _deploy_rows(tenant_id: uuid.UUID | None) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    return [
        {
            "id": stable("deploy-v0.4.0"),
            "tenant_id": tenant_id,
            "version": "v0.4.0",
            "revision": "a1940af",
            "image_tag": "v0.4.0",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=22),
            "notes": None,
        },
        {
            "id": stable("deploy-v0.4.1"),
            "tenant_id": tenant_id,
            "version": "v0.4.1",
            "revision": "b7c3e51",
            "image_tag": "v0.4.1",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=18),
            "notes": None,
        },
        {
            "id": stable("deploy-v0.4.2-billing-hotfix"),
            "tenant_id": tenant_id,
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
            "tenant_id": tenant_id,
            "version": "v0.4.3",
            "revision": "d1e5a83",
            "image_tag": "v0.4.3",
            "environment": "prod",
            "deployed_at": now - timedelta(hours=2),
            "notes": None,
        },
        {
            "id": stable("deploy-v0.4.3-staging"),
            "tenant_id": tenant_id,
            "version": "v0.4.3",
            "revision": "d1e5a83",
            "image_tag": "v0.4.3",
            "environment": "staging",
            "deployed_at": now - timedelta(hours=3),
            "notes": "staging soak before prod",
        },
        {
            "id": stable("deploy-v0.3.9"),
            "tenant_id": tenant_id,
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
    """Each spec becomes one Job + one JobTriage row. Job types map to
    the platform's real enum values but error_message names the real
    intended service (send_email, process_payment) so the agent's LLM
    reads a realistic string."""
    return [
        {
            "job_id": stable("dlq-job-send-email"),
            "triage_id": stable("dlq-triage-send-email"),
            "type": JobType.BULK_API_SYNC.value,
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
                    "rules — cross-reference `get_deploy_history`."
                ),
                "is_retryable": True,
                "confidence": 0.82,
            },
        },
        {
            "job_id": stable("dlq-job-process-payment"),
            "triage_id": stable("dlq-triage-process-payment"),
            "type": JobType.BULK_API_SYNC.value,
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
            "triage_id": stable("dlq-triage-csv-parse"),
            "type": JobType.CSV_UPLOAD.value,
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
            "trace_id": str(stable("failed-trace-a")),
            "type": JobType.REPORT_GEN.value,
            "error_message": "OOM during PDF generation (200MB report)",
            "created_offset": timedelta(minutes=45),
        },
        {
            "job_id": stable("failed-job-trace-b"),
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
    tenant = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if tenant is None:
        raise SystemExit(
            f"error: tenant slug {slug!r} not found. Run `alembic upgrade "
            "head` first."
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
    for group, lag in _CONSUMER_LAGS.items():
        await redis.set(
            f"{_LAG_KEY_PREFIX}{group}", str(lag), ex=_LAG_TTL_SECONDS
        )


async def _seed_deploys(
    session: AsyncSession, tenant_id: uuid.UUID | None
) -> None:
    for spec in _deploy_rows(tenant_id):
        existing = (
            await session.execute(
                select(DeployMarker).where(DeployMarker.id == spec["id"])
            )
        ).scalar_one_or_none()
        if existing is not None:
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
        created_at = now - spec["created_offset"]  # type: ignore[operator]
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
                trace_id=str(stable(f"dlq-trace-{spec['job_id']}")),
                created_at=created_at,
                updated_at=created_at,
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
        created_at = now - spec["created_offset"]  # type: ignore[operator]
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
                updated_at=created_at,
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
    to_add = [
        (parent_id, JobStatus.COMPLETED.value),
        (seed_id, JobStatus.WAITING.value),
        (child_id, JobStatus.WAITING.value),
    ]
    for job_id, status in to_add:
        existing = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            Job(
                id=job_id,
                tenant_id=tenant.id,
                user_id=user.id,
                type=JobType.BULK_API_SYNC.value,
                status=status,
                payload={"eval_fixture": True, "role": job_id.hex[:6]},
                retry_count=0,
                error_message=None,
                trace_id=str(stable(f"dag-trace-{job_id}")),
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
    for spec in _deploy_rows(None):
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
) -> None:
    """Programmatic entry point — usable from tests and the app
    lifespan's `SEED_EVAL_FIXTURES=true` path. No stdout output."""
    engine = create_async_engine(database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = aioredis.from_url(redis_url, decode_responses=True)

    try:
        async with factory() as session:
            async with session.begin():
                tenant = await _ensure_tenant(session, tenant_slug)
                user = await _ensure_seed_user(session, tenant)
                await _seed_deploys(session, tenant.id)
                await _seed_alerts(session, tenant.id)
                await _seed_dlq(session, tenant, user)
                await _seed_failed_traces(session, tenant, user)
                await _seed_dag(session, tenant, user)

        await _seed_consumer_lag(redis)
    finally:
        await redis.aclose()
        await engine.dispose()


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
            for spec in _deploy_rows(None)
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
            {"id": str(spec["job_id"]), "type": spec["type"]}
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


def write_pins_json(path: str = "/app/eval-fixtures-pins.json") -> str:
    """Write the pin manifest to `path` (default a well-known location
    inside the app container). Returns the path so the caller can log
    or print it."""
    import json
    import pathlib

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(collect_pins(), indent=2, sort_keys=True))
    return str(p)


async def main() -> None:
    await seed()
    try:
        pins_path = write_pins_json()
    except OSError as exc:
        pins_path = f"(could not write pins file: {exc})"
    _print_pins()
    print(f"pins manifest: {pins_path}")
    print("Done. All fixtures are idempotent — re-runs are safe.")


if __name__ == "__main__":
    asyncio.run(main())
