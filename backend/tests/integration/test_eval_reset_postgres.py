"""Postgres coverage for the destructive half of the eval-reset protocol.

`scripts/reset_eval_state.py` only runs against Postgres, and SQLite cannot cover these:
`_sweep_nonfixture_dlq` (D-15, the #90 site), `_purge_idempotency_records` (D-15), JSONB
containment in `_delete_seeded_dlq_fixtures` and `slo.py` (S-02 / D-07), the real `uuid`
binding in `_resolve_organic_alerts` (WO-R2-131), and `ON DELETE SET NULL` (D-10).
"""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]

# `scripts/` isn't a package on disk; make it importable, which is also what
# `from scripts import seed_eval_fixtures` inside the reset resolves against.
for _path in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import reset_eval_state  # noqa: E402  # type: ignore[import-not-found]
import seed_eval_fixtures  # noqa: E402  # type: ignore[import-not-found]

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False

pytestmark = pytest.mark.skipif(
    not _HAS_TC or not os.environ.get("RUN_EVAL_RESET_TEST"),
    reason=(
        "set RUN_EVAL_RESET_TEST=1 and install Docker + testcontainers[postgres] "
        "to run"
    ),
)

COMMANDER_SA_NAME = "incident-commander"

# Every table these tests write into, in FK-safe delete order: the reset is
# environment-wide, so each test needs the whole database to itself.
_TABLES_IN_DELETE_ORDER = (
    "audit_logs",
    "job_triages",
    "idempotency_records",
    "jobs",
    "alerts",
    "deploy_markers",
    "service_accounts",
    "users",
    "tenants",
)


def _alembic(database_url: str, *args: str) -> None:
    """Alembic against the container; ALEMBIC_DATABASE_URL is popped because env.py
    prefers it, so an inherited value would migrate the wrong database."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env.pop("ALEMBIC_DATABASE_URL", None)
    subprocess.check_call(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), *args],
        env=env,
        cwd=REPO_ROOT,
    )


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="module")
def eval_db_url(pg: Any) -> str:
    url: str = pg.get_connection_url()
    _alembic(url, "upgrade", "head")
    return url


@pytest_asyncio.fixture
async def session_factory(eval_db_url: str) -> Any:
    """A fresh engine per test: asyncpg is bound to the loop that opened it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(eval_db_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await _truncate(session)
        yield factory
    finally:
        await engine.dispose()


async def _truncate(session: Any) -> None:
    from sqlalchemy import text

    for table in _TABLES_IN_DELETE_ORDER:
        await session.execute(text(f"DELETE FROM {table}"))


async def _make_tenant_and_user(session: Any, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant = Tenant(id=uuid.uuid4(), slug=slug, name=slug, is_active=True)
    session.add(tenant)
    await session.flush()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=f"{slug}@example.test",
        hashed_password="x",
        role="user",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return tenant.id, user.id


def _job(tenant_id: uuid.UUID, user_id: uuid.UUID, **overrides: Any) -> Any:
    from app.models.enums import JobStatus, JobType
    from app.models.job import Job

    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "type": JobType.BULK_API_SYNC.value,
        "status": JobStatus.DEAD_LETTER.value,
        "payload": {},
        "retry_count": 3,
        "max_attempts": 3,
        "priority": 5,
    }
    fields.update(overrides)
    return Job(**fields)


async def _statuses(session: Any, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    from app.models.job import Job
    from sqlalchemy import select

    session.expire_all()
    rows = (
        await session.execute(select(Job.id, Job.status).where(Job.id.in_(ids)))
    ).all()
    return {row.id: row.status for row in rows}


# _sweep_nonfixture_dlq — the #90 regression class (D-15)


async def _seed_sweep_population(
    session_factory: Any,
) -> tuple[list[uuid.UUID], list[uuid.UUID], uuid.UUID]:
    """4 `_dlq_specs()` fixtures, 2 stray dead_letter rows, 1 running."""
    from app.models.enums import JobStatus

    async with session_factory() as session:
        async with session.begin():
            tenant_id, user_id = await _make_tenant_and_user(session, "sweep")
            fixture_ids = [
                uuid.UUID(str(spec["job_id"]))
                for spec in seed_eval_fixtures._dlq_specs()
            ]
            fixtures = [
                _job(tenant_id, user_id, id=job_id, payload={"eval_fixture": True})
                for job_id in fixture_ids
            ]
            strays = [
                _job(tenant_id, user_id, payload={"real": True}) for _ in range(2)
            ]
            running = _job(
                tenant_id, user_id, status=JobStatus.RUNNING.value, payload={}
            )
            session.add_all([*fixtures, *strays, running])
    return fixture_ids, [job.id for job in strays], running.id


async def test_sweep_spares_fixtures_and_cancels_only_stray_dead_letters(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default mode: only stray `dead_letter` rows flip to `cancelled`, the fixture
    baseline stays, and `running` is not a target. The assertion #90 needed."""
    from app.models.enums import JobStatus

    monkeypatch.delenv("EVAL_EMPTY_DLQ_BASELINE", raising=False)
    fixture_ids, stray_ids, running_id = await _seed_sweep_population(session_factory)

    swept = await reset_eval_state._sweep_nonfixture_dlq(session_factory)

    assert swept == 2
    async with session_factory() as session:
        statuses = await _statuses(
            session, [*fixture_ids, *stray_ids, running_id]
        )
    assert [statuses[i] for i in stray_ids] == [JobStatus.CANCELLED.value] * 2
    assert [statuses[i] for i in fixture_ids] == [
        JobStatus.DEAD_LETTER.value
    ] * len(fixture_ids)
    assert statuses[running_id] == JobStatus.RUNNING.value


async def test_sweep_in_empty_baseline_mode_cancels_fixtures_too(
    session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`EVAL_EMPTY_DLQ_BASELINE=1` drops the fixture exclusion (ADR 0012 rule 2),
    still only `dead_letter`."""
    from app.models.enums import JobStatus

    monkeypatch.setenv("EVAL_EMPTY_DLQ_BASELINE", "1")
    fixture_ids, stray_ids, running_id = await _seed_sweep_population(session_factory)

    swept = await reset_eval_state._sweep_nonfixture_dlq(session_factory)

    assert swept == len(fixture_ids) + len(stray_ids)
    async with session_factory() as session:
        statuses = await _statuses(
            session, [*fixture_ids, *stray_ids, running_id]
        )
    assert set(statuses[i] for i in [*fixture_ids, *stray_ids]) == {
        JobStatus.CANCELLED.value
    }
    assert statuses[running_id] == JobStatus.RUNNING.value


# _purge_idempotency_records — principal scoping (D-15)


async def test_purge_idempotency_records_is_scoped_to_the_commander_principal(
    session_factory: Any,
) -> None:
    """Opt-in and narrow: only the incident-commander account's records go,
    or it is a cross-principal cache wipe."""
    from app.models.idempotency import IdempotencyRecord
    from app.models.service_account import ServiceAccount
    from sqlalchemy import select

    async with session_factory() as session:
        async with session.begin():
            tenant_id, _ = await _make_tenant_and_user(session, "idem")
            commander = ServiceAccount(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name=COMMANDER_SA_NAME,
                scopes=["telemetry:read"],
            )
            other = ServiceAccount(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                name="some-other-agent",
                scopes=["telemetry:read"],
            )
            session.add_all([commander, other])
            await session.flush()
            session.add_all(
                [
                    IdempotencyRecord(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        principal_id=sa.id,
                        tool_name="replay_dlq_by_ids",
                        idempotency_key=f"{sa.name}-{n}",
                        arguments_hash="deadbeef",
                        response_json={"ok": True},
                    )
                    for sa in (commander, other)
                    for n in range(2)
                ]
            )
            other_id = other.id

    purged = await reset_eval_state._purge_idempotency_records(session_factory)

    assert purged == 2
    async with session_factory() as session:
        remaining = (
            await session.execute(select(IdempotencyRecord.principal_id))
        ).scalars().all()
    assert set(remaining) == {other_id}
    assert len(remaining) == 2


# _delete_seeded_dlq_fixtures — the JSONB predicate (S-02 / D-07)


async def test_delete_seeded_dlq_fixtures_matches_only_the_structured_marker(
    session_factory: Any,
) -> None:
    """D-07, on the payload shape that ships: top-level JSONB containment of
    `{"seeded_fixture": true}`. Not a text LIKE (it matched all the near misses), not
    a `::boolean` cast (`"banana"` would abort the reset), not equality (hooks add keys).
    """
    from app.models.job import Job
    from sqlalchemy import select

    async with session_factory() as session:
        async with session.begin():
            tenant_id, user_id = await _make_tenant_and_user(session, "marker")
            declared = _job(tenant_id, user_id, payload={"seeded_fixture": True})
            declared_with_extra_keys = _job(
                tenant_id,
                user_id,
                payload={
                    "seeded_fixture": True,
                    "chaos_fixture": "bad_data_job",
                    "fixture_name": "unfenced-csv",
                },
            )
            survivors = [
                _job(tenant_id, user_id, payload={"tag": "seeded_fixture"}),
                _job(
                    tenant_id, user_id, payload={"nested": {"seeded_fixture": True}}
                ),
                _job(tenant_id, user_id, payload={"seeded_fixture": False}),
                _job(tenant_id, user_id, payload={"seeded_fixture": "banana"}),
                _job(tenant_id, user_id, payload={"real": True}),
            ]
            session.add_all(
                [declared, declared_with_extra_keys, *survivors]
            )
            survivor_ids = {job.id for job in survivors}

    deleted = await reset_eval_state._delete_seeded_dlq_fixtures(session_factory)

    assert deleted == 2
    async with session_factory() as session:
        remaining = set(
            (await session.execute(select(Job.id))).scalars().all()
        )
    assert remaining == survivor_ids


# Audit ground truth — D-10 / ADR 0012 amendment


async def test_delete_chaos_owner_users_nulls_audit_fks_but_keeps_resource_id(
    session_factory: Any,
) -> None:
    """The ADR 0012 amendment's contract: `ON DELETE SET NULL` nulls `job_id` and
    `user_id`, so a migration flipping either to CASCADE would delete the audit row.
    `resource_id` is a plain string and the only join key that survives a reset.
    """
    from app.models.audit import AuditLog
    from app.models.enums import JobStatus
    from app.models.user import User
    from sqlalchemy import select

    extra_data = {"previous_status": "dead_letter", "previous_retry_count": 3}

    async with session_factory() as session:
        async with session.begin():
            tenant_id, _ = await _make_tenant_and_user(session, "chaos-audit")
            chaos_user = User(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                email=f"chaos-owner+{tenant_id}@chaos.local",
                hashed_password="!chaos-owner-no-login",
                role="user",
                is_active=False,
            )
            session.add(chaos_user)
            await session.flush()
            chaos_job = _job(
                tenant_id,
                chaos_user.id,
                status=JobStatus.DEAD_LETTER.value,
                # Undeclared: provenance only, no `seeded_fixture` marker, so the
                # DELETE sweep leaves it. Shaped like a pre-v0.6.3 `poison_message`
                # row, which no current hook writes (WO-R2-158, WO-R2-166).
                payload={"chaos_fixture": "poison_message"},
            )
            session.add(chaos_job)
            await session.flush()
            audit = AuditLog(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=chaos_user.id,
                job_id=chaos_job.id,
                action="job.replayed",
                resource_type="job",
                resource_id=str(chaos_job.id),
                extra_data=extra_data,
            )
            session.add(audit)
            audit_id = audit.id
            deleted_job_id = chaos_job.id

    assert await reset_eval_state._delete_chaos_owner_users(session_factory) == 1

    async with session_factory() as session:
        surviving = (
            await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        ).scalar_one_or_none()

    assert surviving is not None, (
        "the reset must never delete an audit row — audit is ground truth "
        "(commander invariant 6)"
    )
    assert surviving.job_id is None, "ON DELETE SET NULL must have nulled job_id"
    assert surviving.user_id is None, "ON DELETE SET NULL must have nulled user_id"
    assert surviving.resource_id == str(deleted_job_id), (
        "resource_id is the durable join key and must survive the delete"
    )
    assert surviving.action == "job.replayed"
    assert surviving.extra_data == extra_data


# _rebaseline_timestamps — BUILD_PLAN 2.5, on real timestamptz columns


async def test_rebaseline_timestamps_on_postgres(session_factory: Any) -> None:
    """`_drifted`'s aware/naive normalisation only meets real timestamptz here.
    Also pins the no-op idempotency of a second run."""
    from datetime import UTC, datetime, timedelta

    from app.models.deploy_marker import DeployMarker
    from sqlalchemy import select

    now = datetime.now(UTC)
    stale = timedelta(days=2)
    specs = seed_eval_fixtures._deploy_rows()
    async with session_factory() as session:
        async with session.begin():
            for spec in specs:
                session.add(
                    DeployMarker(
                        **{**spec, "deployed_at": spec["deployed_at"] - stale}
                    )
                )

    async with session_factory() as session:
        async with session.begin():
            shifted = await seed_eval_fixtures._rebaseline_timestamps(session)
    assert shifted == len(specs)

    async with session_factory() as session:
        markers = (await session.execute(select(DeployMarker))).scalars().all()
    by_version_env = {(m.version, m.environment): m for m in markers}
    hotfix = by_version_env[("v0.4.2", "prod")]
    latest = by_version_env[("v0.4.3", "prod")]
    tol = timedelta(minutes=2)
    assert abs(hotfix.deployed_at - (now - timedelta(hours=6))) < tol
    # Shift, don't flatten: the 4-hour hotfix→latest spacing survives.
    assert abs((latest.deployed_at - hotfix.deployed_at) - timedelta(hours=4)) < tol

    async with session_factory() as session:
        async with session.begin():
            assert await seed_eval_fixtures._rebaseline_timestamps(session) == 0


# _rebuild_read_model — the CQRS projection on the dialect that ships (WO-R2-56).
# Same window function as the unit tier, but `jobs.tenant_id` is a real UUID here.


class _RedisForRebuild:
    """Enough Redis for read_model's write path; nothing here talks to a server."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.strings[key] = str(value)

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def delete(self, *keys: str) -> int:
        gone = 0
        for key in keys:
            gone += int(self.zsets.pop(key, None) is not None)
            gone += int(self.strings.pop(key, None) is not None)
        return gone

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 10
    ) -> tuple[int, list[str]]:
        import fnmatch

        return 0, [
            k for k in (*self.zsets, *self.strings) if fnmatch.fnmatch(k, match)
        ]


async def test_rebuild_read_model_projects_postgres_rows(
    session_factory: Any,
) -> None:
    from app.models.enums import JobStatus
    from app.workers.read_model import rebuild_read_model

    async with session_factory() as session:
        async with session.begin():
            tenant_id, user_id = await _make_tenant_and_user(session, "readmodel")
            completed = [
                _job(tenant_id, user_id, status=JobStatus.COMPLETED.value)
                for _ in range(3)
            ]
            dead = _job(tenant_id, user_id, status=JobStatus.DEAD_LETTER.value)
            # Not a projected status — must not appear anywhere.
            pending = _job(tenant_id, user_id, status=JobStatus.PENDING.value)
            session.add_all([*completed, dead, pending])

    redis = _RedisForRebuild()
    async with session_factory() as session:
        summary = await rebuild_read_model(session, redis, tenant_id=tenant_id)  # type: ignore[arg-type]

    completed_key = f"jobs:tenant:{tenant_id}:status:completed"
    assert set(redis.zsets[completed_key]) == {str(job.id) for job in completed}
    assert set(redis.zsets[f"jobs:tenant:{tenant_id}:status:dead_letter"]) == {
        str(dead.id)
    }
    assert f"jobs:tenant:{tenant_id}:status:pending" not in redis.zsets
    assert set(redis.zsets[f"jobs:user:{user_id}:status:completed"]) == {
        str(job.id) for job in completed
    }
    # 4 projected rows × (tenant key + user key).
    assert summary["members"] == 8
    assert redis.ttls[completed_key] > 0


# _resolve_organic_alerts — WO-R2-131 on the server that actually runs it


async def _seed_alert_population(session_factory: Any) -> tuple[Any, uuid.UUID]:
    """Five fixture alerts plus one organic `slo:*`."""
    from app.models.alert import Alert

    async with session_factory() as session:
        async with session.begin():
            tenant_id, _ = await _make_tenant_and_user(session, "alerts")
            session.add_all(
                Alert(**spec)
                for spec in seed_eval_fixtures._alert_rows(tenant_id)
            )
            organic = Alert(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                severity="critical",
                source="slo:job_completion_rate",
                title="SLO fast burn: Job completion rate",
                description="burning at 80.0x the sustainable rate",
                fired_at=datetime.now(UTC) - timedelta(minutes=3),
                resolved_at=None,
                dedup_key="slo:job_completion_rate:fast_burn:487000",
            )
            session.add(organic)
    return tenant_id, organic.id


async def test_organic_alerts_are_swept_and_the_seeded_baseline_survives(
    session_factory: Any,
) -> None:
    """The WO-R2-131 sweep's binding: five `uuid.UUID` spares against a real `uuid`
    column, which is why the statement is Core `update()` and not `text()`."""
    from app.models.alert import Alert
    from sqlalchemy import select

    tenant_id, organic_id = await _seed_alert_population(session_factory)

    resolved = await reset_eval_state._resolve_organic_alerts(session_factory)

    assert resolved == 1, "only the organic alert was outside the baseline"
    async with session_factory() as session:
        rows = {
            row.id: row
            for row in (
                await session.execute(
                    select(Alert).where(Alert.tenant_id == tenant_id)
                )
            ).scalars()
        }
    assert rows[organic_id].resolved_at is not None
    active = [row for row in rows.values() if row.resolved_at is None]
    assert len(active) == 3, "the world audit's `active alerts 3` baseline"
    assert {row.source for row in active} == {"kafka", "dlq", "api"}

    assert (
        await reset_eval_state._resolve_organic_alerts(session_factory) == 0
    ), "idempotent: a second reset over post-reset state changes nothing"


# SLO lab-fixture exclusion — the JSONB half of WO-R2-132. `slo.py` branches on the
# dialect, so the containment it ships is invisible to the SQLite harness.


async def test_the_evaluator_ignores_lab_fixtures_on_postgres(
    session_factory: Any,
) -> None:
    """A fresh eval world must not burn the budget: 4 of 5 terminal jobs
    dead-lettered is an 80x burn that paged on boot (WO-R2-132)."""
    from app.models.enums import JobStatus
    from app.services.slo import compute_all, is_fast_burning

    async with session_factory() as session:
        async with session.begin():
            tenant_id, user_id = await _make_tenant_and_user(session, "slo")
            session.add_all(
                [
                    *(
                        _job(tenant_id, user_id, payload={"eval_fixture": True})
                        for _ in range(4)
                    ),
                    _job(
                        tenant_id,
                        user_id,
                        status=JobStatus.COMPLETED.value,
                        payload={"eval_fixture": True},
                    ),
                ]
            )

    async with session_factory() as session:
        states = await compute_all(session)

    completion = next(s for s in states if s.definition.id == "job_completion_rate")
    assert completion.total == 0
    assert is_fast_burning(completion) is False


async def test_containment_spares_real_rows_and_survives_a_hostile_value(
    session_factory: Any,
) -> None:
    """Containment matches only a top-level boolean `true`, and does not raise on a
    non-boolean: `(payload ->> 'eval_fixture')::boolean` would abort the evaluation
    pass on `"banana"`. The near misses are S-02, restated where it decides an SLO."""
    from app.models.enums import JobStatus
    from app.services.slo import compute_all

    near_misses: list[dict[str, Any]] = [
        {"tag": "eval_fixture"},
        {"nested": {"eval_fixture": True}},
        {"eval_fixture": False},
        {"eval_fixture": "banana"},
        {"seeded_fixture": 1},
    ]
    async with session_factory() as session:
        async with session.begin():
            tenant_id, user_id = await _make_tenant_and_user(session, "hostile")
            session.add_all(
                [
                    *(
                        _job(tenant_id, user_id, payload=payload)
                        for payload in near_misses
                    ),
                    *(
                        _job(
                            tenant_id,
                            user_id,
                            status=JobStatus.COMPLETED.value,
                            payload=None,
                        )
                        for _ in range(95)
                    ),
                ]
            )

    async with session_factory() as session:
        states = await compute_all(session)

    completion = next(s for s in states if s.definition.id == "job_completion_rate")
    assert completion.total == 100, "a near-miss marker excluded a real row"
    assert completion.failed == 5, "every near-miss row is a counted failure"
    assert completion.healthy is False
