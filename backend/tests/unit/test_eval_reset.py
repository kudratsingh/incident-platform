"""Tests for the eval-reset bundle (FIX_PLAN #7, #19, #79):

  * `seed_eval_fixtures._seed_hot_set` populates the stale-cache
    fixture that `remediate_stale_cache_success` depends on.
  * `seed_eval_fixtures._reset_dlq_state` restores mutated fixture
    rows back to baseline without touching non-fixture data.
  * `reset_eval_state._clear_chaos_keys` finds and deletes every key
    matching the chaos patterns.
  * `reset_eval_state` refuses to run against ENVIRONMENT=production.

Import-guarded so the seed script's heavy DB imports (SQLAlchemy engine,
alembic wiring) don't fire until the test needs them."""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The scripts/ dir isn't a package on disk; make it importable.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _seed_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("seed_eval_fixtures")


def _reset_module():  # type: ignore[no-untyped-def]
    return importlib.import_module("reset_eval_state")


# ---------------------------------------------------------------------------
# _seed_hot_set — FIX_PLAN #19
# ---------------------------------------------------------------------------


async def test_seed_hot_set_populates_expected_key() -> None:
    seed = _seed_module()
    redis = AsyncMock()
    await seed._seed_hot_set(redis)
    redis.set.assert_awaited_once()
    key, value = redis.set.await_args.args
    assert key == "cache:jobs:worker-dispatcher:hot_set"
    # Value must be JSON and non-empty so the stale-cache scenario has
    # something to observe before it invalidates.
    parsed = json.loads(value)
    assert isinstance(parsed, list)
    assert len(parsed) >= 1
    # Referential integrity (D-14): every hot_set member must be a job
    # id that `_seed_dlq` actually seeds. Hardcoded stable() names
    # drifted once when `_dlq_specs` entries were renamed, leaving 2 of
    # 3 members pointing at jobs that never exist.
    assert set(parsed) <= {str(spec["job_id"]) for spec in seed._dlq_specs()}
    # TTL passed so evals don't drift mid-run.
    assert redis.set.await_args.kwargs.get("ex") == 24 * 3600


# ---------------------------------------------------------------------------
# _reset_dlq_state — FIX_PLAN #7
# ---------------------------------------------------------------------------


async def test_reset_dlq_state_restores_mutated_fixture(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A fixture DLQ job that a scenario mutated into RUNNING with
    retry_count=0 gets flipped back to DEAD_LETTER/retry_count=3, and
    the row's other invariants (id, tenant, type) are untouched."""
    seed = _seed_module()
    # Pick the first stable() DLQ spec so we know the ID + expected values.
    spec = seed._dlq_specs()[0]
    job_id = spec["job_id"]
    baseline_retry = spec["retry_count"]
    baseline_hint = spec.get("remediation_hint")
    baseline_error = spec["error_message"]
    now = datetime.now(UTC)

    # Pretend a scenario already ran: seed the fixture in a mutated state.
    db_session.add(
        Job(
            id=job_id,
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=spec["type"],
            status=JobStatus.RUNNING.value,  # mutated
            payload={"eval_fixture": True},
            retry_count=0,  # mutated (a replay reset it)
            error_message="stale value",  # mutated
            remediation_hint=None,  # mutated (cleared)
            trace_id=str(seed.stable(f"dlq-trace-{job_id}")),
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
    )
    await db_session.flush()

    reset_count = await seed._reset_dlq_state(db_session)

    # At least this row was reset — other specs might not have rows in
    # the test session, so we don't assert the exact count.
    assert reset_count >= 1
    restored = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    assert restored.status == JobStatus.DEAD_LETTER.value
    assert restored.retry_count == baseline_retry
    assert restored.remediation_hint == baseline_hint
    assert restored.error_message == baseline_error


async def test_reset_dlq_state_is_noop_when_already_baseline(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A fixture already at baseline doesn't get counted — the reset
    only touches drifted rows. Guards against a re-run turning into
    wasted UPDATEs (and against confusing the summary count)."""
    seed = _seed_module()
    spec = seed._dlq_specs()[0]
    now = datetime.now(UTC)
    db_session.add(
        Job(
            id=spec["job_id"],
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=spec["type"],
            status=JobStatus.DEAD_LETTER.value,  # already baseline
            payload={"eval_fixture": True},
            retry_count=spec["retry_count"],
            error_message=spec["error_message"],
            remediation_hint=spec.get("remediation_hint"),
            trace_id=str(seed.stable(f"dlq-trace-{spec['job_id']}")),
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
    )
    await db_session.flush()

    reset_count = await seed._reset_dlq_state(db_session)
    assert reset_count == 0


async def test_reset_dlq_state_leaves_non_fixture_rows_untouched(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Reset targets only stable() IDs. Non-fixture jobs — a random
    DEAD_LETTER row created by a scenario — must not be reverted."""
    seed = _seed_module()
    real_job = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.RUNNING.value,
        retry_count=99,
        error_message="scenario-owned, not a fixture",
    )
    db_session.add(real_job)
    await db_session.flush()

    await seed._reset_dlq_state(db_session)
    await db_session.refresh(real_job)
    assert real_job.status == JobStatus.RUNNING.value
    assert real_job.retry_count == 99


# ---------------------------------------------------------------------------
# _clear_chaos_keys — FIX_PLAN #79
# ---------------------------------------------------------------------------


async def test_clear_chaos_keys_scans_and_deletes_matching_patterns() -> None:
    reset = _reset_module()
    redis = AsyncMock()

    # Simulate SCAN returning two batches (one match) for the first
    # pattern, no matches for the rest. Real Redis returns (cursor,
    # keys) tuples and terminates on cursor=0.
    scan_results = iter(
        [
            (0, [b"chaos:killed:worker-dispatcher", b"chaos:latency:audit-writer"]),
            (0, [b"kafka:consumer:worker-dispatcher:killed"]),
            (0, []),
        ]
    )
    redis.scan.side_effect = lambda **_: next(scan_results)
    redis.delete = AsyncMock(side_effect=[2, 1])

    deleted = await reset._clear_chaos_keys(redis)
    assert deleted == 3
    assert redis.delete.await_count == 2


# ---------------------------------------------------------------------------
# Tier-1 action residue — delayed replay timers + DAG pauses
# ---------------------------------------------------------------------------


async def test_clear_scheduled_replays_drops_pending_timers() -> None:
    """A 5-minute replay scheduled by one scenario used to survive the
    reset and fire during the next one."""
    reset = _reset_module()
    redis = AsyncMock()
    redis.zcard.return_value = 3

    cleared = await reset._clear_scheduled_replays(redis)

    assert cleared == 3
    redis.delete.assert_awaited_once_with("jobs:dlq_replay_delayed")


async def test_clear_scheduled_replays_noop_when_empty() -> None:
    reset = _reset_module()
    redis = AsyncMock()
    redis.zcard.return_value = 0

    assert await reset._clear_scheduled_replays(redis) == 0
    redis.delete.assert_not_awaited()


async def test_clear_dag_pauses_removes_pause_flags() -> None:
    """Harmless before the resolver enforced pauses; since ADR 0011 a
    leftover flag holds the next scenario's DAG in WAITING."""
    reset = _reset_module()
    redis = AsyncMock()
    scan_results = iter([(0, [b"dag:paused:abc", b"dag:paused:def"])])
    redis.scan.side_effect = lambda **_: next(scan_results)
    redis.delete = AsyncMock(return_value=2)

    assert await reset._clear_dag_pauses(redis) == 2


# ---------------------------------------------------------------------------
# Empty-DLQ baseline mode — commander ADR 0010 / platform ADR 0012
# ---------------------------------------------------------------------------


def test_empty_dlq_baseline_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in on purpose: flipping the baseline is breaking for every
    dlq_* scenario written against the standing pool, so the platform
    ships the capability before the commander migrates."""
    reset = _reset_module()
    monkeypatch.delenv("EVAL_EMPTY_DLQ_BASELINE", raising=False)
    assert reset._empty_dlq_baseline() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes"])
def test_empty_dlq_baseline_accepts_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    reset = _reset_module()
    monkeypatch.setenv("EVAL_EMPTY_DLQ_BASELINE", raw)
    assert reset._empty_dlq_baseline() is True


@pytest.mark.parametrize("raw", ["0", "false", "", "no"])
def test_empty_dlq_baseline_rejects_falsy_spellings(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    reset = _reset_module()
    monkeypatch.setenv("EVAL_EMPTY_DLQ_BASELINE", raw)
    assert reset._empty_dlq_baseline() is False


async def test_delete_seeded_dlq_fixtures_removes_declared_rows(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Scenario-declared scaffolding is DELETEd, not cancelled — it
    isn't a real user's history and cancelling would accumulate dead
    rows across every eval run."""
    reset = _reset_module()

    seeded = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={"seeded_fixture": True},
        retry_count=3,
    )
    ordinary = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={"real": True},
        retry_count=3,
    )
    db_session.add_all([seeded, ordinary])
    await db_session.flush()

    # The fixture session already has a transaction open, so the
    # function's own `session.begin()` would raise. Wrap it so `begin()`
    # is a no-op while every statement still hits the real DB.
    class _NullTx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return None

        async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
            return False

    class _SessionProxy:
        def __init__(self, s):  # type: ignore[no-untyped-def]
            self._s = s

        def begin(self):  # type: ignore[no-untyped-def]
            return _NullTx()

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._s, name)

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
            return False

    deleted = await reset._delete_seeded_dlq_fixtures(
        lambda: _SessionProxy(db_session)
    )

    assert deleted == 1
    remaining = (
        await db_session.execute(select(Job.id).where(Job.id == ordinary.id))
    ).scalars().all()
    assert remaining, "non-seeded DLQ row must survive"


# ---------------------------------------------------------------------------
# _refuse_in_production — FIX_PLAN #79 guardrail
# ---------------------------------------------------------------------------


def test_refuse_in_production_exits_when_env_is_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _reset_module()
    # Config caches settings — clear the LRU so we see our override.
    from app.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "production")
    # Settings validator refuses the default `change-me-...` SECRET_KEY
    # when ENVIRONMENT=production. Feed it something that satisfies the
    # length requirement so the guardrail path is reachable.
    monkeypatch.setenv("SECRET_KEY", "a" * 48)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc_info:
            reset._refuse_in_production()
        assert exc_info.value.code == 1
    finally:
        get_settings.cache_clear()


def test_refuse_in_production_allows_non_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _reset_module()
    from app.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    try:
        # No exception, no exit — just returns.
        reset._refuse_in_production()
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# _delete_chaos_owner_users — follow-up to PR #83's tenant-fallback fix
# ---------------------------------------------------------------------------


async def test_delete_chaos_owner_users_removes_users_and_their_jobs(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The chaos-owner user + any chaos jobs it owns are removed. Real
    users' jobs (owned by non-chaos users) survive."""
    reset = _reset_module()
    from contextlib import asynccontextmanager

    from app.models.user import User
    from sqlalchemy import select as _select

    # Chaos user + one owned chaos job.
    chaos_user = User(
        tenant_id=default_tenant.id,
        email=f"chaos-owner+{default_tenant.id}@chaos.local",
        hashed_password="!chaos-owner-no-login",
        role="user",
        is_active=False,
    )
    db_session.add(chaos_user)
    await db_session.flush()
    chaos_job = Job(
        tenant_id=default_tenant.id,
        user_id=chaos_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={"chaos_fixture": "bad_data_job"},
        retry_count=3,
    )
    db_session.add(chaos_job)
    # Real user + real job — must survive.
    real_job = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.PENDING.value,
    )
    db_session.add(real_job)
    await db_session.flush()

    # Fake session factory that yields the test session with a no-op
    # begin() (the outer test fixture already owns the transaction).
    @asynccontextmanager
    async def _noop_begin():  # type: ignore[no-untyped-def]
        yield

    class _FakeFactoryCtx:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            db_session.begin = _noop_begin  # type: ignore[assignment]
            return db_session

        async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
            return None

    class _FakeFactory:
        def __call__(self):  # type: ignore[no-untyped-def]
            return _FakeFactoryCtx()

    deleted = await reset._delete_chaos_owner_users(_FakeFactory())
    assert deleted == 1

    # Chaos user gone; chaos job gone (deleted first for FK); real job intact.
    assert (
        await db_session.execute(
            _select(User).where(User.id == chaos_user.id)
        )
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(_select(Job).where(Job.id == chaos_job.id))
    ).scalar_one_or_none() is None
    assert (
        await db_session.execute(_select(Job).where(Job.id == real_job.id))
    ).scalar_one_or_none() is not None
