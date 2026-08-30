"""`replay_job` promises "a fresh lifecycle" — these pin the three ways
it did not deliver one (R2-23).

Real rows on a real (SQLite in-memory) engine, for the same reason
`test_terminal_event_single_write.py` is: two of the three findings are
invisible under mocks. `test_job_service.py` already asserts
``previous_status == DEAD_LETTER`` and has always passed, because its
`job_repo` is an `AsyncMock` — the mocked `update_status` never writes
the row, so the identity-mapped `job` the service holds keeps its
pre-replay status and the stale read looks correct. Only a real
repository flips it. Same for the remediation hint: a mock records the
`extra` dict but no column ever changes.

The three:

  1. the `remediation_hint` survived the replay, so one dead-letter
     episode's category (including an agent's `mark_dlq_permanent`
     fence) governed every later, unrelated dead-letter of that job;
  2. the `job.replayed` audit row read `job.status` *after*
     `update_status` had already flipped it, recording "pending" as the
     status every replay was replayed *from*;
  3. the cache was invalidated mid-transaction, so a reader that missed
     between the delete and the commit read the pre-replay row from
     Postgres and put it straight back.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.enums import JobStatus, JobType, RemediationHint, UserRole
from app.models.job import Job
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.job import JobRepository
from app.repositories.outbox import OutboxRepository
from app.services.job import JobService
from app.utils.cache import JobCache
from app.utils.post_commit import run_post_commit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_USER_ID = uuid.UUID("b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e")


# --------------------------------------------------------------------------- #
# Redis stand-ins                                                              #
# --------------------------------------------------------------------------- #


class _InMemoryRedis:
    """Enough real Redis semantics to make the cache race observable.

    `set(..., nx=True)` genuinely refuses to overwrite — that refusal is
    the mechanism under test, so a stub that ignored `nx` would let the
    race pass.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ops: list[str] = []

    async def get(self, key: str) -> str | None:
        self.ops.append(f"get {key}")
        return self.store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        self.ops.append(f"set {key}")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        self.ops.append(f"delete {keys[0] if keys else ''}")
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    def __getattr__(self, name: str) -> Any:
        async def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        return _noop


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[  # type: ignore[return]
    async_sessionmaker[AsyncSession], None
]:
    """Module-local engine: these tests commit, and committed rows must
    not leak into the shared session-scoped engine other suites roll
    back against."""
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
                email="replay@test.example",
                hashed_password="x",
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        await session.commit()

    yield factory
    await engine.dispose()


async def _insert_dead_letter(
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
                error_message="boom",
            )
        )
        await session.commit()
    return job_id


def _service(session: AsyncSession, redis: Any) -> JobService:
    return JobService(
        job_repo=JobRepository(session),
        audit_repo=AuditRepository(session),
        outbox_repo=OutboxRepository(session),
        redis=redis,
    )


async def _replay(
    factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    redis: Any,
) -> None:
    """Replay the way every caller does: inside one transaction, with the
    post-commit queue drained once the commit has landed."""
    async with factory() as session:
        async with session.begin():
            await _service(session, redis).replay_job(
                job_id=job_id,
                tenant_id=DEFAULT_TENANT_ID,
                requesting_user_id=_USER_ID,
            )
        await run_post_commit(session)


async def _reload(
    factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> Job:
    async with factory() as session:
        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()
        return job


# --------------------------------------------------------------------------- #
# Finding 1 — the remediation hint outlived its episode                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_replay_clears_the_remediation_hint(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`replay_job` already clears `dead_lettered_by` and calls that "a
    fresh lifecycle". `remediation_hint` is the same kind of value —
    episode-scoped classification — and no production code path could
    ever clear it, so a `human_required` fence set for one failure
    silently governed the routing of every later, unrelated dead-letter
    of that job.

    RED before: the hint is still `human_required` after the replay.
    """
    job_id = await _insert_dead_letter(
        session_factory, remediation_hint=RemediationHint.HUMAN_REQUIRED.value
    )

    await _replay(session_factory, job_id, _InMemoryRedis())

    job = await _reload(session_factory, job_id)
    assert job.status == JobStatus.PENDING
    assert job.remediation_hint is None, (
        "a replay is a fresh lifecycle — the previous episode's category "
        "must not govern the next one"
    )
    # The paired half of the same invariant, already shipped (F2-16).
    assert job.dead_lettered_by is None


@pytest.mark.asyncio
async def test_a_replayed_job_is_visible_to_the_blind_bulk_replay_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fence must be per-episode in both directions (R2-22 + R2-23).

    `replay_dlq_messages` now excludes `human_required` at the query
    level. If the hint were sticky, one `mark_dlq_permanent` would
    permanently remove a job from every future bulk remediation — a
    fence that can be raised and never lowered. Asserted through the
    exclusion the tool actually issues.
    """
    job_id = await _insert_dead_letter(
        session_factory, remediation_hint=RemediationHint.HUMAN_REQUIRED.value
    )
    await _replay(session_factory, job_id, _InMemoryRedis())

    # Back to the DLQ on a later, unrelated failure.
    async with session_factory() as session:
        async with session.begin():
            job = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            job.status = JobStatus.DEAD_LETTER.value

    async with session_factory() as session:
        jobs, _ = await JobRepository(session).list_jobs(
            tenant_id=DEFAULT_TENANT_ID,
            status=JobStatus.DEAD_LETTER.value,
            exclude_remediation_hints=(RemediationHint.HUMAN_REQUIRED.value,),
        )
    assert [j.id for j in jobs] == [job_id], (
        "the new episode carries no category, so the blind batch must "
        "consider it again"
    )


# --------------------------------------------------------------------------- #
# Finding 2 — previous_status was read after the flip                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_replay_audit_records_the_status_it_was_replayed_from(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`previous_status` is an operator/compliance value that is
    unrecoverable once the row flips — nothing else in the system
    remembers what the job was replayed *from*.

    RED before: `job.status` was read for the audit row after
    `update_status` had already written PENDING through the same
    identity-mapped object, so every replay recorded "pending".
    """
    job_id = await _insert_dead_letter(session_factory)

    await _replay(session_factory, job_id, _InMemoryRedis())

    async with session_factory() as session:
        row = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "job.replayed")
            )
        ).scalar_one()
    assert row.extra_data is not None
    assert row.extra_data["previous_status"] == JobStatus.DEAD_LETTER, (
        "the audit trail must record the status replayed from, not the "
        "status replayed to"
    )
    assert row.extra_data["previous_retry_count"] == 3


@pytest.mark.asyncio
async def test_replay_of_a_failed_job_records_failed_not_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The FAILED branch of the same guard — `replay_job` accepts both
    FAILED and DEAD_LETTER, and the audit row flattened both to
    "pending", which is exactly what made the bug invisible: the value
    was constant, so it never looked wrong for any *particular* job."""
    job_id = await _insert_dead_letter(session_factory)
    async with session_factory() as session:
        async with session.begin():
            job = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            job.status = JobStatus.FAILED.value

    await _replay(session_factory, job_id, _InMemoryRedis())

    async with session_factory() as session:
        row = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "job.replayed")
            )
        ).scalar_one()
    assert row.extra_data is not None
    assert row.extra_data["previous_status"] == JobStatus.FAILED


# --------------------------------------------------------------------------- #
# Finding 3 — the cache was invalidated inside the transaction                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_invalidation_happens_after_the_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Invalidating mid-transaction advertises a change nobody else can
    see yet: the row is still `dead_letter` for every other connection,
    so a reader that misses on the hole we just punched reads the
    pre-replay row and caches it.

    RED before: the only cache op recorded happens while the
    transaction is still open.
    """
    job_id = await _insert_dead_letter(session_factory)
    redis = _InMemoryRedis()

    async with session_factory() as session:
        async with session.begin():
            await _service(session, redis).replay_job(
                job_id=job_id,
                tenant_id=DEFAULT_TENANT_ID,
                requesting_user_id=_USER_ID,
            )
            assert redis.ops == [], (
                "nothing may touch the cache before the commit — until "
                "then the cached row is still the truth"
            )
        redis.ops.append("COMMIT")
        await run_post_commit(session)

    assert "COMMIT" in redis.ops
    after_commit = redis.ops[redis.ops.index("COMMIT") + 1 :]
    assert any(
        op.startswith(f"set {JobCache._key(job_id, DEFAULT_TENANT_ID)}")
        for op in after_commit
    ), f"expected the invalidation after COMMIT, got {redis.ops}"


@pytest.mark.asyncio
async def test_a_racing_reader_cannot_repopulate_the_pre_replay_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The race moving the delete alone does not close.

    A reader that loaded the job from Postgres *before* the replay
    committed still holds the pre-replay row. Whenever its `JobCache.set`
    lands — including after the invalidation — it must not be able to
    put that row back. The invalidation therefore leaves a short
    no-cache tombstone in the slot rather than an empty hole.

    RED before: `JobCache.set` overwrites unconditionally, so the stale
    `dead_letter` row is served for a full TTL after the replay.
    """
    job_id = await _insert_dead_letter(session_factory)
    redis = _InMemoryRedis()

    # The concurrent reader's snapshot, taken before the replay commits.
    stale = {"id": str(job_id), "status": JobStatus.DEAD_LETTER.value}

    await _replay(session_factory, job_id, redis)

    # ...and its write lands after the invalidation.
    await JobCache.set(redis, job_id, DEFAULT_TENANT_ID, stale)

    assert await JobCache.get(redis, job_id, DEFAULT_TENANT_ID) is None, (
        "a pre-replay snapshot must not be cacheable after the replay "
        "committed"
    )
    # And the raw slot holds the tombstone, not the stale row, so nothing
    # downstream can dig the old status back out of Redis either.
    raw = redis.store[JobCache._key(job_id, DEFAULT_TENANT_ID)]
    assert JobStatus.DEAD_LETTER.value not in raw
    assert json.loads(json.dumps(raw))  # non-empty marker


@pytest.mark.asyncio
async def test_a_rolled_back_replay_never_touches_the_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The mirror of the ordering guarantee: no commit, no invalidation.

    The pre-fix code deleted the key from inside the transaction, so a
    replay that rolled back still evicted a perfectly valid entry —
    cheap, but it is the same confusion about when a write is real.
    """
    job_id = await _insert_dead_letter(session_factory)
    redis = _InMemoryRedis()

    with pytest.raises(RuntimeError):
        async with session_factory() as session:
            async with session.begin():
                await _service(session, redis).replay_job(
                    job_id=job_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    requesting_user_id=_USER_ID,
                )
                raise RuntimeError("caller blew up after the replay")

    assert redis.ops == []
    assert (await _reload(session_factory, job_id)).status == (
        JobStatus.DEAD_LETTER
    )
