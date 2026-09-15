"""Unit tests for the SLO computation."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.services.slo import SLODefinition, _state, compute_all, is_fast_burning
from sqlalchemy.ext.asyncio import AsyncSession


def _def(target: float = 0.99) -> SLODefinition:
    return SLODefinition(
        id="t", name="t", description="t", target=target, window_hours=24, runbook_id="rb"
    )


def test_no_traffic_is_healthy() -> None:
    """An idle service should not look broken in the dashboard."""
    s = _state(_def(), total=0, failed=0)
    assert s.healthy is True
    assert s.current == 1.0
    assert s.budget_remaining_pct == 100.0
    assert s.burn_rate == 0.0


def test_zero_failures_full_budget() -> None:
    s = _state(_def(0.99), total=100, failed=0)
    assert s.healthy is True
    assert s.current == 1.0
    assert s.budget_remaining_pct == 100.0
    assert s.burn_rate == 0.0


def test_at_target_burn_rate_one() -> None:
    """Exactly at the target = consuming budget at the SLO-window rate, burn=1×."""
    s = _state(_def(0.99), total=100, failed=1)
    assert s.healthy is True  # 0.99 >= 0.99
    assert s.current == 0.99
    assert abs(s.burn_rate - 1.0) < 1e-9
    assert abs(s.budget_remaining_pct - 0.0) < 1e-9


def test_above_target_breaches_and_burn_above_one() -> None:
    s = _state(_def(0.99), total=100, failed=5)
    assert s.healthy is False
    assert abs(s.current - 0.95) < 1e-9
    assert abs(s.burn_rate - 5.0) < 1e-9  # 0.05 / 0.01
    # Budget over-consumed; clamped at -100%.
    assert s.budget_remaining_pct == -100.0


def test_partial_consumption_reports_remaining_share() -> None:
    """50% of the budget used → 50% remaining."""
    s = _state(_def(0.99), total=200, failed=1)  # 0.5% failure rate vs 1% allowed
    assert abs(s.budget_remaining_pct - 50.0) < 1e-6
    assert abs(s.burn_rate - 0.5) < 1e-6
    assert s.healthy is True


def test_target_100pct_any_failure_breaches() -> None:
    s = _state(_def(1.0), total=100, failed=1)
    assert s.healthy is False
    assert s.burn_rate == float("inf")


# ---------------------------------------------------------------------------
# Lab fixtures are not platform traffic (WO-R2-132)
#
# `_LAB_FIXTURE_PAYLOAD_MARKERS` is a SQL predicate, so these run real rows
# through `compute_all` on the SQLite harness rather than asserting on the
# string. The Postgres spelling — JSONB containment, which is the one that
# actually ships — is covered in
# `backend/tests/integration/test_eval_reset_postgres.py`.
# ---------------------------------------------------------------------------

# The standing eval world's terminal jobs, in the proportions
# `scripts/seed_eval_fixtures.py` writes them: 4 dead-lettered DLQ rows and
# the DAG parent that completed. 4 failed of 5 is 80× the 99% objective's
# budget — a fast burn by construction, which is what made a freshly booted
# eval world page about itself within one evaluation interval.
_EVAL_WORLD = ((JobStatus.COMPLETED, 1), (JobStatus.DEAD_LETTER, 4))


async def _add_jobs(
    session: AsyncSession,
    tenant_id: Any,
    user_id: Any,
    *,
    status: str,
    count: int,
    payload: dict[str, Any] | None,
) -> None:
    created = datetime.now(UTC) - timedelta(hours=1)
    for _ in range(count):
        session.add(
            Job(
                tenant_id=tenant_id,
                user_id=user_id,
                type=JobType.BULK_API_SYNC.value,
                status=status,
                payload=payload,
                created_at=created,
                updated_at=created,
                started_at=created + timedelta(seconds=4),
                completed_at=created + timedelta(seconds=10),
            )
        )
    await session.flush()


async def _seed_world(
    session: AsyncSession,
    tenant_id: Any,
    user_id: Any,
    *,
    payload: dict[str, Any] | None,
) -> None:
    for status, count in _EVAL_WORLD:
        await _add_jobs(
            session,
            tenant_id,
            user_id,
            status=status,
            count=count,
            payload=payload,
        )


async def _completion(session: AsyncSession) -> Any:
    states = await compute_all(session)
    return next(s for s in states if s.definition.id == "job_completion_rate")


async def _latency(session: AsyncSession) -> Any:
    states = await compute_all(session)
    return next(s for s in states if s.definition.id == "job_dispatch_latency")


async def test_the_seeded_eval_world_does_not_burn_the_budget(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The WO-R2-132 assertion at the computation level.

    Rows the seed script wrote directly into `dead_letter` are not failures
    the platform produced, so they are in neither half of the fraction. With
    nothing else on the stack the objective reads as idle — which is the
    honest answer for a platform that has dispatched nothing."""
    await _seed_world(
        db_session,
        default_tenant.id,
        test_user.id,
        payload={"eval_fixture": True},
    )

    state = await _completion(db_session)

    assert state.total == 0
    assert state.failed == 0
    assert state.healthy is True
    assert is_fast_burning(state) is False


async def test_a_scenario_declared_fixture_is_excluded_too(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """`seeded_fixture` is the marker the chaos hooks write and the reset
    DELETEs. A scenario that seeds a DLQ backlog mid-run must not raise an
    SLO alert on top of the incident it is staging — that alert would be a
    distractor in the agent's own alert surface."""
    await _seed_world(
        db_session,
        default_tenant.id,
        test_user.id,
        payload={"seeded_fixture": True, "chaos_fixture": "bad_data_job"},
    )

    state = await _completion(db_session)

    assert state.total == 0
    assert is_fast_burning(state) is False


async def test_the_same_rows_unmarked_still_burn(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The other half: the exclusion must not blind the evaluator.

    Identical rows without a lab marker are real dead-letters and still
    produce the fast burn — otherwise this change would have closed the only
    non-chaos alert producer the platform has."""
    await _seed_world(db_session, default_tenant.id, test_user.id, payload={"real": 1})

    state = await _completion(db_session)

    assert state.total == 5
    assert state.failed == 4
    assert state.healthy is False
    assert is_fast_burning(state) is True


async def test_a_job_with_no_payload_stays_in_the_denominator(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """`payload` is nullable and most rows carry nothing interesting in it.

    Both dialect spellings return NULL for a NULL payload, so a bare
    `NOT (...)` would be NULL rather than true and would silently empty the
    denominator. The COALESCE in `_not_a_lab_fixture` is what this pins."""
    await _seed_world(db_session, default_tenant.id, test_user.id, payload=None)

    state = await _completion(db_session)

    assert state.total == 5
    assert is_fast_burning(state) is True


async def test_the_marker_must_be_a_top_level_true(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Containment, not a substring test — the S-02 tightening, restated
    where it now also decides an SLO.

    A row that merely mentions the word, nests it, or sets it false is
    ordinary traffic and stays counted. Anything looser and a caller could
    drop their own failures out of the objective by naming a payload key."""
    for payload in (
        {"tag": "eval_fixture"},
        {"nested": {"eval_fixture": True}},
        {"eval_fixture": False},
        {"seeded_fixture": "banana"},
    ):
        await _add_jobs(
            db_session,
            default_tenant.id,
            test_user.id,
            status=JobStatus.DEAD_LETTER,
            count=1,
            payload=payload,
        )

    state = await _completion(db_session)

    assert state.total == 4, "a near-miss marker must not exclude a row"
    assert state.failed == 4


async def test_the_dispatch_latency_objective_excludes_fixtures_too(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Both objectives share `_dispatched_in_window`, so both get the
    exclusion from one definition. Asserted rather than assumed: the latency
    SLO has two computation paths (SQL and the Python fallback) and only a
    shared denominator keeps them from drifting apart."""
    await _add_jobs(
        db_session,
        default_tenant.id,
        test_user.id,
        status=JobStatus.DEAD_LETTER,
        count=3,
        payload={"eval_fixture": True},
    )
    await _add_jobs(
        db_session,
        default_tenant.id,
        test_user.id,
        status=JobStatus.COMPLETED,
        count=2,
        payload=None,
    )

    state = await _latency(db_session)

    assert state.total == 2, "only the unmarked rows have a dispatch outcome"
