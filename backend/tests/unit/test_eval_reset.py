"""Tests for the eval-reset bundle (FIX_PLAN #7, #19, #79): the stale-cache hot set, the DLQ
baseline restore, the chaos-key sweep, the production refusal from both entry points, the structured
seeded-fixture marker, and that the reset never touches `audit_logs` (ADR 0012).

Import-guarded so the seeder's heavy DB imports stay lazy.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import inspect
import json
import os
import sys
import uuid
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


# Shared session harness: every destructive helper opens its own `async with session.begin()`, but
# the `db_session` fixture already owns the transaction — so `begin()` has to be a no-op while each
# statement still hits the real DB. Module level because four tests need it.


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


# _seed_hot_set — FIX_PLAN #19


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
    # Referential integrity (D-14): every hot_set member must be a job id `_seed_dlq` really seeds.
    # Hardcoded stable() names drifted once.
    assert set(parsed) <= {str(spec["job_id"]) for spec in seed._dlq_specs()}
    # TTL passed so evals don't drift mid-run.
    assert redis.set.await_args.kwargs.get("ex") == 24 * 3600


# _reset_dlq_state — FIX_PLAN #7


async def test_reset_dlq_state_restores_mutated_fixture(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A fixture DLQ job a scenario mutated goes back to DEAD_LETTER/retry_count=3, id and tenant
    untouched."""
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
    """A fixture already at baseline is not counted: the reset only touches drifted rows."""
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
    """Only stable() ids are reset; other DEAD_LETTER rows stay."""
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


# _rebaseline_timestamps — BUILD_PLAN 2.5 (time re-baselining)


def _utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; they're UTC by construction."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


_SHIFT_TOL = timedelta(minutes=2)


async def test_rebaseline_refreshes_stale_fixture_timestamps(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Age-sensitive scenarios graded against an apparently healthy system because no reset
    re-anchored `created_at` (verified live 2026-08-16). The re-baseline shifts every fixture row to
    its spec offset from now, keeping the relative spacing."""
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
    deploy_specs = seed._deploy_rows()
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
    """Rows already at their now-relative offsets are not rewritten."""
    seed = _seed_module()
    spec = seed._dlq_specs()[0]
    # Fresh in `created_at` but missing `started_at`/`completed_at` is the incoherent shape WO-R2-69
    # fixed.
    created, started, completed = seed._lifecycle(
        datetime.now(UTC), spec["created_offset"], spec["run_seconds"]
    )
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
            updated_at=completed,
            started_at=started,
            completed_at=completed,
        )
    )
    await db_session.flush()

    assert await seed._rebaseline_timestamps(db_session) == 0


# _clear_chaos_keys — FIX_PLAN #79


def test_every_chaos_key_helper_lives_under_the_chaos_namespace() -> None:
    """`_CHAOS_KEY_PATTERNS` is complete only while every chaos key stays under `chaos:*`, so assert
    that statically rather than trusting the pattern list."""
    from app.workers.async_tasks import downstream_flag_key
    from app.workers.control_loop_pause import pause_key_for
    from app.workers.db_pool_hold import hold_key
    from app.workers.db_slow_query import slow_query_key
    from app.workers.kafka_consumer import kill_key_for, latency_key_for

    # The last three take no argument — one key each, which is what makes a repeat call replace the
    # state rather than stack a second one (ADR 0031, ADR 0034).
    for nullary in (hold_key, downstream_flag_key, slow_query_key):
        assert fnmatch.fnmatch(nullary(), "chaos:*"), (
            f"{nullary.__name__} produces a key outside chaos:* — either move "
            "it back under that namespace or add a pattern for it"
        )

    for helper in (kill_key_for, latency_key_for, pause_key_for):
        assert fnmatch.fnmatch(helper("any-group"), "chaos:*"), (
            f"{helper.__name__} produces a key outside chaos:* — either move "
            "it back under that namespace or add a pattern for it"
        )


async def test_clear_chaos_keys_scans_and_deletes_matching_patterns() -> None:
    """The SCAN inputs are the real key shapes the helpers emit; the old ones matched nothing
    (D-13)."""
    reset = _reset_module()
    from app.workers.control_loop_pause import ControlLoopName, pause_key_for
    from app.workers.kafka_consumer import kill_key_for, latency_key_for

    redis = AsyncMock()
    matched = [
        kill_key_for("worker-dispatcher").encode(),
        latency_key_for("audit-writer").encode(),
        pause_key_for(ControlLoopName.OUTBOX_RELAY).encode(),
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


# Tier-1 action residue — delayed replay timers + DAG pauses


async def test_clear_scheduled_replays_drops_pending_timers() -> None:
    """A scheduled replay used to survive the reset and fire in the next scenario. Both sets are
    swept (R2-21): an un-acked in-flight claim is a replay a later tick would recover."""
    reset = _reset_module()
    redis = AsyncMock()
    redis.zcard.side_effect = [3, 2]

    cleared = await reset._clear_scheduled_replays(redis)

    assert cleared == 5
    # Asserted against the worker's constants: the script duplicates these literals on purpose, so
    # re-typing them here would leave the pair unguarded (R2-76).
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
    """Since ADR 0011 a leftover flag strands the next scenario's DAG."""
    reset = _reset_module()
    redis = AsyncMock()
    scan_results = iter([(0, [b"dag:paused:abc", b"dag:paused:def"])])
    redis.scan.side_effect = lambda **_: next(scan_results)
    redis.delete = AsyncMock(return_value=2)

    assert await reset._clear_dag_pauses(redis) == 2


# Recorded consumer-lag measurements — WO-R3-254, reversed by WO-R3-333


def test_the_reset_no_longer_clears_the_recorded_lag_window() -> None:
    """WO-R3-333: the window is history, not residue.

    Clearing it opened the demo's fifteen-minute lag chart on two points while the fault it was
    drawn to show was climbing — and the window's TTL is longer than the value key's precisely so
    it outlives the pass that wrote it (ADR 0037). Asserted on the source rather than on a call,
    because the way this comes back is a helper somebody re-adds.
    """
    from app.workers.dispatcher import LAG_SAMPLES_KEY

    reset = _reset_module()
    source = inspect.getsource(reset)
    assert f'"{LAG_SAMPLES_KEY}"' not in source, (
        "reset_eval_state names the lag WINDOW key — since WO-R3-333 the reset "
        "preserves the window and must not delete it"
    )
    assert not hasattr(reset, "_clear_lag_samples"), (
        "the window-clearing helper is back; the counter is a permanent 0"
    )


def test_the_reset_still_reports_the_window_counter_as_zero() -> None:
    """The count stays in the summary (`make eval-reset` parses it, and the boundary row carries
    it), and it is now a claim: this reset preserved the window."""
    reset = _reset_module()
    assert reset._LAG_SAMPLES_CLEARED == 0

    tree = ast.parse(inspect.getsource(reset.reset))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == "lag_samples_cleared":
                assert isinstance(value, ast.Name), (
                    "lag_samples_cleared must report the named constant, so the "
                    "reason it is always 0 is one hop from the number"
                )
                assert value.id == "_LAG_SAMPLES_CLEARED"
                return
    raise AssertionError("lag_samples_cleared is no longer in the reset summary")


def test_the_reset_names_neither_consumer_lag_key() -> None:
    """The value key was already untouched — loop-owned under a 90 s TTL, so it is fresh-or-absent
    without help, and deleting it would only blind backpressure. Since WO-R3-333 the window is
    untouched as well, so the script may name neither."""
    from app.core.consumer_lag import LIVE_REFRESHED_GROUP, samples_key
    from app.utils.backpressure import BACKPRESSURE_LAG_KEY

    source = inspect.getsource(_reset_module())
    # Quoted on both sides: the window key starts with the value key, so an unquoted search would
    # match it.
    for key in (BACKPRESSURE_LAG_KEY, samples_key(LIVE_REFRESHED_GROUP)):
        assert f'"{key}"' not in source, (
            f"reset_eval_state names {key} — the metrics loop owns both consumer-lag "
            "keys and the reset must touch neither"
        )


# Empty-DLQ baseline mode — commander ADR 0010 / platform ADR 0012


def test_empty_dlq_baseline_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in on purpose: flipping the baseline breaks every dlq_* scenario written against the
    pool."""
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
    """Declared scaffolding is DELETEd, not cancelled. The predicate is the structured top-level
    marker (`seed_dlq_messages.SEEDED_FIXTURE_MARKER`), so the adversarial payloads below survive —
    HEAD's `CAST(payload AS text) LIKE` substring match deleted all four (S-02)."""
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


async def test_delete_seeded_dlq_fixtures_removes_a_bad_data_job_row(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The disposal half of WO-R2-158, against the payload the hook really writes.

    `create_bad_data_job` rows used to be merely cancelled, one dead row per run; the id is now
    pinned in advance, so the row is declared scaffolding and DELETEd. Three payload keys is why the
    predicate has to be JSONB containment and not equality."""
    reset = _reset_module()
    from app.mcp.tools.chaos.create_bad_data_job import fixture_id
    from app.mcp.tools.chaos.seed_dlq_messages import SEEDED_FIXTURE_MARKER

    job_id = fixture_id(default_tenant.id, "unfenced-csv")
    declared = Job(
        id=job_id,
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={
            SEEDED_FIXTURE_MARKER: True,
            "chaos_fixture": "bad_data_job",
            "fixture_name": "unfenced-csv",
        },
        retry_count=3,
        remediation_hint=None,
    )
    # An undeclared row: provenance only, no marker, so this sweep leaves it. Shaped like a
    # pre-v0.6.3 `poison_message` row — since WO-R2-166 `_sweep_nonfixture_dlq` catches only older
    # releases and organic dead-letters.
    undeclared = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={"chaos_fixture": "poison_message", "topic": "job.submitted"},
        retry_count=3,
    )
    db_session.add_all([declared, undeclared])
    await db_session.flush()
    undeclared_id = undeclared.id

    deleted = await reset._delete_seeded_dlq_fixtures(_factory(db_session))

    assert deleted == 1
    remaining = set(
        (
            await db_session.execute(
                select(Job.id).where(Job.id.in_([job_id, undeclared_id]))
            )
        ).scalars()
    )
    assert remaining == {undeclared_id}, (
        "the declared bad-data fixture must be deleted and the "
        "provenance-only chaos row left for the cancel sweep"
    )


async def test_delete_seeded_dlq_fixtures_removes_the_v063_declared_rows(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The disposal half of WO-R2-166, for both hooks that joined this sweep.

    Each payload is built through the hook's own `fixture_id` and the shared marker, so a shape
    change shows up here. On the mislabelled row disposal matters beyond tidiness: it is
    deliberately self-contradictory, so a cancelled copy per run would teach the wrong lesson."""
    reset = _reset_module()
    from app.mcp.tools.chaos.create_mislabeled_dlq_job import (
        fixture_id as mislabel_fixture_id,
    )
    from app.mcp.tools.chaos.poison_message import (
        fixture_id as poison_fixture_id,
    )
    from app.mcp.tools.chaos.seed_dlq_messages import SEEDED_FIXTURE_MARKER
    from app.models.enums import RemediationHint

    poison_id = poison_fixture_id(default_tenant.id, "poison-message")
    poisoned = Job(
        id=poison_id,
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.BULK_API_SYNC.value,
        status=JobStatus.DEAD_LETTER.value,
        payload={
            SEEDED_FIXTURE_MARKER: True,
            "chaos_fixture": "poison_message",
            "fixture_name": "poison-message",
            "topic": "job.submitted",
        },
        retry_count=3,
        remediation_hint=None,
    )
    mislabel_id = mislabel_fixture_id(default_tenant.id, "mislabeled-dlq-job")
    mislabelled = Job(
        id=mislabel_id,
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        status=JobStatus.DEAD_LETTER.value,
        type=JobType.CSV_UPLOAD.value,
        payload={
            SEEDED_FIXTURE_MARKER: True,
            "chaos_fixture": "mislabeled_dlq_job",
            "fixture_name": "mislabeled-dlq-job",
        },
        retry_count=3,
        remediation_hint=RemediationHint.REPLAY_SAFE.value,
    )
    db_session.add_all([poisoned, mislabelled])
    await db_session.flush()

    deleted = await reset._delete_seeded_dlq_fixtures(_factory(db_session))

    assert deleted == 2
    remaining = (
        await db_session.execute(
            select(Job.id).where(Job.id.in_([poison_id, mislabel_id]))
        )
    ).scalars().all()
    assert remaining == [], (
        "both v0.6.3 declared fixtures must be DELETEd, not cancelled"
    )


# _resolve_chaos_alerts — D-03, the compensator ADR 0008's amendment requires


async def test_resolve_chaos_alerts_clears_bad_deploy_residue(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """`bad_deploy` fires a critical alert nothing resolved, so every invocation moved the baseline
    later alert-count scenarios are graded against. The reset resolves it (never deletes), leaves
    non-chaos alerts alone, and does not re-stamp already-resolved ones."""
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


# _resolve_organic_alerts — WO-R2-131, the distractor that aborted run C


def test_the_spared_alert_ids_come_from_the_seed_itself() -> None:
    """The exclusion list is read from the seeder: a copy left in `reset_eval_state` would resolve a
    seeded fixture alert on every reset and move the `active alerts 3` baseline."""
    reset = _reset_module()
    seed = _seed_module()

    spared = reset._seeded_alert_ids()

    assert len(spared) == 5
    assert set(spared) == {
        uuid.UUID(str(spec["id"])) for spec in seed._alert_rows(uuid.uuid4())
    }


async def test_organic_alerts_are_resolved_and_the_seeded_five_survive(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Both halves of WO-R2-131: an organic `slo:*` alert — the shape `chaos:%` could never match,
    whose stray copies aborted run C — is resolved, the five seeded alerts survive, and a chaos
    alert in the population stays catchable if the sweep order ever changes."""
    reset = _reset_module()
    seed = _seed_module()
    from app.models.alert import Alert
    from app.repositories.alert import AlertRepository

    tenant_id = default_tenant.id
    now = datetime.now(UTC)
    specs = seed._alert_rows(tenant_id)
    seeded_active = {
        uuid.UUID(str(spec["id"]))
        for spec in specs
        if spec["resolved_at"] is None
    }
    seeded_resolved = {
        uuid.UUID(str(spec["id"])): spec["resolved_at"]
        for spec in specs
        if spec["resolved_at"] is not None
    }
    assert len(seeded_active) == 3, "the seeded baseline is 3 active alerts"

    organic = Alert(
        tenant_id=tenant_id,
        severity="critical",
        source="slo:job_completion_rate",
        title="SLO fast burn: Job completion rate",
        description="burning at 80.0x the sustainable rate",
        fired_at=now - timedelta(minutes=3),
        resolved_at=None,
        dedup_key="slo:job_completion_rate:fast_burn:487000",
    )
    chaos = Alert(
        tenant_id=tenant_id,
        severity="critical",
        source="chaos:bad_deploy",
        title="Simulated bad deploy",
        fired_at=now - timedelta(minutes=5),
        resolved_at=None,
    )
    db_session.add_all([*(Alert(**spec) for spec in specs), organic, chaos])
    await db_session.flush()
    organic_id = organic.id

    resolved = await reset._resolve_organic_alerts(_factory(db_session))

    assert resolved == 2, "the slo alert and the chaos alert, nothing else"

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
    assert rows[organic_id].resolved_at is not None
    for alert_id in seeded_active:
        assert rows[alert_id].resolved_at is None, (
            "a seeded fixture alert was swept — the world-audit baseline moved"
        )
    for alert_id, was_resolved in seeded_resolved.items():
        assert rows[alert_id].resolved_at is not None
        assert not _drifted_seconds(rows[alert_id].resolved_at, was_resolved), (
            "an already-resolved fixture alert had its timestamp re-stamped"
        )

    active, total = await AlertRepository(db_session).list_active_for_tenant(
        tenant_id
    )
    assert total == 3, "the post-reset surface is the seeded baseline"
    assert {a.source for a in active} == {"kafka", "dlq", "api"}

    assert await reset._resolve_organic_alerts(_factory(db_session)) == 0, (
        "second run over post-reset state must be a no-op"
    )


def _drifted_seconds(actual: datetime, target: datetime) -> bool:
    """True when two timestamps differ by more than a second; SQLite hands back naive datetimes."""
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return abs((actual - target).total_seconds()) > 1.0


async def test_an_alert_the_agent_raised_is_resolved_never_deleted(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """An alert is resolved, never deleted: its id is quoted in the invoking scenario's trajectory,
    so deleting one mutates the evidence (ADR 0012's audit amendment). `resolved_at` is the model's
    own off-switch."""
    reset = _reset_module()
    from app.models.alert import Alert

    alert = Alert(
        tenant_id=default_tenant.id,
        severity="critical",
        source="slo:job_dispatch_latency",
        title="SLO fast burn: Job dispatch latency",
        fired_at=datetime.now(UTC) - timedelta(minutes=2),
        resolved_at=None,
    )
    db_session.add(alert)
    await db_session.flush()
    alert_id = alert.id

    await reset._resolve_organic_alerts(_factory(db_session))

    db_session.expire_all()
    still_there = (
        await db_session.execute(select(Alert).where(Alert.id == alert_id))
    ).scalar_one()
    assert still_there.resolved_at is not None
    assert still_there.source == "slo:job_dispatch_latency"


# _refuse_in_production — FIX_PLAN #79 guardrail


def test_refuse_in_production_exits_when_env_is_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _reset_module()
    # Config caches settings — clear the LRU so we see our override.
    from app.config import get_settings

    monkeypatch.setenv("ENVIRONMENT", "production")
    # The Settings validator refuses the default SECRET_KEY under production; feed it a long enough
    # one so the guardrail path is reachable.
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
    """The programmatic entry point is gated too (D-08): `reset()` is exported and ran every
    destructive step against production while its docstring claimed it was gated. The guard now
    raises `RuntimeError` as its first statement, before anything connects."""
    reset = _reset_module()
    from app.config import get_settings

    def _no_engine(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "reset() must refuse before creating an engine — the gate has to "
            "precede create_async_engine, not follow it"
        )

    monkeypatch.setattr(reset, "create_async_engine", _no_engine)
    monkeypatch.setenv("ENVIRONMENT", "production")
    # Feed a long-enough SECRET_KEY so the production guardrail is the path we reach.
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


# Audit ground truth — D-10 / ADR 0012 amendment


def _sql_literals(module) -> list[str]:  # type: ignore[no-untyped-def]
    """Every string handed to a `text(...)` call, parsed rather than grepped so prose that does
    discuss audit_logs cannot satisfy or trip the assertion."""
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
    """Static tripwire: the commander grades against `audit_logs` (invariant 6), so no raw
    statement here may reach that table.

    Since WO-R3-327 the reset does APPEND one row to it — the `lab.world_reset` boundary —
    and that is the only audit write it performs. It goes through `AuditRepository.log`,
    so this tripwire keeps its whole original force: an UPDATE or DELETE against
    `audit_logs` is still impossible to write here, and so is a second writer smuggled in
    as SQL."""
    reset = _reset_module()
    statements = _sql_literals(reset)
    assert statements, "expected the reset to issue raw SQL; parser found none"
    offenders = [sql for sql in statements if "audit_logs" in sql]
    assert offenders == [], (
        "reset_eval_state must not touch audit_logs in SQL — the deleted rows' "
        "identity survives on audit_logs.resource_id (ADR 0012 amendment), and the "
        "one row it appends goes through the repository"
    )


def test_the_reset_only_ever_appends_to_the_audit_log() -> None:
    """The other half of the tripwire above, on the ORM side: the boundary row is written
    by `record_world_reset` and nothing in this module may mutate an existing row."""
    reset = _reset_module()
    source = inspect.getsource(reset)
    tree = ast.parse(source)
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "record_world_reset" in called, (
        "the boundary row must go through app.services.operator_audit, which is where "
        "the stream's writer and its withholding rule live together"
    )
    assert "AuditLog" not in source, (
        "the reset must not construct an audit row itself — one writer, one place"
    )


async def test_reset_deletes_leave_audit_rows_byte_identical(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Behavioural half: an audit row referencing a hard-DELETEd job keeps its action, `resource_id`
    and `extra_data`. `resource_id` is the durable join key, because the FK columns are nulled by
    design (`ON DELETE SET NULL`)."""
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
        # An undeclared chaos row: no `seeded_fixture` marker, so the DELETE sweep leaves it and
        # this one cancels it (pre-v0.6.3 `poison_message` shape).
        payload={"chaos_fixture": "poison_message"},
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


# _delete_chaos_owner_users — follow-up to PR #83's tenant-fallback fix


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
        # An undeclared chaos row: no `seeded_fixture` marker, so the DELETE sweep leaves it and
        # this one cancels it (pre-v0.6.3 `poison_message` shape).
        payload={"chaos_fixture": "poison_message"},
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
    """R2-20: a stale-cache write on `cache:job:{tenant}:{job}` carries no chaos marker, so a
    poisoned entry outlived the reset for its TTL. Deleting the read cache is free — it repopulates
    from Postgres on the next request."""
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


# _purge_idempotency_records — WO-R2-18 finding 2


async def test_purge_idempotency_survives_two_tenants_holding_the_sa(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """`service_accounts.name` is unique per tenant, not globally, so `scalar_one_or_none()` raised
    `MultipleResultsFound` from the tail of `reset()` — after every destructive step had committed
    (WO-R2-18). Two tenants in one database is the normal shape of a dev stack. Green-after: both
    commanders are purged, an unrelated principal is not."""
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
    """Ordering asserted on the source: a purge failure must never land after the deletes have
    committed, leaving an operator with a traceback and no record of what was destroyed. Nothing in
    the reset creates idempotency records, so going first is free."""
    reset = _reset_module()
    body = inspect.getsource(reset.reset)
    purge_at = body.index("_purge_idempotency_records(factory)")
    for destructive in (
        "_clear_chaos_keys(redis)",
        # Added by #162 (WO-R2-20); listed so the guard keeps pace with the steps it guards.
        "_clear_job_read_cache(redis)",
        "_delete_chaos_owner_users(factory)",
        "_delete_seeded_dlq_fixtures(factory)",
        "_sweep_nonfixture_dlq(factory)",
    ):
        assert purge_at < body.index(destructive), (
            f"{destructive} must not commit before the idempotency purge "
            "has had its chance to fail"
        )


# _seed_consumer_lag durability — WO-R2-17


class _ExpiringRedis:
    """Redis stub that honours `ex` against a fake clock.

    An `AsyncMock` records the kwarg and answers every later `get`, which hides the TTL bug."""

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
    """R2-17: the seven fixture lag keys carried a 24h TTL while every other fixture is durable, so
    a stack up longer than a day answered `lag: null` and burned a paid run."""
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
    """Durable like every other fixture, re-anchored by the reset rather than by a TTL.
    `worker-dispatcher` stays out: the metrics loop owns that key under a 90s TTL."""
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


# Fixture rows match the contracts that read them (WO-R2-69): `update_status` stamps `started_at`
# and `completed_at`, and two readers take their absence literally — the dispatch-latency SLO counts
# `started_at IS NULL` as a miss, and the DLQ sort orders on `coalesce(completed_at, created_at)`.


def test_every_dispatched_fixture_spec_carries_a_run_duration() -> None:
    """A terminal job with no run duration cannot be given a coherent
    lifecycle, so the specs have to supply one."""
    seed = _seed_module()
    for spec in (*seed._dlq_specs(), *seed._failed_trace_specs()):
        assert isinstance(spec["run_seconds"], int), spec["job_id"]
    dag = {s["name"]: s for s in seed._dag_specs()}
    assert dag["dag-parent-job"]["run_seconds"] is not None
    # The waiting nodes were never dispatched and must stay that way.
    assert dag["dag-seed-job"]["run_seconds"] is None
    assert dag["dag-child-job"]["run_seconds"] is None


def test_lifecycle_orders_created_started_completed() -> None:
    seed = _seed_module()
    now = datetime.now(UTC)

    created, started, completed = seed._lifecycle(now, timedelta(minutes=40), 12)

    assert started is not None and completed is not None
    assert created <= started <= completed
    # Dispatch latency stays well inside the SLO's 30s threshold, so a
    # fixture never contributes a latency violation of its own.
    assert (started - created).total_seconds() == 4

    # Never-dispatched jobs keep NULLs rather than invented timestamps.
    assert seed._lifecycle(now, timedelta(minutes=5), None)[1:] == (None, None)


async def test_rebaseline_leaves_no_job_starting_before_it_was_created(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """THE assertion for finding 2: the re-baseline moved `created_at` and left `started_at`, so the
    DAG trio started before it was created — a negative dispatch latency in every tool that
    subtracts the two."""
    seed = _seed_module()
    stale = timedelta(days=2)
    now = datetime.now(UTC)

    specs = [*seed._dlq_specs(), *seed._failed_trace_specs()]
    for spec in specs:
        created, started, completed = seed._lifecycle(
            now - stale, spec["created_offset"], spec["run_seconds"]
        )
        db_session.add(
            Job(
                id=spec["job_id"],
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type=spec["type"],
                status=JobStatus.DEAD_LETTER.value,
                payload={"eval_fixture": True},
                retry_count=3,
                error_message=spec["error_message"],
                trace_id=str(seed.stable(f"seedtrace-{spec['job_id']}")),
                created_at=created,
                updated_at=completed,
                started_at=started,
                completed_at=completed,
            )
        )
    # The DAG trio as an older run left it: the parent ran two days ago.
    dag_ids = {}
    for spec in seed._dag_specs():
        job_id = seed.stable(spec["name"])
        dag_ids[spec["name"]] = job_id
        created, started, completed = seed._lifecycle(
            now - stale, spec["created_offset"], spec["run_seconds"]
        )
        db_session.add(
            Job(
                id=job_id,
                tenant_id=default_tenant.id,
                user_id=test_user.id,
                type=JobType.BULK_API_SYNC.value,
                status=spec["status"],
                payload={"eval_fixture": True},
                retry_count=0,
                created_at=created,
                updated_at=completed or created,
                started_at=started,
                completed_at=completed,
            )
        )
    await db_session.flush()

    await seed._rebaseline_timestamps(db_session)

    rows = (
        (await db_session.execute(select(Job).where(Job.id.in_(
            [s["job_id"] for s in specs] + list(dag_ids.values())
        )))).scalars().all()
    )
    assert len(rows) == len(specs) + 3
    for job in rows:
        created = _utc(job.created_at)
        started = _utc(job.started_at) if job.started_at is not None else None
        completed = (
            _utc(job.completed_at) if job.completed_at is not None else None
        )
        if started is not None:
            assert started >= created, f"{job.id} started before it was created"
        if completed is not None:
            assert completed >= started or completed >= created
        # The reset advances rows it resets. Drained DAG children are excluded:
        # their seed spec is WAITING but their boot-time status is COMPLETED.
        if job.id not in {dag_ids["dag-seed-job"], dag_ids["dag-child-job"]}:
            assert created > now - stale + timedelta(hours=1)
        if started is not None:
            assert started > now - stale + timedelta(hours=1)

    parent = next(j for j in rows if j.id == dag_ids["dag-parent-job"])
    assert parent.started_at is not None, "a completed job must have a start"
    assert parent.completed_at is not None
    assert (
        _utc(parent.started_at) - _utc(parent.created_at)
    ).total_seconds() >= 0, "the DAG trio's dispatch latency went negative"

    for waiting in ("dag-seed-job", "dag-child-job"):
        job = next(j for j in rows if j.id == dag_ids[waiting])
        assert job.started_at is None, "a waiting job was never dispatched"
        assert job.completed_at is None
        assert _utc(job.created_at) < now - stale + timedelta(hours=1)


async def test_seeded_deploy_markers_are_platform_wide(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """A deploy marker's tenant_id is always NULL — the seeder was the writer that broke it."""
    from app.models.deploy_marker import DeployMarker

    seed = _seed_module()

    assert all(spec["tenant_id"] is None for spec in seed._deploy_rows())

    # A stack seeded before this fix already holds tenant-stamped rows, and
    # check-then-insert would skip past them forever.
    stamped = seed._deploy_rows()[0]
    db_session.add(DeployMarker(**{**stamped, "tenant_id": default_tenant.id}))
    await db_session.flush()

    await seed._seed_deploys(db_session)
    await db_session.flush()

    markers = (await db_session.execute(select(DeployMarker))).scalars().all()
    assert len(markers) == len(seed._deploy_rows())
    assert [m.tenant_id for m in markers] == [None] * len(markers)


# The seeded pack's TEXTS are re-baselined too (WO-R2-146)


async def test_reset_restores_a_row_carrying_the_old_contradictory_text(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A stack seeded before WO-R2-146 holds `replay_safe` rows whose text names a permanent fault —
    the pair a live run graded an honest escalation on — so it must not survive a reset. Also the
    general case: a replay overwrites `error_message` with the processor's own error."""
    from app.lab.dlq_failure_stories import coherence_violations

    seed = _seed_module()
    spec = seed._dlq_specs()[0]
    assert spec["remediation_hint"] == "replay_safe"
    now = datetime.now(UTC)

    db_session.add(
        Job(
            id=spec["job_id"],
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=spec["type"],
            status=JobStatus.DEAD_LETTER.value,
            payload={"eval_fixture": True},
            retry_count=spec["retry_count"],
            # Verbatim from live run efdc3b2a9864.
            error_message=(
                "SchemaValidationError: payload missing required field "
                "'user_id' (received keys: ['tenant_id', 'action', 'ts'])"
            ),
            remediation_hint=spec["remediation_hint"],
            trace_id=str(seed.stable(f"dlq-trace-{spec['job_id']}")),
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
    )
    await db_session.flush()

    assert await seed._reset_dlq_state(db_session) >= 1

    restored = (
        await db_session.execute(select(Job).where(Job.id == spec["job_id"]))
    ).scalar_one()
    assert restored.error_message == spec["error_message"]
    assert not coherence_violations(
        restored.remediation_hint, restored.error_message or ""
    )


async def test_reset_restores_a_drifted_triage_row(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """`list_dlq_messages` returns the triage inline, so a stale triage is the same contradiction
    one field lower. `_seed_dlq` only inserts when none exists, so nothing else updates one."""
    from app.models.triage import JobTriage

    seed = _seed_module()
    spec = seed._dlq_specs()[0]
    want = spec["triage"]
    now = datetime.now(UTC)

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
            remediation_hint=spec["remediation_hint"],
            trace_id=str(seed.stable(f"dlq-trace-{spec['job_id']}")),
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
    )
    db_session.add(
        JobTriage(
            id=spec["triage_id"],
            tenant_id=default_tenant.id,
            job_id=spec["job_id"],
            # The pre-WO-R2-146 story for this row: it told the agent to
            # fix the producer before replaying a row marked replay_safe.
            root_cause_category="schema_violation",
            summary=(
                "Producer sent a payload missing user_id — schema "
                "rejected it three times."
            ),
            suggested_fix=(
                "Fix the producer to include user_id, then replay the "
                "DLQ entry."
            ),
            is_retryable=True,
            confidence=0.91,
            model_used="seed-fixture",
            usage={"input_tokens": 0, "output_tokens": 0},
        )
    )
    await db_session.flush()

    # The job row is already at baseline, so the only thing to fix is
    # the triage — and the reset must still report the fixture as reset.
    assert await seed._reset_dlq_state(db_session) >= 1

    restored = (
        await db_session.execute(
            select(JobTriage).where(JobTriage.id == spec["triage_id"])
        )
    ).scalar_one()
    for field, expected in want.items():
        assert getattr(restored, field) == expected, field


async def test_reset_leaves_a_matching_triage_row_alone(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Back-to-back resets stay no-ops though a second field can now trigger a write."""
    from app.models.triage import JobTriage

    seed = _seed_module()
    spec = seed._dlq_specs()[0]
    now = datetime.now(UTC)

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
            remediation_hint=spec["remediation_hint"],
            trace_id=str(seed.stable(f"dlq-trace-{spec['job_id']}")),
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
        )
    )
    db_session.add(
        JobTriage(
            id=spec["triage_id"],
            tenant_id=default_tenant.id,
            job_id=spec["job_id"],
            model_used="seed-fixture",
            usage={"input_tokens": 0, "output_tokens": 0},
            **spec["triage"],
        )
    )
    await db_session.flush()

    assert await seed._reset_dlq_state(db_session) == 0


# WO-R3-267 — the hot_set the reset re-populates must READ as healthy


class _TinyRedis:
    """Just enough for `_seed_hot_set` to write and the record check to
    read: `set` and `get`, values held as written."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


async def test_reset_leaves_the_hot_set_reading_as_healthy(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The consistency the reset owes the new evidence: `get_cache_key_info` reports how many of the
    job records an entry names still exist, so after a reset every hot_set member must resolve —
    asserted through the tool's own resolver, because a copy would drift. Reading-level counterpart
    to `test_seed_hot_set_populates_expected_key`, which pins the id set."""
    from app.dependencies import Principal
    from app.mcp.registry import ToolContext
    from app.mcp.tools.cache_key_info import resolve_record_references

    seed = _seed_module()
    await seed._seed_dlq(db_session, default_tenant, test_user)
    await db_session.flush()

    redis = _TinyRedis()
    await seed._seed_hot_set(redis)
    key = seed._HOT_SET_KEY

    ctx = ToolContext(
        db=db_session,
        redis=redis,  # type: ignore[arg-type]
        principal=Principal(
            kind="service_account", tenant_id=default_tenant.id
        ),
    )
    referenced, found = await resolve_record_references(key, "string", ctx=ctx)

    assert referenced == len(json.loads(redis.store[key]))
    assert found == referenced, (
        "the reset re-populated the hot_set with ids the database does not "
        "hold — the next run would open on a reading that says the copy is "
        "out of date"
    )


async def test_the_hot_set_reading_is_not_healthy_by_construction(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Negative control: with the same key and no rows behind it the reading must read all-absent.
    """
    from app.dependencies import Principal
    from app.mcp.registry import ToolContext
    from app.mcp.tools.cache_key_info import resolve_record_references

    seed = _seed_module()
    redis = _TinyRedis()
    await seed._seed_hot_set(redis)

    ctx = ToolContext(
        db=db_session,
        redis=redis,  # type: ignore[arg-type]
        principal=Principal(
            kind="service_account", tenant_id=default_tenant.id
        ),
    )
    referenced, found = await resolve_record_references(
        seed._HOT_SET_KEY, "string", ctx=ctx
    )
    assert referenced and referenced > 0
    assert found == 0


# _reseed_hot_set — WO-R3-310: the reset owns the key, and says so


async def test_reseed_hot_set_restores_an_evicted_key_and_reports_it() -> None:
    """`saturate_redis` evicts this key — it is the one fixture written with a TTL, which
    is exactly what a `volatile-*` policy evicts first — and every later world then reads
    `exists: false`. The reset writes it back and the count is how the caller can tell."""
    reset = _reset_module()
    seed = _seed_module()
    redis = _TinyRedis()

    assert await reset._reseed_hot_set(redis) == 1
    assert redis.store[seed._HOT_SET_KEY] == seed.hot_set_payload()


async def test_reseed_hot_set_reports_nothing_to_do_on_an_intact_world() -> None:
    """The reset's standing promise is that a second run is a no-op summary."""
    reset = _reset_module()
    seed = _seed_module()
    redis = _TinyRedis()
    await seed._seed_hot_set(redis)

    assert await reset._reseed_hot_set(redis) == 0


async def test_reseed_hot_set_repairs_a_payload_that_drifted() -> None:
    """Present is not correct: the scenario reads the members, so a key holding something
    else is a world that opens on a reading nobody graded."""
    reset = _reset_module()
    seed = _seed_module()
    redis = _TinyRedis()
    await redis.set(seed._HOT_SET_KEY, '["not-a-seeded-job"]')

    assert await reset._reseed_hot_set(redis) == 1
    assert redis.store[seed._HOT_SET_KEY] == seed.hot_set_payload()


def test_the_hot_set_payload_is_read_from_the_seed_not_restated() -> None:
    """D-14 again: a hardcoded id set drifted once and left phantom UUIDs, so the reset
    compares against the seeder's own payload."""
    seed = _seed_module()
    assert json.loads(seed.hot_set_payload()) == [
        str(spec["job_id"]) for spec in seed._dlq_specs()[:3]
    ]


# _close_open_agent_runs — WO-R3-315


async def _sa_id(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    from app.core.scopes import Scope
    from app.models.service_account import ServiceAccount

    sa = ServiceAccount(
        tenant_id=tenant_id,
        name=f"reporter-{uuid.uuid4().hex[:8]}",
        scopes=[Scope.AGENT_RUNS_WRITE.value],
        is_active=True,
    )
    session.add(sa)
    await session.flush()
    return uuid.UUID(str(sa.id))


async def _agent_run(
    session: AsyncSession, tenant_id: uuid.UUID, **overrides: object
):  # type: ignore[no-untyped-def]
    from app.models.agent_run import AgentRun
    from app.models.enums import AgentRunState

    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "service_account_id": await _sa_id(session, tenant_id),
        "scenario": "remediate_consumer_lag_success",
        "state": AgentRunState.INVESTIGATING.value,
        "phase_history": [
            {"state": "triage", "at": "2026-09-19T05:00:00+00:00"},
            {"state": "investigating", "at": "2026-09-19T05:00:09+00:00"},
        ],
        "current_hypothesis": {"name": "consumer_saturation", "confidence": 0.75},
        "last_step": {"kind": "read", "tool": "get_consumer_lag"},
    }
    fields.update(overrides)
    run = AgentRun(**fields)
    session.add(run)
    await session.flush()
    return run


async def test_open_agent_runs_are_closed_as_failed_and_marked_by_the_reset(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """A run the reset ends stays open for ever otherwise: only the responder reports a
    terminal state, and the reset is the thing that just took its world away (ADR 0035
    recorded this gap; ADR 0036 closes it)."""
    reset = _reset_module()
    from app.models.agent_run import AgentRun
    from app.models.enums import AgentRunState

    run = await _agent_run(db_session, default_tenant.id)

    assert await reset._close_open_agent_runs(_factory(db_session)) == 1

    closed = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == run.id))
    ).scalar_one()
    assert closed.state == AgentRunState.FAILED.value
    assert closed.finished_at is not None
    assert closed.phase_history[:2] == [
        {"state": "triage", "at": "2026-09-19T05:00:00+00:00"},
        {"state": "investigating", "at": "2026-09-19T05:00:09+00:00"},
    ]
    last = closed.phase_history[-1]
    assert last["state"] == AgentRunState.FAILED.value
    assert last["closed_by"] == reset.AGENT_RUN_CLOSED_BY
    assert last["at"]


async def test_closing_a_run_leaves_the_responders_own_words_alone(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The rows are evidence, like `audit_logs` (ADR 0012): the reset says who closed the
    run and changes nothing the responder wrote."""
    reset = _reset_module()
    from app.models.agent_run import AgentRun

    briefing = {"final_state": "escalated", "escalation_reason": "budget spent"}
    run = await _agent_run(db_session, default_tenant.id, briefing=briefing)

    await reset._close_open_agent_runs(_factory(db_session))

    closed = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == run.id))
    ).scalar_one()
    assert closed.briefing == briefing
    assert closed.current_hypothesis == {
        "name": "consumer_saturation",
        "confidence": 0.75,
    }
    assert closed.last_step == {"kind": "read", "tool": "get_consumer_lag"}
    assert closed.scenario == "remediate_consumer_lag_success"


async def test_a_finished_run_is_left_alone_and_a_second_sweep_is_a_no_op(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    reset = _reset_module()
    from app.models.agent_run import AgentRun
    from app.models.enums import AgentRunState

    finished_at = datetime.now(UTC) - timedelta(minutes=5)
    done = await _agent_run(
        db_session,
        default_tenant.id,
        state=AgentRunState.RESOLVED.value,
        finished_at=finished_at,
        phase_history=[{"state": "resolved", "at": "2026-09-19T05:02:00+00:00"}],
    )
    await _agent_run(db_session, default_tenant.id)

    assert await reset._close_open_agent_runs(_factory(db_session)) == 1
    assert await reset._close_open_agent_runs(_factory(db_session)) == 0

    untouched = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == done.id))
    ).scalar_one()
    assert untouched.state == AgentRunState.RESOLVED.value
    assert untouched.phase_history == [
        {"state": "resolved", "at": "2026-09-19T05:02:00+00:00"}
    ]


async def test_the_reset_never_deletes_an_agent_run(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The order offered deleting lab-labelled rows; declined. A console read this, and a
    row that vanishes is a demo that cannot be replayed."""
    reset = _reset_module()
    from app.models.agent_run import AgentRun

    await _agent_run(db_session, default_tenant.id)
    await _agent_run(db_session, default_tenant.id)

    await reset._close_open_agent_runs(_factory(db_session))

    rows = (await db_session.execute(select(AgentRun))).scalars().all()
    assert len(rows) == 2


# The summary the commander prints — WO-R3-310's real cost


def test_the_reset_summary_names_every_counter_it_owns() -> None:
    """Static tripwire. `make eval-reset` parses this dict, so a step whose count is not
    in it is a step nobody can tell ran — which is how the hot_set gap survived long
    enough to cost 108 unledgered fixture values.

    Since WO-R3-327 the summary is built as one `counters` literal (which is also the
    `lab.world_reset` row's payload) and returned with `world_reset_recorded` added, so
    the keys are gathered from every dict literal in the function and the returned
    expression is checked separately."""
    reset = _reset_module()
    tree = ast.parse(inspect.getsource(reset.reset))
    keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert {
        "agent_runs_closed",
        "breakers_reset",
        "hot_set_reseeded",
        "world_reset_recorded",
    } <= keys

    returned = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert returned, "reset() must return its summary as a dict literal"
    # The returned literal spreads the counters and adds the boundary's own count —
    # the one number the row cannot carry about itself.
    assert any(key is None for key in returned[0].keys), (
        "reset() must return the counters it wrote to the boundary row, not a second "
        "dict that can drift from it"
    )
    assert "world_reset_recorded" in {
        key.value
        for key in returned[0].keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }

    awaited = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert {
        "_close_open_agent_runs",
        "_record_world_reset",
        "_reset_breaker_states",
        "_reseed_hot_set",
    } <= awaited


# The boundary row — WO-R3-327


async def test_the_reset_appends_one_world_reset_row_carrying_its_counters(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The row the `/demo` page reads as the end of a take. Without it the newest
    `chaos.*` row is still the previous take's, so a clean world opens the strip at
    `agent remediating`."""
    reset = _reset_module()
    from app.models.audit import PRINCIPAL_TYPE_SERVICE_ACCOUNT, AuditLog
    from app.services.operator_audit import WORLD_RESET_ACTION

    counters = {"chaos_keys_cleared": 4, "dlq_reset": 5, "hot_set_reseeded": 1}

    assert await reset._record_world_reset(_factory(db_session), counters) == 1

    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == WORLD_RESET_ACTION)
        )
    ).scalars().all()
    assert len(rows) == 1, "exactly one boundary per reset — it is a moment, not a log"
    row = rows[0]
    assert row.tenant_id == default_tenant.id
    assert row.principal_type == PRINCIPAL_TYPE_SERVICE_ACCOUNT
    assert row.extra_data == counters


async def test_the_boundary_row_is_attributed_to_the_evaluator_account(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Whoever may fire the lab may read the lab (ADR 0012): the principal that can read
    this withheld row is the one the row says wrote it."""
    reset = _reset_module()
    from app.core.scopes import Scope
    from app.models.audit import AuditLog
    from app.models.service_account import ServiceAccount
    from app.services.operator_audit import WORLD_RESET_ACTION

    evaluator = ServiceAccount(
        tenant_id=default_tenant.id,
        name="incident-commander-chaos",
        scopes=[Scope.CHAOS_INVOKE.value],
        is_active=True,
    )
    db_session.add(evaluator)
    await db_session.flush()

    await reset._record_world_reset(_factory(db_session), {})

    row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == WORLD_RESET_ACTION)
        )
    ).scalar_one()
    assert row.principal_id == evaluator.id


async def test_the_boundary_is_written_even_with_no_evaluator_account(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """A stack seeded for fixtures but not for the agent still gets its boundary. The
    row is worth more than the attribution, and `principal_id` is nullable by design."""
    reset = _reset_module()
    from app.models.audit import AuditLog
    from app.services.operator_audit import WORLD_RESET_ACTION

    assert await reset._record_world_reset(_factory(db_session), {}) == 1

    row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == WORLD_RESET_ACTION)
        )
    ).scalar_one()
    assert row.principal_id is None


async def test_the_boundary_row_is_not_in_the_chaos_stream(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The console reads the newest `chaos.*` row as the FAULT. A boundary filed there
    would read as the very thing it exists to say did not happen."""
    reset = _reset_module()
    from app.models.audit import AuditLog
    from app.services.operator_audit import CHAOS_ACTION_PREFIX

    await reset._record_world_reset(_factory(db_session), {})

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert rows
    assert not any(row.action.startswith(CHAOS_ACTION_PREFIX) for row in rows)
