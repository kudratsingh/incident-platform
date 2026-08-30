"""Scheduled SLO evaluation, real-condition alerts, and cancellations (WO-R2-29).

Two findings that had to land together. Turning on scheduled evaluation while
the dispatch-latency objective still counted every cancellation as a dispatch
failure would have meant paging on saga rollbacks — the alerting would have
been worse than none, because it would have been confidently wrong.

  * `compute_all` had exactly one caller, a read-only admin endpoint, so
    nothing evaluated the objectives on a schedule and no real platform
    condition ever created an Alert. The alert webhook is the incident
    commander's production trigger and its only producer was a chaos tool.
  * the `job_dispatch_latency` SLO admitted CANCELLED into its denominator.
    Those rows never left PENDING — they were cancelled while WAITING — so
    each arrived with `started_at IS NULL` and was counted as a dispatch miss.

Real rows on a real (SQLite in-memory) engine: both halves are SQL predicates
and a unique constraint. The engine is module-local so committed rows never
leak into the shared session-scoped `sqlite_engine` other suites roll back
against.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
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


# ---------------------------------------------------------------------------
# Finding 2 — cancellations are not dispatch failures
# ---------------------------------------------------------------------------


async def test_a_six_step_saga_rollback_does_not_move_the_objective(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE assertion for finding 2, in the shape the spec names.

    A saga that rolls back cancels its remaining steps; the dependency
    cascade does the same to a stranded parent's WAITING descendants. Those
    rows never left PENDING, so every one of them reached the objective with
    `started_at IS NULL` and was counted as a job we failed to dispatch. A
    rollback is a decision, not an outage, and it must not cost error budget.
    """
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
    """Why the two findings had to land together.

    `cascade_cancel_blocked_children` cancels a whole DAG subtree in one
    write, so cancellations arrive in bulk rather than one at a time. With
    them in the denominator a single cascade could push the objective past
    14.4× on its own — so switching on scheduled evaluation first would have
    paged an operator, at critical severity, because a dependency parent
    failed and the platform correctly cleaned up after it.
    """
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
    """The fix must not over-correct.

    A job that reached a terminal state without anything ever claiming it is
    exactly what "we failed to dispatch it" means — no processor registered,
    or the dispatcher's safety net firing. Only CANCELLED leaves the
    denominator; `started_at IS NULL` stays a failure everywhere else.
    """
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
    """The portable fallback, asserted directly — and deliberately so.

    `_compute_latency_slo` builds `EXTRACT(EPOCH FROM started_at -
    created_at)`. SQLite does not raise on that: it evaluates the expression
    to NULL, so the comparison never matches and the SQL path silently
    reports zero latency failures on this engine. (The `started_at IS NULL`
    branch of the same CASE does work, which is why the tests above are
    meaningful.) Calling the fallback directly is therefore the only way this
    suite can assert the threshold behaviour at all.

    Both paths take their denominator from `_dispatched_in_window`, so this
    also proves the cancellation exclusion on the second implementation.
    """
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


def test_the_sql_denominator_excludes_cancellations_too() -> None:
    """The SQL path is never exercised by this suite, so assert it directly.

    `_compute_latency_slo` uses `EXTRACT(EPOCH FROM ...)`, which SQLite does
    not have, so every test above runs the Python fallback. Production runs
    the other one. Both now build their WHERE clause from
    `_dispatched_in_window`, and this compiles it to make the exclusion
    visible rather than merely intended.
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects import postgresql

    stmt = sa_select(Job.id).where(
        *slo_mod._dispatched_in_window(datetime.now(UTC))
    )
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "'cancelled'" not in sql
    assert "'waiting'" not in sql
    assert "'pending'" not in sql
    assert "'completed'" in sql
    assert "'dead_letter'" in sql


# ---------------------------------------------------------------------------
# Finding 1 — scheduled evaluation creates real alerts
# ---------------------------------------------------------------------------


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
    """THE assertion for finding 1.

    Before this order nothing evaluated the objectives on a schedule, so the
    answer here was zero alerts and zero deliveries — not "not yet", but
    ever. And a loop without de-duplication would answer with one alert per
    tick, which is how an alerting channel gets muted.

    50 of 100 jobs dead-lettering is a 50% failure rate against a 1% budget:
    50× burn, comfortably past the 14.4× threshold.
    """
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
    """De-duplication must not become suppression.

    The key carries a time bucket, so a burn that outlives the window gets a
    fresh key and alerts again — an incident that is still burning an hour
    later is worth saying twice.
    """
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
    # The latency objective has a 5% budget, so it needs a far higher failure
    # share than completion's 1% to reach the same 14.4x: 60 of 70 never
    # dispatched is ~86%, which burns both.
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
    """The guarantee that makes cross-replica de-duplication safe.

    `worker_loop` runs in every API replica, so two of them evaluate the same
    window at the same time. A "look for a recent alert, then insert" check
    is a race both can win; the unique constraint is what makes the database
    settle it, and `run_evaluation` treats the resulting IntegrityError as
    "already alerted".

    Asserted at the constraint rather than by racing two evaluations: this
    suite's SQLite engine serialises everything onto one connection, so a
    `gather` of two passes would prove nothing about concurrency and would
    only test the driver.
    """
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
    """Required by the spec, and the reason is the whole finding: the
    computation already existed and was already correct — what was missing was
    anything that ran it. A loop that is written but never registered leaves
    the alert webhook with no producer, exactly as before."""
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
