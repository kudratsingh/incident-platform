"""Unit tests for the CQRS read-model projector — Redis faked in-process.

The fake is a real (tiny) implementation of the handful of commands the
projector uses, not a call recorder: these tests are about what the
projection *contains* after a sequence of events — that a terminal job is
not demoted by a redelivery, that a key stays bounded however many jobs
flow through it, that the counts survive the trim — and a mock asserting
that ZADD was called cannot see any of that.
"""

import fnmatch
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.workers import read_model
from app.workers.read_model import (
    READ_MODEL_TTL_SECONDS,
    ReadModelProjector,
    rebuild_read_model,
)
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeRedis:
    """The commands read_model.py uses, over plain dicts."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}

    # -- type dispatch ---------------------------------------------------

    def _wrongtype(self, key: str, wanted: str) -> None:
        holders = {"zset": self.zsets, "string": self.strings, "set": self.sets}
        for kind, store in holders.items():
            if kind != wanted and key in store:
                raise ResponseError(
                    "WRONGTYPE Operation against a key holding the wrong kind of value"
                )

    # -- sorted sets -----------------------------------------------------

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._wrongtype(key, "zset")
        z = self.zsets.setdefault(key, {})
        added = sum(1 for m in mapping if m not in z)
        z.update(mapping)
        return added

    async def zrem(self, key: str, member: str) -> int:
        self._wrongtype(key, "zset")
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def zcard(self, key: str) -> int:
        self._wrongtype(key, "zset")
        return len(self.zsets.get(key, {}))

    async def zscore(self, key: str, member: str) -> float | None:
        self._wrongtype(key, "zset")
        return self.zsets.get(key, {}).get(member)

    async def zremrangebyrank(self, key: str, start: int, stop: int) -> int:
        self._wrongtype(key, "zset")
        z = self.zsets.get(key, {})
        ordered = sorted(z.items(), key=lambda kv: (kv[1], kv[0]))
        doomed = ordered[start : stop + 1]
        for member, _score in doomed:
            del z[member]
        return len(doomed)

    def members(self, key: str) -> set[str]:
        return set(self.zsets.get(key, {}))

    # -- strings / sets --------------------------------------------------

    async def get(self, key: str) -> str | None:
        self._wrongtype(key, "string")
        return self.strings.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.strings[key] = str(value)
        if ex is not None:
            self.ttls[key] = ex

    async def incrby(self, key: str, amount: int) -> int:
        self._wrongtype(key, "string")
        new = int(self.strings.get(key, "0")) + amount
        self.strings[key] = str(new)
        return new

    async def sadd(self, key: str, *members: str) -> int:
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    async def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def delete(self, *keys: str) -> int:
        gone = 0
        for key in keys:
            for store in (self.zsets, self.strings, self.sets):
                if key in store:
                    del store[key]
                    gone += 1
            self.ttls.pop(key, None)
        return gone

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 10
    ) -> tuple[int, list[str]]:
        keys = [
            k
            for k in (*self.zsets, *self.strings, *self.sets)
            if fnmatch.fnmatch(k, match)
        ]
        return 0, keys


def _ids() -> tuple[str, str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def _event(name: str, tenant_id: str, job_id: str, user_id: str, **extra: Any) -> dict:
    return {
        "event": name,
        "tenant_id": tenant_id,
        "job_id": job_id,
        "user_id": user_id,
        **extra,
    }


# ---------------------------------------------------------------------------
# Projection semantics
# ---------------------------------------------------------------------------


async def test_completed_event_moves_job_into_completed_view() -> None:
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()

    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value=_event(
            "job.completed", tenant_id, job_id, user_id, job_type="csv_upload"
        ),
    )

    assert redis.members(f"jobs:tenant:{tenant_id}:status:completed") == {job_id}
    assert redis.members(f"jobs:user:{user_id}:status:completed") == {job_id}
    for status in ("running", "failed", "dead_letter"):
        assert redis.members(f"jobs:tenant:{tenant_id}:status:{status}") == set()


async def test_failed_event_with_dlq_flag_lands_in_dead_letter() -> None:
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()

    await projector.handle_message(
        topic="job.dlq",
        key=f"{tenant_id}:{user_id}",
        value=_event(
            "job.failed", tenant_id, job_id, user_id, dead_lettered=True
        ),
    )

    assert redis.members(f"jobs:tenant:{tenant_id}:status:dead_letter") == {job_id}
    assert redis.members(f"jobs:user:{user_id}:status:dead_letter") == {job_id}


async def test_failed_event_non_dlq_lands_in_failed() -> None:
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()

    await projector.handle_message(
        topic="job.failed",
        key=f"{tenant_id}:{user_id}",
        value=_event(
            "job.failed", tenant_id, job_id, user_id, dead_lettered=False
        ),
    )

    assert redis.members(f"jobs:tenant:{tenant_id}:status:failed") == {job_id}


async def test_idempotent_under_redelivery() -> None:
    """Reprocessing the same message twice yields the same membership — no
    double-counting, because re-adding an existing member is a no-op."""
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()
    msg = _event("job.completed", tenant_id, job_id, user_id, job_type="csv_upload")

    await projector.handle_message(topic="job.completed", key="u", value=msg)
    await projector.handle_message(topic="job.completed", key="u", value=msg)

    assert await read_model.read_global_stats(redis, tenant_id) == {  # type: ignore[arg-type]
        "running": 0,
        "completed": 1,
        "failed": 0,
        "dead_letter": 0,
    }


async def test_progress_after_terminal_does_not_demote() -> None:
    """A job.progress consumed after that job's job.completed (cross-topic
    reordering or redelivery) must NOT drag the job back into running."""
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()

    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value=_event("job.completed", tenant_id, job_id, user_id),
    )
    await projector.handle_message(
        topic="job.progress",
        key=f"{tenant_id}:{user_id}",
        value=_event("job.progress", tenant_id, job_id, user_id, progress=50),
    )

    assert redis.members(f"jobs:tenant:{tenant_id}:status:running") == set()
    assert redis.members(f"jobs:tenant:{tenant_id}:status:completed") == {job_id}


async def test_terminal_event_still_applies_after_terminal() -> None:
    """Terminal→terminal is exempt from the guard: after a DLQ replay the
    job's job.completed MUST still project (a naive 'never touch terminal
    jobs' guard would freeze replayed jobs in dead_letter forever)."""
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()

    await projector.handle_message(
        topic="job.dlq",
        key=f"{tenant_id}:{user_id}",
        value=_event("job.failed", tenant_id, job_id, user_id, dead_lettered=True),
    )
    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value=_event("job.completed", tenant_id, job_id, user_id),
    )

    assert redis.members(f"jobs:tenant:{tenant_id}:status:completed") == {job_id}
    assert redis.members(f"jobs:tenant:{tenant_id}:status:dead_letter") == set()


async def test_unknown_event_is_ignored() -> None:
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    await projector.handle_message(
        topic="job.submitted",
        key="u",
        value=_event("job.submitted", *_ids()),  # not mapped — just queued
    )
    assert redis.zsets == {}


async def test_missing_tenant_id_skips_projection() -> None:
    """Events without tenant_id (legacy or malformed) must not silently land
    in a global key — they used to before Phase 12. Now they're dropped."""
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    await projector.handle_message(
        topic="job.completed",
        key="u",
        value={
            "event": "job.completed",
            "job_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
        },
    )
    assert redis.zsets == {}


async def test_read_global_stats_returns_counts_per_tenant() -> None:
    redis = _FakeRedis()
    tenant_id = str(uuid.uuid4())
    for status, n in (("running", 3), ("completed", 7), ("failed", 1), ("dead_letter", 2)):
        for _ in range(n):
            await redis.zadd(
                f"jobs:tenant:{tenant_id}:status:{status}", {str(uuid.uuid4()): 1.0}
            )

    stats = await read_model.read_global_stats(redis, tenant_id)  # type: ignore[arg-type]

    assert stats == {"running": 3, "completed": 7, "failed": 1, "dead_letter": 2}


async def test_read_user_stats_uses_user_keys() -> None:
    redis = _FakeRedis()
    user_id = str(uuid.uuid4())
    await redis.zadd(f"jobs:user:{user_id}:status:running", {str(uuid.uuid4()): 1.0})

    assert (await read_model.read_user_stats(redis, user_id))["running"] == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Bounded growth (WO-R2-56)
# ---------------------------------------------------------------------------


async def test_status_view_stays_bounded_across_many_terminal_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this closes: every terminal job_id retained forever, on a
    Redis whose production policy cannot evict a key that has no TTL."""
    monkeypatch.setattr(read_model, "READ_MODEL_WINDOW", 10)
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    _job, user_id, tenant_id = _ids()

    for _ in range(200):
        await projector.handle_message(
            topic="job.completed",
            key=f"{tenant_id}:{user_id}",
            value=_event("job.completed", tenant_id, str(uuid.uuid4()), user_id),
        )

    key = f"jobs:tenant:{tenant_id}:status:completed"
    assert len(redis.members(key)) == 10
    # Bounded, but not lying about it: the count is still every job.
    stats = await read_model.read_global_stats(redis, tenant_id)  # type: ignore[arg-type]
    assert stats["completed"] == 200
    assert redis.strings[f"{key}:evicted"] == "190"


async def test_trim_evicts_the_oldest_projection_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recency ordering is why this is a ZSET: the ids that can still receive
    an event are exactly the ones the trim must not drop."""
    monkeypatch.setattr(read_model, "READ_MODEL_WINDOW", 3)
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    _job, user_id, tenant_id = _ids()

    job_ids = [str(uuid.uuid4()) for _ in range(5)]
    for job_id in job_ids:
        await projector.handle_message(
            topic="job.completed",
            key=f"{tenant_id}:{user_id}",
            value=_event("job.completed", tenant_id, job_id, user_id),
        )

    retained = redis.members(f"jobs:tenant:{tenant_id}:status:completed")
    assert retained == set(job_ids[-3:])


async def test_every_projected_key_carries_a_ttl() -> None:
    """The reaper. A key with no TTL cannot be evicted under `volatile-*` and
    is never reclaimed under `noeviction` — which is what production runs."""
    redis = _FakeRedis()
    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    job_id, user_id, tenant_id = _ids()

    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value=_event("job.completed", tenant_id, job_id, user_id),
    )

    assert redis.ttls[f"jobs:tenant:{tenant_id}:status:completed"] == (
        READ_MODEL_TTL_SECONDS
    )
    assert redis.ttls[f"jobs:user:{user_id}:status:completed"] == READ_MODEL_TTL_SECONDS


async def test_legacy_set_key_is_migrated_rather_than_raising() -> None:
    """A deployment that ran the SET-based projector leaves keys of the wrong
    type behind; every ZSET command against one is a WRONGTYPE. Raising here
    means the consumer never commits an offset and the projection stalls."""
    redis = _FakeRedis()
    job_id, user_id, tenant_id = _ids()
    key = f"jobs:tenant:{tenant_id}:status:completed"
    await redis.sadd(key, "some-older-job-id")

    projector = ReadModelProjector(redis)  # type: ignore[arg-type]
    await projector.handle_message(
        topic="job.completed",
        key=f"{tenant_id}:{user_id}",
        value=_event("job.completed", tenant_id, job_id, user_id),
    )

    assert redis.members(key) == {job_id}
    assert key not in redis.sets


# ---------------------------------------------------------------------------
# Rebuild from Postgres (WO-R2-56)
# ---------------------------------------------------------------------------


async def _job_row(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, status: str, age: int
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        user_id=user_id,
        type=JobType.CSV_UPLOAD,
        status=status,
        payload={"row_count": 1},
        updated_at=datetime.now(UTC) - timedelta(minutes=age),
    )
    session.add(job)
    await session.flush()
    return job


async def test_rebuild_restores_a_wiped_read_model(
    db_session: AsyncSession, test_user: Any
) -> None:
    """`saturate_redis` (or a restart, or an eviction) can empty the
    projection, and nothing rebuilds it: an id only moves when an event names
    it, and no further event is coming for a finished job."""
    redis = _FakeRedis()
    tenant_id, user_id = test_user.tenant_id, test_user.id
    completed = [
        await _job_row(db_session, tenant_id, user_id, JobStatus.COMPLETED, age)
        for age in range(3)
    ]
    dead = await _job_row(db_session, tenant_id, user_id, JobStatus.DEAD_LETTER, 5)
    # Statuses outside the projection stay outside it.
    await _job_row(db_session, tenant_id, user_id, JobStatus.PENDING, 1)

    summary = await rebuild_read_model(db_session, redis)  # type: ignore[arg-type]

    # Four projected rows, each written into both its tenant and its user key.
    assert summary["members"] == 8
    assert redis.members(f"jobs:tenant:{tenant_id}:status:completed") == {
        str(j.id) for j in completed
    }
    assert redis.members(f"jobs:tenant:{tenant_id}:status:dead_letter") == {
        str(dead.id)
    }
    assert redis.members(f"jobs:user:{user_id}:status:completed") == {
        str(j.id) for j in completed
    }
    stats = await read_model.read_global_stats(redis, str(tenant_id))  # type: ignore[arg-type]
    assert stats == {"running": 0, "completed": 3, "failed": 0, "dead_letter": 1}


async def test_rebuild_drops_ids_the_table_no_longer_backs(
    db_session: AsyncSession, test_user: Any
) -> None:
    """Rebuild is a recompute, not a merge: a projection holding ids the table
    doesn't (a chaos run's deleted jobs) is exactly what it has to correct."""
    redis = _FakeRedis()
    tenant_id = test_user.tenant_id
    key = f"jobs:tenant:{tenant_id}:status:completed"
    await redis.zadd(key, {"ghost-job": 1.0})
    await redis.set(f"{key}:evicted", 99)
    await _job_row(db_session, tenant_id, test_user.id, JobStatus.COMPLETED, 1)

    await rebuild_read_model(db_session, redis)  # type: ignore[arg-type]

    assert "ghost-job" not in redis.members(key)
    assert (await read_model.read_global_stats(redis, str(tenant_id)))["completed"] == 1  # type: ignore[arg-type]


async def test_rebuild_windows_membership_and_credits_the_remainder(
    db_session: AsyncSession, test_user: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rebuilt key is bounded the same way a grown one is, and its count is
    still whole — otherwise the rebuild would reintroduce the unbounded key it
    exists to repair."""
    monkeypatch.setattr(read_model, "READ_MODEL_WINDOW", 2)
    redis = _FakeRedis()
    tenant_id = test_user.tenant_id
    jobs = [
        await _job_row(db_session, tenant_id, test_user.id, JobStatus.COMPLETED, age)
        for age in range(5)
    ]

    await rebuild_read_model(db_session, redis)  # type: ignore[arg-type]

    key = f"jobs:tenant:{tenant_id}:status:completed"
    # age=0 and age=1 are the most recently updated of the five.
    assert redis.members(key) == {str(jobs[0].id), str(jobs[1].id)}
    assert (await read_model.read_global_stats(redis, str(tenant_id)))["completed"] == 5  # type: ignore[arg-type]


async def test_rebuild_scoped_to_one_tenant_leaves_siblings_alone(
    db_session: AsyncSession, test_user: Any
) -> None:
    redis = _FakeRedis()
    other_tenant = uuid.uuid4()
    other_key = f"jobs:tenant:{other_tenant}:status:completed"
    await redis.zadd(other_key, {"sibling-job": 1.0})
    await _job_row(db_session, test_user.tenant_id, test_user.id, JobStatus.COMPLETED, 1)

    await rebuild_read_model(db_session, redis, tenant_id=test_user.tenant_id)  # type: ignore[arg-type]

    assert redis.members(other_key) == {"sibling-job"}
