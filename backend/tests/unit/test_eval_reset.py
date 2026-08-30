"""Tests for the eval-reset bundle (FIX_PLAN #7, #19, #79):

  * `seed_eval_fixtures._seed_hot_set` populates the stale-cache
    fixture that `remediate_stale_cache_success` depends on.
  * `seed_eval_fixtures._reset_dlq_state` restores mutated fixture
    rows back to baseline without touching non-fixture data.
  * `reset_eval_state._clear_chaos_keys` finds and deletes every key
    matching the chaos patterns.
  * `reset_eval_state` refuses to run against ENVIRONMENT=production —
    from `main()` (SystemExit) *and* from the programmatic `reset()`
    entry point (RuntimeError, raised before anything is connected).
  * `reset_eval_state._delete_seeded_dlq_fixtures` keys off the
    structured top-level marker, not a substring of the serialized
    payload.
  * `reset_eval_state` never reads or writes `audit_logs` (ADR 0012's
    "reset disposal vs audit ground truth" amendment).

Import-guarded so the seed script's heavy DB imports (SQLAlchemy engine,
alembic wiring) don't fire until the test needs them."""

from __future__ import annotations

import ast
import fnmatch
import importlib
import inspect
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
# Shared session harness
#
# Every destructive helper in reset_eval_state opens its own
# `async with session.begin()`, but the `db_session` fixture already owns
# the transaction — so `begin()` has to be a no-op while each statement
# still hits the real DB. Hoisted to module level because four tests
# need it (it was copy-pasted into two of them before).
# ---------------------------------------------------------------------------


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
        # Forwards `bind` too, which is what the dialect branch in
        # `_delete_seeded_dlq_fixtures` reads.
        return getattr(self._s, name)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *_a):  # type: ignore[no-untyped-def]
        return False


def _factory(session):  # type: ignore[no-untyped-def]
    """A `session_factory`-shaped callable over an existing session."""
    return lambda: _SessionProxy(session)


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
# _rebaseline_timestamps — BUILD_PLAN 2.5 (time re-baselining)
# ---------------------------------------------------------------------------


def _utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; they're UTC by construction."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


_SHIFT_TOL = timedelta(minutes=2)


async def test_rebaseline_refreshes_stale_fixture_timestamps(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Two days after seeding, `search_traces(since_hours=1)` found
    nothing: created_at offsets are computed at seed time and no reset
    restored them, so age-sensitive scenarios graded against an
    apparently healthy system (verified live 2026-08-16 — post-reset
    dead_letter rows still carried created_at from 2026-08-14).

    The re-baseline re-anchors every fixture row to its spec offset from
    *now* — shifted, not flattened, so the stories the offsets encode
    keep their relative spacing."""
    seed = _seed_module()
    from app.models.alert import Alert
    from app.models.deploy_marker import DeployMarker

    stale = timedelta(days=2)
    now = datetime.now(UTC)

    # A failed-trace job aged 2 days past its 45-minute offset — the
    # exact row the search_traces(since_hours=1) scenario asserts on.
    trace_spec = seed._failed_trace_specs()[0]
    stale_created = now - stale - trace_spec["created_offset"]
    db_session.add(
        Job(
            id=trace_spec["job_id"],
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=trace_spec["type"],
            status=JobStatus.FAILED.value,
            payload={"eval_fixture": True},
            retry_count=2,
            error_message=trace_spec["error_message"],
            trace_id=trace_spec["trace_id"],
            created_at=stale_created,
            updated_at=stale_created,
        )
    )
    # Two deploy markers whose 4-hour hotfix→latest spacing must survive.
    deploy_specs = seed._deploy_rows(default_tenant.id)
    d_hotfix = next(s for s in deploy_specs if s["version"] == "v0.4.2")
    d_latest = next(
        s
        for s in deploy_specs
        if s["version"] == "v0.4.3" and s["environment"] == "prod"
    )
    for spec in (d_hotfix, d_latest):
        db_session.add(
            DeployMarker(**{**spec, "deployed_at": spec["deployed_at"] - stale})
        )
    # An active fixture alert, aged the same way.
    alert_spec = next(
        s
        for s in seed._alert_rows(default_tenant.id)
        if s["id"] == seed.stable("alert-kafka-active")
    )
    db_session.add(Alert(**{**alert_spec, "fired_at": alert_spec["fired_at"] - stale}))
    await db_session.flush()

    shifted = await seed._rebaseline_timestamps(db_session)
    assert shifted == 4

    db_session.expire_all()
    job = (
        await db_session.execute(select(Job).where(Job.id == trace_spec["job_id"]))
    ).scalar_one()
    job_target = now - trace_spec["created_offset"]
    assert abs(_utc(job.created_at) - job_target) < _SHIFT_TOL
    assert abs(_utc(job.updated_at) - job_target) < _SHIFT_TOL

    markers = {
        m.id: m
        for m in (
            await db_session.execute(
                select(DeployMarker).where(
                    DeployMarker.id.in_([d_hotfix["id"], d_latest["id"]])
                )
            )
        ).scalars()
    }
    hotfix = markers[d_hotfix["id"]]
    latest = markers[d_latest["id"]]
    assert abs(_utc(hotfix.deployed_at) - (now - timedelta(hours=6))) < _SHIFT_TOL
    # Shift, don't flatten: the hotfix stays 4 hours before the latest
    # prod deploy — the deploy-then-failure story survives the re-anchor.
    assert (
        abs((_utc(latest.deployed_at) - _utc(hotfix.deployed_at)) - timedelta(hours=4))
        < _SHIFT_TOL
    )

    alert = (
        await db_session.execute(
            select(Alert).where(Alert.id == alert_spec["id"])
        )
    ).scalar_one()
    assert abs(_utc(alert.fired_at) - (now - timedelta(minutes=25))) < _SHIFT_TOL
    assert alert.resolved_at is None, "an active fixture alert stays active"


async def test_rebaseline_leaves_organic_rows_untouched(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Only stable() fixture ids are addressed — a live-traffic job with
    a genuinely old created_at keeps it."""
    seed = _seed_module()
    old = datetime.now(UTC) - timedelta(days=3)
    organic = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.DEAD_LETTER.value,
        retry_count=3,
        error_message="organic, not a fixture",
        created_at=old,
        updated_at=old,
    )
    db_session.add(organic)
    await db_session.flush()

    assert await seed._rebaseline_timestamps(db_session) == 0

    await db_session.refresh(organic)
    assert abs(_utc(organic.created_at) - old) < timedelta(seconds=1)
    assert abs(_utc(organic.updated_at) - old) < timedelta(seconds=1)


async def test_rebaseline_is_noop_when_already_fresh(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Rows already at their now-relative offsets aren't rewritten, so
    back-to-back resets keep the documented no-op idempotency."""
    seed = _seed_module()
    spec = seed._dlq_specs()[0]
    created = datetime.now(UTC) - spec["created_offset"]
    db_session.add(
        Job(
            id=spec["job_id"],
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=spec["type"],
            status=JobStatus.DEAD_LETTER.value,
            payload={"eval_fixture": True},
            retry_count=spec["retry_count"],
            error_message=spec["error_message"],
            remediation_hint=spec.get("remediation_hint"),
            trace_id=str(seed.stable(f"dlq-trace-{spec['job_id']}")),
            created_at=created,
            updated_at=created,
        )
    )
    await db_session.flush()

    assert await seed._rebaseline_timestamps(db_session) == 0


# ---------------------------------------------------------------------------
# _clear_chaos_keys — FIX_PLAN #79
# ---------------------------------------------------------------------------


def test_every_chaos_key_helper_lives_under_the_chaos_namespace() -> None:
    """`_CHAOS_KEY_PATTERNS` is only complete because every chaos key a
    helper can produce is under `chaos:*`. A future helper that escapes
    the namespace escapes the reset silently — so assert the property
    statically rather than trusting a hand-typed pattern list."""
    from app.workers.kafka_consumer import kill_key_for, latency_key_for

    for helper in (kill_key_for, latency_key_for):
        assert fnmatch.fnmatch(helper("any-group"), "chaos:*"), (
            f"{helper.__name__} produces a key outside chaos:* — either move "
            "it back under that namespace or add a pattern for it"
        )


async def test_clear_chaos_keys_scans_and_deletes_matching_patterns() -> None:
    """The SCAN inputs are the REAL key shapes the helpers emit.

    The previous version fed `kafka:consumer:<group>:killed`, a shape no
    code has ever written — the tuple carried two patterns matching
    nothing (D-13), and the test cemented the fiction.
    """
    reset = _reset_module()
    from app.workers.kafka_consumer import kill_key_for, latency_key_for

    redis = AsyncMock()
    matched = [
        kill_key_for("worker-dispatcher").encode(),
        latency_key_for("audit-writer").encode(),
        b"chaos:bad_deploy",
    ]
    # Real Redis returns (cursor, keys) tuples and terminates on cursor=0.
    scan_results = iter([(0, matched)])
    scanned_patterns: list[str] = []

    def _scan(**kwargs):  # type: ignore[no-untyped-def]
        scanned_patterns.append(kwargs["match"])
        return next(scan_results)

    redis.scan.side_effect = _scan
    redis.delete = AsyncMock(return_value=3)

    deleted = await reset._clear_chaos_keys(redis)

    assert deleted == 3
    assert redis.delete.await_count == 1
    # Exactly one SCAN, because exactly one pattern is live.
    assert scanned_patterns == ["chaos:*"]
    assert reset._CHAOS_KEY_PATTERNS == ("chaos:*",)


# ---------------------------------------------------------------------------
# Tier-1 action residue — delayed replay timers + DAG pauses
# ---------------------------------------------------------------------------


async def test_clear_scheduled_replays_drops_pending_timers() -> None:
    """A 5-minute replay scheduled by one scenario used to survive the
    reset and fire during the next one.

    Both sets are swept (R2-21): an un-acked claim on the in-flight set
    is a pending replay that a later tick is *designed* to recover, so
    leaving it would restore the bleed."""
    reset = _reset_module()
    redis = AsyncMock()
    redis.zcard.side_effect = [3, 2]

    cleared = await reset._clear_scheduled_replays(redis)

    assert cleared == 5
    # Asserted against the worker's constants, not re-hardcoded strings.
    # The reset script duplicates these literals on purpose, so it has no
    # import dependency on the worker package — which means this test is
    # the only thing standing between that duplication and a silent
    # divergence. Re-typing the literal here made the pair unguarded:
    # renaming SCHEDULED_KEY would leave the script sweeping a key
    # nothing writes any more, and the test would still pass (R2-76).
    from app.workers.dlq_replay_scheduler import INFLIGHT_KEY, SCHEDULED_KEY

    assert [c.args[0] for c in redis.delete.await_args_list] == [
        SCHEDULED_KEY,
        INFLIGHT_KEY,
    ]


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
    rows across every eval run.

    The predicate is the *structured* top-level marker
    (`seed_dlq_messages.SEEDED_FIXTURE_MARKER` writes
    `payload={"seeded_fixture": True}`), so the three adversarial
    payloads below survive. HEAD's `CAST(payload AS text) LIKE
    '%"seeded_fixture"%'` deleted all four (S-02): a substring match
    against the serialized payload hits the marker as a *value*, at any
    nesting depth, and with any value including `false` — a hard DELETE
    with CASCADE onto `job_triages` and SET NULL onto the audit FKs.
    """
    reset = _reset_module()
    from app.mcp.tools.chaos.seed_dlq_messages import SEEDED_FIXTURE_MARKER

    def _job(payload: dict[str, object]) -> Job:
        return Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.BULK_API_SYNC.value,
            status=JobStatus.DEAD_LETTER.value,
            payload=payload,
            retry_count=3,
        )

    # The one row the chaos hook actually writes.
    seeded = _job({SEEDED_FIXTURE_MARKER: True})
    ordinary = _job({"real": True})
    # Adversarial: marker as a VALUE, marker nested under another key,
    # marker present but false. None of these is declared scaffolding.
    marker_as_value = _job({"tag": SEEDED_FIXTURE_MARKER})
    marker_nested = _job({"nested": {SEEDED_FIXTURE_MARKER: True}})
    marker_false = _job({SEEDED_FIXTURE_MARKER: False})
    survivors = [ordinary, marker_as_value, marker_nested, marker_false]
    db_session.add_all([seeded, *survivors])
    await db_session.flush()
    survivor_ids = {job.id for job in survivors}

    deleted = await reset._delete_seeded_dlq_fixtures(_factory(db_session))

    assert deleted == 1
    remaining = set(
        (
            await db_session.execute(
                select(Job.id).where(Job.id.in_(survivor_ids | {seeded.id}))
            )
        ).scalars()
    )
    assert remaining == survivor_ids, (
        "only the top-level boolean-true marker row may be deleted"
    )


# ---------------------------------------------------------------------------
# _resolve_chaos_alerts — D-03, the compensator ADR 0008's amendment requires
# ---------------------------------------------------------------------------


async def test_resolve_chaos_alerts_clears_bad_deploy_residue(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """`bad_deploy` fires a critical alert that nothing ever resolved, so
    every invocation permanently added one active critical alert to the
    surface later alert-count/noise scenarios are graded against.

    The reset resolves it (never deletes — the alert id appears in the
    invoking scenario's trajectory), leaves non-chaos alerts alone, and
    doesn't re-stamp alerts that were already resolved."""
    reset = _reset_module()
    from app.models.alert import Alert
    from app.repositories.alert import AlertRepository

    now = datetime.now(UTC)
    already_resolved_at = now - timedelta(hours=2)
    tenant_id = default_tenant.id
    chaos_active = Alert(
        tenant_id=tenant_id,
        severity="critical",
        source="chaos:bad_deploy",
        title="Simulated bad deploy",
        fired_at=now - timedelta(minutes=5),
        resolved_at=None,
    )
    kafka_active = Alert(
        tenant_id=tenant_id,
        severity="critical",
        source="kafka",
        title="billing-consumer lag exceeds 10k",
        fired_at=now - timedelta(minutes=25),
        resolved_at=None,
    )
    chaos_resolved = Alert(
        tenant_id=tenant_id,
        severity="critical",
        source="chaos:bad_deploy",
        title="Simulated bad deploy",
        fired_at=now - timedelta(hours=3),
        resolved_at=already_resolved_at,
    )
    db_session.add_all([chaos_active, kafka_active, chaos_resolved])
    await db_session.flush()

    chaos_active_id = chaos_active.id
    kafka_active_id = kafka_active.id
    chaos_resolved_id = chaos_resolved.id

    resolved = await reset._resolve_chaos_alerts(_factory(db_session))

    # Only the ACTIVE chaos alert is touched — the already-resolved one
    # must not have its resolved_at re-stamped (idempotent second run).
    assert resolved == 1

    # The raw UPDATE bypassed the identity map; re-read from the DB.
    db_session.expire_all()
    rows = {
        row.id: row
        for row in (
            await db_session.execute(
                select(Alert).where(Alert.tenant_id == tenant_id)
            )
        ).scalars()
    }
    assert rows[chaos_active_id].resolved_at is not None
    assert rows[kafka_active_id].resolved_at is None, "non-chaos alert untouched"
    assert rows[chaos_resolved_id].resolved_at is not None

    # The post-reset active-alert surface is the seeded baseline: no
    # chaos residue survives where `list_active_alerts` can read it.
    active, total = await AlertRepository(db_session).list_active_for_tenant(
        tenant_id
    )
    assert total == 1
    assert [a.source for a in active] == ["kafka"]
    assert not [a for a in active if a.source.startswith("chaos:")]


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


async def test_reset_refuses_production_before_creating_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The programmatic entry point is gated too (D-08).

    `_refuse_in_production()` ran only in `main()`, so `reset()` — which
    is exported, takes arbitrary DB/Redis URLs, and is what the eval
    harness imports — executed every destructive step against a
    production platform while its own docstring claimed the script was
    gated. The guard now raises `RuntimeError` as `reset()`'s first
    statement: a library caller gets an exception, the CLI keeps
    `SystemExit` (asserted above), and nothing is connected first.
    """
    reset = _reset_module()
    from app.config import get_settings

    def _no_engine(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "reset() must refuse before creating an engine — the gate has to "
            "precede create_async_engine, not follow it"
        )

    monkeypatch.setattr(reset, "create_async_engine", _no_engine)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # The Settings validator refuses the default `change-me-...` key under
    # ENVIRONMENT=production; feed it a long-enough one so the guardrail
    # path is what we actually reach.
    monkeypatch.setenv("SECRET_KEY", "a" * 48)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="refuses to run in production"):
            await reset.reset(
                database_url="sqlite+aiosqlite://",
                redis_url="redis://nowhere",
            )
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Audit ground truth — D-10 / ADR 0012 amendment
# ---------------------------------------------------------------------------


def _sql_literals(module) -> list[str]:  # type: ignore[no-untyped-def]
    """Every string handed to a `text(...)` call in the module's source.

    Parsed rather than grepped so prose (docstrings, comments — which
    *do* discuss audit_logs, deliberately) can't satisfy or trip the
    assertion. Non-literal arguments are unparsed back to source so a
    future f-string SQL builder is still screened."""
    literals: list[str] = []
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "text":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literals.append(arg.value)
            else:
                literals.append(ast.unparse(arg))
    return literals


def test_reset_sql_never_names_audit_logs() -> None:
    """Static tripwire: audit rows are immutable to the reset.

    The commander grades against `audit_logs` (invariant 6), so the reset
    must never write, update or delete one. Today it doesn't — this pins
    that, and fails loudly the moment someone "tidies up" audit rows
    alongside the jobs and users the reset legitimately deletes."""
    reset = _reset_module()
    statements = _sql_literals(reset)
    assert statements, "expected the reset to issue raw SQL; parser found none"
    offenders = [sql for sql in statements if "audit_logs" in sql]
    assert offenders == [], (
        "reset_eval_state must not touch audit_logs — the deleted rows' "
        "identity survives on audit_logs.resource_id (ADR 0012 amendment)"
    )


async def test_reset_deletes_leave_audit_rows_byte_identical(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Behavioural half of the same contract.

    Both hard-DELETE helpers run over a job that a pre-existing audit row
    references. The audit row must survive with its action, `resource_id`
    and `extra_data` untouched — `resource_id` is the durable join key
    any grader or forensic query must use, because the FK columns are
    nulled by design (`ON DELETE SET NULL`; asserted on the Postgres
    harness, where FK actions actually fire)."""
    reset = _reset_module()
    from app.mcp.tools.chaos.seed_dlq_messages import SEEDED_FIXTURE_MARKER
    from app.models.audit import AuditLog
    from app.models.user import User

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
    seeded_job = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={SEEDED_FIXTURE_MARKER: True},
        retry_count=3,
    )
    db_session.add_all([chaos_job, seeded_job])
    await db_session.flush()

    extra_data = {"previous_status": "dead_letter", "previous_retry_count": 3}
    audits = [
        AuditLog(
            tenant_id=default_tenant.id,
            job_id=job.id,
            action="job.replayed",
            resource_type="job",
            resource_id=str(job.id),
            extra_data=extra_data,
        )
        for job in (chaos_job, seeded_job)
    ]
    db_session.add_all(audits)
    await db_session.flush()
    audit_ids = [row.id for row in audits]
    expected_resource_ids = {str(chaos_job.id), str(seeded_job.id)}

    assert await reset._delete_chaos_owner_users(_factory(db_session)) == 1
    assert await reset._delete_seeded_dlq_fixtures(_factory(db_session)) == 1

    db_session.expire_all()
    surviving = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.id.in_(audit_ids))
        )
    ).scalars().all()
    assert len(surviving) == 2, "the reset must never delete an audit row"
    assert {row.action for row in surviving} == {"job.replayed"}
    assert {row.resource_id for row in surviving} == expected_resource_ids
    assert [row.extra_data for row in surviving] == [extra_data, extra_data]


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


async def test_clear_poisoned_job_cache_sweeps_the_live_read_namespace() -> None:
    """R2-20: the reset sweeps `chaos:*`, but a stale-cache write that
    landed on `cache:job:{tenant}:{job}` carries no chaos marker — so a
    poisoned entry outlived the reset for the rest of its TTL, and the
    500s it caused could not be correlated with the scenario that caused
    them. Deleting the read cache is free: it is a 10s read-through
    cache that repopulates from Postgres on the next request."""
    import uuid as _uuid

    from app.utils.cache import JobCache

    reset = _reset_module()
    poisoned = JobCache._key(_uuid.uuid4(), _uuid.uuid4())

    redis = AsyncMock()
    scan_results = iter([(0, [poisoned.encode()])])
    scanned_patterns: list[str] = []

    def _scan(**kwargs):  # type: ignore[no-untyped-def]
        scanned_patterns.append(kwargs["match"])
        return next(scan_results)

    redis.scan.side_effect = _scan
    redis.delete = AsyncMock(return_value=1)

    deleted = await reset._clear_job_read_cache(redis)

    assert deleted == 1
    assert scanned_patterns == [reset._JOB_CACHE_PATTERN]
    # The pattern must actually match the key builder's output — a
    # hand-typed pattern that matches nothing is the D-13 failure mode.
    assert fnmatch.fnmatch(poisoned, reset._JOB_CACHE_PATTERN)


# ---------------------------------------------------------------------------
# _purge_idempotency_records — WO-R2-18 finding 2
# ---------------------------------------------------------------------------


async def test_purge_idempotency_survives_two_tenants_holding_the_sa(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """`service_accounts.name` is unique *per tenant*, not globally.

    The purge resolved it with `scalar_one_or_none()`, so a second
    tenant that had also seeded an `incident-commander` account made the
    whole reset raise `MultipleResultsFound` — and it raised from the
    tail of `reset()`, where every destructive step had already
    committed. The operator got a traceback, no summary, and a non-zero
    exit describing a reset that had in fact already deleted rows. Two
    tenants sharing one database is the normal shape of a dev stack, so
    nothing unusual had to happen for this to fire (WO-R2-18).

    Green-after: both commanders' records are purged, and an unrelated
    principal's are not."""
    import uuid as _uuid

    from app.models.idempotency import IdempotencyRecord
    from app.models.service_account import ServiceAccount
    from app.models.tenant import Tenant

    second_tenant = Tenant(
        id=_uuid.uuid4(), name="Second Tenant", slug="second-tenant"
    )
    db_session.add(second_tenant)
    await db_session.flush()

    accounts = {
        "commander_a": ServiceAccount(
            id=_uuid.uuid4(),
            tenant_id=default_tenant.id,
            name="incident-commander",
            scopes=["telemetry:read"],
        ),
        # Same name, different tenant — legal, and what used to break it.
        "commander_b": ServiceAccount(
            id=_uuid.uuid4(),
            tenant_id=second_tenant.id,
            name="incident-commander",
            scopes=["telemetry:read"],
        ),
        "other": ServiceAccount(
            id=_uuid.uuid4(),
            tenant_id=default_tenant.id,
            name="some-other-agent",
            scopes=["telemetry:read"],
        ),
    }
    db_session.add_all(list(accounts.values()))
    await db_session.flush()

    for key, sa in accounts.items():
        db_session.add(
            IdempotencyRecord(
                id=_uuid.uuid4(),
                tenant_id=sa.tenant_id,
                principal_id=sa.id,
                tool_name="replay_dlq_by_ids",
                idempotency_key=f"{key}-1",
                arguments_hash="deadbeef",
                response_json={"ok": True},
            )
        )
    await db_session.flush()
    other_id = accounts["other"].id

    reset = _reset_module()
    purged = await reset._purge_idempotency_records(_factory(db_session))

    # Both commanders, neither more nor fewer — and no exception.
    assert purged == 2
    remaining = (
        (await db_session.execute(select(IdempotencyRecord.principal_id)))
        .scalars()
        .all()
    )
    assert list(remaining) == [other_id]


async def test_purge_idempotency_is_a_noop_with_no_commander(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The zero-match path still returns 0 rather than tripping over an
    empty IN list."""
    reset = _reset_module()
    assert await reset._purge_idempotency_records(_factory(db_session)) == 0


def test_idempotency_purge_precedes_the_destructive_steps() -> None:
    """Ordering, asserted on the source rather than by running a reset.

    The purge can still fail for reasons this module does not control (a
    lock, a dropped connection). What must never recur is a failure
    there landing *after* the deletes have committed, leaving an
    operator with a traceback and no record of what was destroyed. So it
    runs first: nothing in the reset creates idempotency records, which
    makes the ordering free."""
    reset = _reset_module()
    body = inspect.getsource(reset.reset)
    purge_at = body.index("_purge_idempotency_records(factory)")
    for destructive in (
        "_clear_chaos_keys(redis)",
        # Added by #162 (WO-R2-20). Listed here so the guard keeps pace
        # with the steps it guards: a new destructive step that lands
        # after the purge is exactly what this test exists to catch.
        "_clear_job_read_cache(redis)",
        "_delete_chaos_owner_users(factory)",
        "_delete_seeded_dlq_fixtures(factory)",
        "_sweep_nonfixture_dlq(factory)",
    ):
        assert purge_at < body.index(destructive), (
            f"{destructive} must not commit before the idempotency purge "
            "has had its chance to fail"
        )


# ---------------------------------------------------------------------------
# _seed_consumer_lag durability — WO-R2-17
# ---------------------------------------------------------------------------


class _ExpiringRedis:
    """Redis stub that actually honours `ex`, driven by a fake clock.

    The 24h-TTL bug is invisible to an `AsyncMock`: it records the
    `ex` kwarg and answers every later `get`. To assert the fixtures
    survive an aged stack the stub has to expire them the way Redis
    would."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self.now = 0.0

    async def set(self, key: str, value: object, ex: int | None = None) -> bool:
        expires_at = None if ex is None else self.now + ex
        self._store[key] = (str(value), expires_at)
        return True

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self.now >= expires_at:
            del self._store[key]
            return None
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_seeded_lag_fixtures_survive_a_stack_older_than_a_day() -> None:
    """R2-17: the seven fixture lag keys were written with a 24h TTL
    while every other fixture is durable. Six live scenarios assert a
    non-null lag for these groups, so a stack up longer than a day since
    its last seed/reset answered `lag: null` and burned a paid run —
    with no precondition guarding it.

    Seeds, ages the clock past 24h, and reads back the way
    `get_consumer_lag` would."""
    seed = _seed_module()
    redis = _ExpiringRedis()

    await seed._seed_consumer_lag(redis)
    redis.advance(24 * 3600 + 60)

    for group, expected in seed._CONSUMER_LAGS.items():
        raw = await redis.get(f"{seed._LAG_KEY_PREFIX}{group}")
        assert raw is not None, (
            f"{group} lag fixture expired after 24h — the scenarios that "
            "assert a non-null lag for it now fail as agent errors"
        )
        assert int(raw) == expected


async def test_seed_consumer_lag_writes_no_expiry() -> None:
    """The mechanism behind the test above, pinned directly: durable
    like every other fixture, re-anchored by the reset rather than by a
    TTL. `worker-dispatcher` stays out of the fixture set — the metrics
    loop owns that key with a 90s TTL and must not be overwritten."""
    seed = _seed_module()
    redis = AsyncMock()

    await seed._seed_consumer_lag(redis)

    assert redis.set.await_count == len(seed._CONSUMER_LAGS)
    for call in redis.set.await_args_list:
        assert call.kwargs.get("ex") is None, (
            "seeded lag keys must be durable — a TTL makes an aged stack "
            "answer lag: null for groups scenarios assert are non-null"
        )
    assert "worker-dispatcher" not in seed._CONSUMER_LAGS
