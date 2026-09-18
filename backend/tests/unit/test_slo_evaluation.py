"""Scheduled SLO evaluation, real-condition alerts, and cancellations (WO-R2-29).

Two findings that had to land together. `compute_all` had one caller, a read-only admin endpoint, so
nothing evaluated the objectives on a schedule and the alert webhook's only producer was a chaos
tool. And `job_dispatch_latency` admitted CANCELLED rows, which never left PENDING, so each was
counted as a dispatch miss. Real rows on a module-local SQLite engine, so committed rows never leak
into the shared `sqlite_engine`.
"""

import asyncio
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.models.alert import Alert
from app.models.base import Base
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.services import slo as slo_mod
from app.workers import dispatcher as dispatcher_mod
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_USER_ID = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
_WEBHOOK = "http://receiver.invalid/hook"


class _RecordingClient:
    """Stand-in for `httpx.AsyncClient` that records every POST body."""

    posts: list[dict[str, Any]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def post(self, url: str, content: bytes, headers: dict[str, str]) -> Any:
        import json

        type(self).posts.append(
            {"url": url, "payload": json.loads(content), "headers": headers}
        )

        class _Resp:
            status_code = 200

        return _Resp()


@pytest.fixture(autouse=True)
def _reset_recorded_posts() -> None:
    _RecordingClient.posts = []


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[  # type: ignore[return]
    async_sessionmaker[AsyncSession], None
]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    slug="default",
                    name="Default Tenant",
                    is_active=True,
                )
            )
            session.add(
                User(
                    id=_USER_ID,
                    tenant_id=DEFAULT_TENANT_ID,
                    email="slo@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_jobs(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: str,
    count: int,
    dispatch_delay_seconds: float | None = 1.0,
) -> None:
    """Seed `count` jobs created an hour ago.

    `dispatch_delay_seconds=None` leaves `started_at` NULL — which is what a
    cancelled or never-dispatched job looks like.
    """
    created = datetime.now(UTC) - timedelta(hours=1)
    async with factory() as session:
        async with session.begin():
            for _ in range(count):
                session.add(
                    Job(
                        id=uuid.uuid4(),
                        tenant_id=DEFAULT_TENANT_ID,
                        user_id=_USER_ID,
                        type=JobType.CSV_UPLOAD,
                        status=status,
                        payload={"rows": 1},
                        created_at=created,
                        updated_at=created,
                        started_at=(
                            None
                            if dispatch_delay_seconds is None
                            else created
                            + timedelta(seconds=dispatch_delay_seconds)
                        ),
                    )
                )


async def _latency_state(
    factory: async_sessionmaker[AsyncSession],
) -> Any:
    async with factory() as session:
        states = await slo_mod.compute_all(session)
    return next(s for s in states if s.definition.id == "job_dispatch_latency")


async def _alerts(factory: async_sessionmaker[AsyncSession]) -> list[Alert]:
    async with factory() as session:
        return list((await session.execute(select(Alert))).scalars().all())


# Finding 2 — cancellations are not dispatch failures


async def test_a_six_step_saga_rollback_does_not_move_the_objective(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for finding 2: cancelled saga steps and cascaded WAITING descendants never left
    PENDING, so each reached the objective with `started_at IS NULL`. A rollback is a decision, not
    an outage, and must not cost error budget."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=20)
    before = await _latency_state(session_factory)

    await _seed_jobs(
        session_factory,
        status=JobStatus.CANCELLED,
        count=6,
        dispatch_delay_seconds=None,
    )
    after = await _latency_state(session_factory)

    assert after.total == before.total == 20, (
        "cancelled jobs entered the dispatch-latency denominator — a saga "
        "rollback now burns error budget for work nobody tried to dispatch"
    )
    assert after.failed == before.failed == 0
    assert after.current == before.current == 1.0
    assert after.budget_remaining_pct == 100.0


async def test_a_cancellation_cascade_does_not_trip_the_fast_burn_alarm(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Why the two findings had to land together: `cascade_cancel_blocked_children` cancels a whole
    subtree in one write, so with cancellations in the denominator a single cascade could pass 14.4×
    on its own and page at critical severity for correct cleanup."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=2)
    await _seed_jobs(
        session_factory,
        status=JobStatus.CANCELLED,
        count=40,
        dispatch_delay_seconds=None,
    )

    state = await _latency_state(session_factory)

    assert not slo_mod.is_fast_burning(state), (
        f"burn rate {state.burn_rate} — a dependency cascade alone would page"
    )
    assert state.healthy is True


async def test_a_job_that_never_started_is_still_a_dispatch_miss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fix must not over-correct: only CANCELLED leaves the denominator, and `started_at IS
    NULL` stays a dispatch failure everywhere else."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=9)
    await _seed_jobs(
        session_factory,
        status=JobStatus.DEAD_LETTER,
        count=1,
        dispatch_delay_seconds=None,
    )

    state = await _latency_state(session_factory)

    assert state.total == 10
    assert state.failed == 1


async def test_slow_dispatch_is_still_a_failure_and_cancellations_are_not(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The portable fallback, asserted directly. `EXTRACT(EPOCH FROM ...)` evaluates to NULL on
    SQLite, so the SQL path silently reports zero latency failures here and calling the fallback is
    the only way to assert the threshold. Both paths take their denominator from
    `_dispatched_in_window`."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=8)
    await _seed_jobs(
        session_factory,
        status=JobStatus.COMPLETED,
        count=2,
        dispatch_delay_seconds=45.0,  # threshold is 30s
    )
    await _seed_jobs(
        session_factory,
        status=JobStatus.CANCELLED,
        count=6,
        dispatch_delay_seconds=None,
    )

    definition = next(
        d for d in slo_mod.SLOS if d.id == "job_dispatch_latency"
    )
    async with session_factory() as session:
        state = await slo_mod._compute_latency_slo_python(
            session, definition, datetime.now(UTC) - timedelta(hours=24)
        )

    assert state.total == 10, "cancellations reached the fallback denominator"
    assert state.failed == 2


async def test_queued_and_waiting_jobs_stay_out_of_the_denominator(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unchanged behaviour, pinned: a job still queued has no dispatch
    outcome yet, and must not read as a miss just because it is young."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=5)
    await _seed_jobs(
        session_factory,
        status=JobStatus.PENDING,
        count=3,
        dispatch_delay_seconds=None,
    )
    await _seed_jobs(
        session_factory,
        status=JobStatus.WAITING,
        count=3,
        dispatch_delay_seconds=None,
    )

    state = await _latency_state(session_factory)

    assert state.total == 5
    assert state.failed == 0


class _PostgresishSession:
    """The two attributes `_dispatched_in_window` reads, on a fake bind: `_not_a_lab_fixture`
    branches on the dialect name. Nothing executes; the statement is compiled, not run."""

    class _Bind:
        dialect = postgresql.dialect()

    def get_bind(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._Bind()


def _rendered_denominator() -> str:
    from sqlalchemy import select as sa_select

    stmt = sa_select(Job.id).where(
        *slo_mod._dispatched_in_window(
            cast("Any", _PostgresishSession()), datetime.now(UTC)
        )
    )
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_the_sql_denominator_excludes_cancellations_too() -> None:
    """The SQL path is never exercised by this suite — SQLite has no `EXTRACT` — so compile it and
    make the exclusion visible rather than merely intended."""
    sql = _rendered_denominator()

    assert "'cancelled'" not in sql
    assert "'waiting'" not in sql
    assert "'pending'" not in sql
    assert "'completed'" in sql
    assert "'dead_letter'" in sql


def test_the_sql_denominator_excludes_lab_fixtures_by_jsonb_containment() -> None:
    """The Postgres spelling of the WO-R2-132 exclusion, rendered. Containment (`@>`) matches a
    top-level key holding boolean `true` only, where a `::boolean` cast would raise on
    `{"eval_fixture": "banana"}`, and `COALESCE` keeps a NULL payload from nulling the predicate.
    Server behaviour: `backend/tests/integration/test_eval_reset_postgres.py`."""
    sql = _rendered_denominator()

    for marker in slo_mod._LAB_FIXTURE_PAYLOAD_MARKERS:
        assert f'''jobs.payload @> \'{{"{marker}": true}}\'::jsonb''' in sql
    assert sql.count("COALESCE") == len(slo_mod._LAB_FIXTURE_PAYLOAD_MARKERS)
    assert "::boolean" not in sql


# Finding 1 — scheduled evaluation creates real alerts


def _webhook_settings(**overrides: Any) -> Settings:
    return Settings(
        alert_webhook_url=_WEBHOOK,
        alert_webhook_secret="s3cr3t",
        **overrides,
    )


async def _run_evaluation(
    factory: async_sessionmaker[AsyncSession],
) -> list[uuid.UUID]:
    with patch(
        "app.services.alerts.get_settings", return_value=_webhook_settings()
    ), patch(
        "app.services.slo.get_settings", return_value=_webhook_settings()
    ), patch(
        "app.services.alerts.httpx.AsyncClient", _RecordingClient
    ):
        return await slo_mod.run_evaluation(factory)


async def test_a_seeded_burn_produces_one_alert_and_one_webhook_per_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for finding 1: before this, zero alerts and zero deliveries — not "not yet",
    ever — and a loop without de-duplication would answer one alert per tick. 50 of 100 jobs
    dead-lettering is 50× burn against a 1% budget."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=50)
    await _seed_jobs(session_factory, status=JobStatus.DEAD_LETTER, count=50)

    first = await _run_evaluation(session_factory)
    second = await _run_evaluation(session_factory)  # the next tick, same window

    assert len(first) == 1, "a real platform condition produced no alert"
    assert second == [], "a sustained burn minted a second alert in one window"

    alerts = await _alerts(session_factory)
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].source == "slo:job_completion_rate"
    assert alerts[0].dedup_key is not None

    assert len(_RecordingClient.posts) == 1, (
        "one condition, one delivery — the webhook is the commander's trigger"
    )


async def test_the_webhook_payload_carries_what_the_commander_needs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An alert that says only "something is wrong" costs the agent a
    round-trip it can be spared: the runbook pointer and the burn numbers are
    the difference between diagnosing and asking."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=50)
    await _seed_jobs(session_factory, status=JobStatus.DEAD_LETTER, count=50)

    await _run_evaluation(session_factory)

    payload = _RecordingClient.posts[0]["payload"]
    assert payload["severity"] == "critical"
    assert payload["source"] == "slo:job_completion_rate"
    extra = payload["extra_data"]
    assert extra["slo_id"] == "job_completion_rate"
    assert extra["runbook_id"] == "rb-slo-job-completion"
    assert extra["threshold"] == slo_mod.FAST_BURN_THRESHOLD
    assert extra["burn_rate"] >= slo_mod.FAST_BURN_THRESHOLD
    assert extra["total"] == 100
    assert extra["failed"] == 50
    assert "X-Alert-Signature" in _RecordingClient.posts[0]["headers"]


async def test_a_healthy_platform_raises_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of "alerts from real conditions": no condition, no
    alert. One dead letter in a hundred is exactly the budget, not a burn."""
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=99)
    await _seed_jobs(session_factory, status=JobStatus.DEAD_LETTER, count=1)

    created = await _run_evaluation(session_factory)

    assert created == []
    assert await _alerts(session_factory) == []
    assert _RecordingClient.posts == []


async def test_an_idle_platform_raises_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing ran, so nothing was promised and nothing was broken. An empty
    window must not page — `_state` reports it healthy with burn 0."""
    created = await _run_evaluation(session_factory)

    assert created == []
    assert await _alerts(session_factory) == []


async def test_a_burn_alerts_again_in_the_next_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """De-duplication must not become suppression: the key carries a time bucket, so a burn that
    outlives the window gets a fresh key and alerts again."""
    now = datetime(2026, 8, 30, 10, 30, 0, tzinfo=UTC)
    window = 3600.0

    same_window = slo_mod._fast_burn_dedup_key(
        "job_completion_rate", window, now + timedelta(minutes=20)
    )
    first = slo_mod._fast_burn_dedup_key("job_completion_rate", window, now)
    next_window = slo_mod._fast_burn_dedup_key(
        "job_completion_rate", window, now + timedelta(hours=1)
    )

    assert first == same_window
    assert first != next_window


async def test_two_objectives_burning_raise_one_alert_each(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """De-duplication is per objective, not global — the key is built from
    the SLO id. Two things being wrong at once must not hide one of them."""
    # The latency objective's 5% budget needs a far higher failure share to reach the same 14.4x: 60
    # of 70 never dispatched is ~86%, which burns both.
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=10)
    await _seed_jobs(
        session_factory,
        status=JobStatus.DEAD_LETTER,
        count=60,
        dispatch_delay_seconds=None,
    )

    created = await _run_evaluation(session_factory)

    sources = {a.source for a in await _alerts(session_factory)}
    assert len(created) == 2
    assert sources == {"slo:job_completion_rate", "slo:job_dispatch_latency"}


async def test_the_database_refuses_a_duplicate_dedup_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The guarantee that makes cross-replica de-duplication safe: `worker_loop` runs in every
    replica, so look-then-insert is a race both can win and the unique constraint settles it
    (`run_evaluation` reads the IntegrityError as "already alerted"). Asserted at the constraint,
    because this suite's SQLite serialises everything onto one connection."""
    from sqlalchemy.exc import IntegrityError

    async def _insert(key: str) -> None:
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    Alert(
                        id=uuid.uuid4(),
                        tenant_id=DEFAULT_TENANT_ID,
                        severity="critical",
                        source="slo:job_completion_rate",
                        title="SLO fast burn",
                        dedup_key=key,
                    )
                )

    await _insert("slo:job_completion_rate:fast_burn:1")
    with pytest.raises(IntegrityError):
        await _insert("slo:job_completion_rate:fast_burn:1")

    # A different window is a different key, and is allowed.
    await _insert("slo:job_completion_rate:fast_burn:2")
    # NULL keys are the normal case and never collide with each other.
    await _insert_null_key(session_factory)
    await _insert_null_key(session_factory)

    assert len(await _alerts(session_factory)) == 4


async def _insert_null_key(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        async with session.begin():
            session.add(
                Alert(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    severity="info",
                    source="chaos:bad_deploy",
                    title="Simulated bad deploy",
                )
            )


async def test_slo_evaluation_loop_is_registered_in_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Required by the spec, and the whole finding: the computation already existed and was correct
    — what was missing was anything that ran it."""
    started: set[str] = set()

    def _recorder(name: str) -> Any:
        async def _loop(*_args: Any, **_kwargs: Any) -> None:
            started.add(name)
            await asyncio.Event().wait()

        return _loop

    for name in (
        "_promote_delayed_loop",
        "_promote_dlq_replay_loop",
        "_resume_unblocked_waiting_loop",
        "_requeue_stale_pending_loop",
        "_outbox_relay_loop",
        "_metrics_loop",
        "_digest_loop",
        "_idempotency_reaper_loop",
        "_stale_running_sweep_loop",
        "_renew_running_leases_loop",
        "_slo_evaluation_loop",
    ):
        monkeypatch.setattr(dispatcher_mod, name, _recorder(name))
    monkeypatch.setattr(
        dispatcher_mod, "_supervise_consumer", _recorder("consumers")
    )

    task = asyncio.create_task(
        dispatcher_mod.worker_loop(session_factory, None)
    )
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "_slo_evaluation_loop" in started, (
        "the SLO loop is not in worker_loop's task list — the objectives are "
        "computed on demand only, and the alert webhook has no producer"
    )


# The eval world may run with evaluation ON (WO-R2-132).
#
# These two build their population from `scripts/seed_eval_fixtures.py`'s own specs, because the
# claim is about that world specifically. A hand-written copy of the fixture shape would keep
# passing after the seed changed.


def _seed_module() -> Any:
    """`scripts/` is not a package on disk; make it importable first."""
    import importlib
    import os

    root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    scripts = os.path.join(root, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("seed_eval_fixtures")


async def _seed_the_eval_world(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    """Insert the standing eval fixtures the way the seed script does — 4 DLQ rows, 2 failed traces
    and the 3-node DAG, each with its lifecycle and the `eval_fixture` payload marker."""
    seed = _seed_module()
    now = datetime.now(UTC)
    rows: list[Job] = []

    def _add(job_id: uuid.UUID, status: str, spec: dict[str, Any]) -> None:
        created_at, started_at, completed_at = seed._lifecycle(
            now, spec["created_offset"], spec["run_seconds"]
        )
        rows.append(
            Job(
                id=job_id,
                tenant_id=DEFAULT_TENANT_ID,
                user_id=_USER_ID,
                type=JobType.BULK_API_SYNC,
                status=status,
                payload={"eval_fixture": True},
                created_at=created_at,
                updated_at=completed_at or created_at,
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    for spec in seed._dlq_specs():
        _add(spec["job_id"], JobStatus.DEAD_LETTER, spec)
    for spec in seed._failed_trace_specs():
        _add(spec["job_id"], JobStatus.FAILED, spec)
    for spec in seed._dag_specs():
        _add(seed.stable(spec["name"]), spec["status"], spec)

    async with factory() as session:
        async with session.begin():
            session.add_all(rows)
    return len(rows)


async def test_a_freshly_seeded_eval_world_raises_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for WO-R2-132: two passes over a world of nothing but seeded fixtures. Before
    the exclusion this raised a critical fast-burn on `job_completion_rate` within one interval of
    every boot, which is why the eval world ran with `SLO_EVALUATION_INTERVAL_SECONDS=0` — switching
    off the only non-chaos producer of the alert the agent under test is woken by."""
    seeded = await _seed_the_eval_world(session_factory)
    assert seeded == 9, "the seeded world is 4 DLQ + 2 failed + 3 DAG rows"

    first = await _run_evaluation(session_factory)
    second = await _run_evaluation(session_factory)

    assert first == [], "a fresh eval world alerted on its own fixtures"
    assert second == []
    assert await _alerts(session_factory) == []
    assert _RecordingClient.posts == [], "no webhook, so no agent woken"


async def test_a_real_burn_beside_the_fixtures_still_alerts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exclusion must narrow what is measured, not switch it off: the same seeded world plus 50
    real dead-letters in 100 real jobs still pages, on a total of 100 rather than 105."""
    await _seed_the_eval_world(session_factory)
    await _seed_jobs(session_factory, status=JobStatus.COMPLETED, count=50)
    await _seed_jobs(session_factory, status=JobStatus.DEAD_LETTER, count=50)

    created = await _run_evaluation(session_factory)

    assert len(created) == 1
    alerts = await _alerts(session_factory)
    assert alerts[0].source == "slo:job_completion_rate"
    assert alerts[0].extra_data["total"] == 100, (
        "the seeded fixtures leaked into the denominator"
    )
    assert alerts[0].extra_data["failed"] == 50
