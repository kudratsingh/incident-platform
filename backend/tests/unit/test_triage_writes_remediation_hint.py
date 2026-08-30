"""LLM triage has to actually set `jobs.remediation_hint` (R2-24).

Three places name triage as the setter of that column — the
`RemediationHint` enum docstring, the model's column comment, and the DLQ
tool descriptions the agent reads — and none of them was true. The
consumer wrote a `job_triages` row and stopped, so no organically
dead-lettered job ever carried a remediation category. Every hint in the
system came from the eval seed script, the chaos hooks, or an agent's
`mark_dlq_permanent`, which means the agent's categorised-replay path was
only ever exercised against fixtures.

Real rows on a real (SQLite in-memory) engine: the claim is what lands in
the `jobs` row next to the `job_triages` row, in one transaction, and a
mocked session proves neither.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from app.models.base import Base
from app.models.enums import JobStatus, JobType, RemediationHint, UserRole
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.triage import JobTriage
from app.models.user import User
from app.repositories.job import JobRepository
from app.services.triage import TriageAnalysis, remediation_hint_for
from app.workers.triage_consumer import LlmTriageConsumer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_USER_ID = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


def _analysis(**overrides: Any) -> TriageAnalysis:
    base: dict[str, Any] = {
        "root_cause_category": "transient",
        "summary": "upstream timed out",
        "suggested_fix": "retry once the dependency is back",
        "is_retryable": True,
        "confidence": 0.9,
    }
    base.update(overrides)
    return TriageAnalysis(**base)


# --------------------------------------------------------------------------- #
# The mapping                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("category", "is_retryable", "expected"),
    [
        # Not replayable as-is → the category that stops automation from
        # trying. This is the same judgement `is_retryable` already encodes,
        # so it is read first and the root cause only refines the retryable
        # half.
        ("validation_error", False, RemediationHint.HUMAN_REQUIRED.value),
        ("configuration", False, RemediationHint.HUMAN_REQUIRED.value),
        ("data_corruption", False, RemediationHint.HUMAN_REQUIRED.value),
        ("external_api_failure", False, RemediationHint.HUMAN_REQUIRED.value),
        # Retryable, but only once something else recovers.
        ("external_api_failure", True, RemediationHint.WAIT_AND_REPLAY.value),
        ("infrastructure", True, RemediationHint.WAIT_AND_REPLAY.value),
        # Retryable with nothing to wait for.
        ("transient", True, RemediationHint.REPLAY_SAFE.value),
        ("validation_error", True, RemediationHint.REPLAY_SAFE.value),
    ],
)
def test_analysis_maps_to_a_remediation_category(
    category: str, is_retryable: bool, expected: str
) -> None:
    analysis = _analysis(
        root_cause_category=category, is_retryable=is_retryable
    )
    assert remediation_hint_for(analysis) == expected


def test_an_unknown_root_cause_gets_no_category() -> None:
    """`unknown` means "too generic to classify" by the taxonomy's own
    definition. Writing any category off it would be inventing one."""
    assert remediation_hint_for(_analysis(root_cause_category="unknown")) is None


@pytest.mark.parametrize("confidence", [0.0, 0.2, 0.49])
def test_a_low_confidence_analysis_gets_no_category(confidence: float) -> None:
    """NULL already means "not categorised, treat as unknown, not
    replay-safe" — the honest output for a guess. Both directions are
    gated, not just the replayable ones: a low-confidence `replay_safe`
    feeds `replay_dlq_by_category` a job that re-fails, and a
    low-confidence `human_required` over-claims a persistent bug and
    escalates a job nobody needed to look at."""
    for retryable in (True, False):
        analysis = _analysis(confidence=confidence, is_retryable=retryable)
        assert remediation_hint_for(analysis) is None


def test_every_mapped_value_is_a_real_enum_member() -> None:
    """The column is a plain String with no CHECK constraint, so a typo
    here would persist happily and only surface as a category no tool
    matches."""
    seen = {
        remediation_hint_for(
            _analysis(root_cause_category=cat, is_retryable=retryable)
        )
        for cat in (
            "external_api_failure",
            "validation_error",
            "infrastructure",
            "data_corruption",
            "configuration",
            "transient",
            "unknown",
        )
        for retryable in (True, False)
    }
    assert seen - {None} <= {h.value for h in RemediationHint}


# --------------------------------------------------------------------------- #
# The write                                                                    #
# --------------------------------------------------------------------------- #


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
                email="triage@test.example",
                hashed_password="x",
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        await session.commit()

    yield factory
    await engine.dispose()


async def _insert_dlq_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    remediation_hint: str | None = None,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Job(
                id=job_id,
                tenant_id=DEFAULT_TENANT_ID,
                user_id=_USER_ID,
                type=JobType.CSV_UPLOAD.value,
                status=JobStatus.DEAD_LETTER.value,
                priority=0,
                payload={"file": "x.csv"},
                retry_count=3,
                max_retries=3,
                remediation_hint=remediation_hint,
                error_message="timeout calling upstream",
            )
        )
        await session.commit()
    return job_id


def _dlq_event(job_id: uuid.UUID) -> dict[str, Any]:
    return {
        "event": "job.failed",
        "dead_lettered": True,
        "tenant_id": str(DEFAULT_TENANT_ID),
        "job_id": str(job_id),
        "user_id": str(_USER_ID),
        "job_type": "csv_upload",
        "error": "timeout calling upstream",
        "message": "Job exhausted after 3 attempts",
        "retry_count": 3,
        "max_retries": 3,
        "payload": {"file": "x.csv"},
        "trace_id": "trace-abc",
    }


async def _run_triage(
    factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    analysis: TriageAnalysis,
) -> None:
    consumer = LlmTriageConsumer(factory)
    with patch(
        "app.services.triage.is_enabled", new=lambda: True
    ), patch(
        "app.services.triage.triage_failure",
        new=AsyncMock(return_value=(analysis, {}, "claude-opus-4-7")),
    ):
        await consumer.handle_message(
            "job.dlq", None, _dlq_event(job_id)
        )


async def _hint(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> str | None:
    async with factory() as session:
        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()
        return job.remediation_hint


@pytest.mark.asyncio
async def test_triage_writes_the_mapped_category_onto_the_job_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """RED before: the consumer wrote the `job_triages` row and nothing
    else, so the column stayed NULL for every organically dead-lettered
    job in the platform's history."""
    job_id = await _insert_dlq_job(session_factory)

    await _run_triage(
        session_factory,
        job_id,
        _analysis(root_cause_category="transient", is_retryable=True),
    )

    assert await _hint(session_factory, job_id) == RemediationHint.REPLAY_SAFE

    # And the triage row is still written — this is an addition to that
    # transaction, not a replacement for it.
    async with session_factory() as session:
        triage = (
            await session.execute(
                select(JobTriage).where(JobTriage.job_id == job_id)
            )
        ).scalar_one()
    assert triage.root_cause_category == "transient"


@pytest.mark.asyncio
async def test_a_non_retryable_analysis_fences_the_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id = await _insert_dlq_job(session_factory)

    await _run_triage(
        session_factory,
        job_id,
        _analysis(root_cause_category="validation_error", is_retryable=False),
    )

    assert await _hint(session_factory, job_id) == (
        RemediationHint.HUMAN_REQUIRED
    )


@pytest.mark.asyncio
async def test_triage_does_not_overwrite_a_human_set_fence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`mark_dlq_permanent` is a deliberate human (or agent) judgement
    that this job must not be replayed automatically. A re-triage that
    downgraded it to `replay_safe` would silently lower a fence somebody
    raised on purpose — and post-R2-22 that fence is what keeps the job
    out of the blind bulk replay."""
    job_id = await _insert_dlq_job(
        session_factory, remediation_hint=RemediationHint.HUMAN_REQUIRED.value
    )

    await _run_triage(
        session_factory,
        job_id,
        _analysis(root_cause_category="transient", is_retryable=True),
    )

    assert await _hint(session_factory, job_id) == (
        RemediationHint.HUMAN_REQUIRED
    )


@pytest.mark.asyncio
async def test_triage_does_not_overwrite_any_existing_category(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The guard is "only fill a NULL", not "only protect
    human_required". A seeded or chaos-written category is equally
    somebody else's statement about this episode, and triage arriving
    late should not relabel it."""
    job_id = await _insert_dlq_job(
        session_factory, remediation_hint=RemediationHint.WAIT_AND_REPLAY.value
    )

    await _run_triage(
        session_factory,
        job_id,
        _analysis(root_cause_category="validation_error", is_retryable=False),
    )

    assert await _hint(session_factory, job_id) == (
        RemediationHint.WAIT_AND_REPLAY
    )


@pytest.mark.asyncio
async def test_an_unclassifiable_analysis_leaves_the_column_null(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """NULL is a real answer, not a failure to write one — and the tools
    read it as "unknown, not replay-safe"."""
    job_id = await _insert_dlq_job(session_factory)

    await _run_triage(
        session_factory,
        job_id,
        _analysis(root_cause_category="unknown", confidence=0.95),
    )

    assert await _hint(session_factory, job_id) is None
    # The triage row is still there — the admin gets the summary and the
    # suggested fix even when the coarse category is not safe to assert.
    async with session_factory() as session:
        assert (
            await session.execute(
                select(JobTriage).where(JobTriage.job_id == job_id)
            )
        ).scalar_one() is not None


@pytest.mark.asyncio
async def test_triage_writes_nothing_when_the_job_belongs_to_another_tenant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The conditional UPDATE is tenant-scoped like every other write.
    The event carries the tenant, but a malformed or replayed event must
    not be able to relabel a row outside it."""
    job_id = await _insert_dlq_job(session_factory)

    async with session_factory() as session:
        written = await JobRepository(session).set_remediation_hint_if_unset(
            job_id=job_id,
            tenant_id=uuid.uuid4(),  # not this job's tenant
            hint=RemediationHint.REPLAY_SAFE.value,
        )
        await session.commit()

    assert written is False
    assert await _hint(session_factory, job_id) is None


# --------------------------------------------------------------------------- #
# The advertised contract                                                      #
# --------------------------------------------------------------------------- #


def test_the_dlq_tool_description_admits_triage_is_off_by_default() -> None:
    """The other half of the finding. Three places named triage as the
    setter of this column; the code half is fixed above, but triage is
    still gated behind `LLM_TRIAGE_ENABLED` and that defaults to false
    (ADR 0005). An agent told the column is populated, looking at a DLQ
    where every hint is null, has no way to tell "nothing categorised
    these" from "these are uncategorisable" — and the difference decides
    whether it should be reaching for a categorised replay at all.
    """
    # Importing the package is what fires the `@tool` decorators — the
    # registry is populated by import side-effect, so asking it anything
    # without this passes only when some earlier test happened to import
    # the MCP server first.
    import app.mcp.tools  # noqa: F401
    from app.config import get_settings
    from app.mcp import registry

    assert get_settings().llm_triage_enabled is False, (
        "if triage ever ships on by default, this description needs "
        "rewriting rather than deleting"
    )

    spec = registry.get_tool("list_dlq_messages")
    assert spec is not None
    description = spec.description.lower()
    assert "off by default" in description
    assert "null" in description
    assert "not replay-safe" in description


# --------------------------------------------------------------------------- #
# What it unlocks downstream                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_triaged_job_reaches_the_categorised_replay_path(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The point of the finding. `replay_dlq_by_category` and
    `list_dlq_messages(remediation_hint=…)` both filter on this column,
    so before triage wrote it those paths could only ever match seeded
    fixtures — the agent's categorised-replay branch was never exercised
    against a real production dead-letter.
    """
    job_id = await _insert_dlq_job(session_factory)
    await _run_triage(
        session_factory,
        job_id,
        _analysis(root_cause_category="transient", is_retryable=True),
    )

    async with session_factory() as session:
        matched, _ = await JobRepository(session).list_jobs(
            tenant_id=DEFAULT_TENANT_ID,
            status=JobStatus.DEAD_LETTER.value,
            remediation_hint=RemediationHint.REPLAY_SAFE.value,
        )
    assert [j.id for j in matched] == [job_id]


@pytest.mark.asyncio
async def test_a_triage_fenced_job_is_excluded_from_the_blind_bulk_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The other half of the composition, and the one with teeth: R2-22
    made `replay_dlq_messages` skip `human_required`, and triage
    becoming a real writer of that category means the fence now covers
    organically dead-lettered jobs rather than only fixtures and
    `mark_dlq_permanent` calls."""
    fenced = await _insert_dlq_job(session_factory)
    plain = await _insert_dlq_job(session_factory)

    await _run_triage(
        session_factory,
        fenced,
        _analysis(root_cause_category="validation_error", is_retryable=False),
    )

    async with session_factory() as session:
        replayable, _ = await JobRepository(session).list_jobs(
            tenant_id=DEFAULT_TENANT_ID,
            status=JobStatus.DEAD_LETTER.value,
            exclude_remediation_hints=(RemediationHint.HUMAN_REQUIRED.value,),
        )
    ids = [j.id for j in replayable]
    assert plain in ids
    assert fenced not in ids
