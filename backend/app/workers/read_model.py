"""
CQRS read-model projector: denormalized job-status views in Redis so admin queries never
aggregate the `jobs` table. Keys are `jobs:tenant:{tenant_id}:status:{status}` and
`jobs:user:{user_id}:status:{status}`, each with a sibling `:evicted` counter, so a status count
is `ZCARD + evicted`. Bounded by construction (WO-R2-56): ZSETs scored by projection time,
trimmed oldest-first to `READ_MODEL_WINDOW`, every key carrying `READ_MODEL_TTL_SECONDS`.
"""

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.models.job import Job
from app.workers.kafka_consumer import BaseKafkaConsumer
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

# Statuses we track in the projection. Strings rather than the enum so the
# projector doesn't need to import the SQLAlchemy enum module.
_TRACKED_STATUSES = ("running", "completed", "failed", "dead_letter", "cancelled")

# Once a job sits in one of these views only another terminal event may move it. 'failed' is NOT
# terminal — the retry cycle moves failed jobs back to running. 'cancelled' joined at WO-R2-113.
_TERMINAL_STATUSES = ("completed", "dead_letter", "cancelled")

# Map Kafka event → new status. job.submitted intentionally doesn't change a
# status view: the dispatcher creates the row in PENDING, and the next event
# (job.progress with status=running, or job.failed) moves it forward.
_EVENT_TO_STATUS: dict[str, str] = {
    "job.progress": "running",
    "job.completed": "completed",
    "job.cancelled": "cancelled",
}

# How many job_ids one (scope, status) key retains. Must comfortably outlive Kafka retention, so
# the trim never evicts an id a late event could still refer to.
READ_MODEL_WINDOW = 10_000

# Refreshed on every write, so an active key never expires and an abandoned one
# (a tenant that stopped submitting, a deleted user) is reaped a week later.
READ_MODEL_TTL_SECONDS = 7 * 24 * 3600


def _tenant_key(tenant_id: str, status: str) -> str:
    return f"jobs:tenant:{tenant_id}:status:{status}"


def _user_key(user_id: str, status: str) -> str:
    return f"jobs:user:{user_id}:status:{status}"


def _evicted_key(status_key: str) -> str:
    """Companion counter holding what the trim removed from `status_key`."""
    return f"{status_key}:evicted"


def _is_wrongtype(exc: ResponseError) -> bool:
    return "WRONGTYPE" in str(exc).upper()


async def _zcall(redis: Redis, op: str, key: str, *args: Any) -> Any:
    """Run a ZSET command, dropping a pre-WO-R2-56 SET key that WRONGTYPEs.

    An incomplete projection (which `rebuild_read_model` repairs) beats a consumer that raises on
    every message and never commits an offset.
    """
    fn = getattr(redis, op)
    try:
        return await fn(key, *args)
    except ResponseError as exc:
        if not _is_wrongtype(exc):
            raise
        logger.warning(
            "read-model dropping pre-bounded key of legacy type — "
            "run rebuild_read_model to restore its counts",
            extra={"key": key, "op": op},
        )
        await redis.delete(key)
        return await fn(key, *args)


async def _project(redis: Redis, key: str, job_id: str, score: float) -> None:
    """Add job_id to `key`, trim the key to its window, refresh both TTLs."""
    await _zcall(redis, "zadd", key, {job_id: score})
    overflow = int(await _zcall(redis, "zcard", key)) - READ_MODEL_WINDOW
    if overflow > 0:
        # Rank 0 is the lowest score — the oldest projection, and the id least
        # likely to ever be spoken about again.
        removed = int(await _zcall(redis, "zremrangebyrank", key, 0, overflow - 1))
        if removed:
            evicted = _evicted_key(key)
            await redis.incrby(evicted, removed)
            await redis.expire(evicted, READ_MODEL_TTL_SECONDS)
    await redis.expire(key, READ_MODEL_TTL_SECONDS)


async def _member_count(redis: Redis, key: str) -> int:
    """ZCARD + whatever the trim evicted, i.e. the true status count."""
    try:
        live = int(await redis.zcard(key))  # type: ignore[misc,unused-ignore]
    except ResponseError as exc:
        if not _is_wrongtype(exc):
            raise
        # A legacy SET key the projector hasn't rewritten yet: report what it
        # holds rather than zero. The next write migrates it.
        logger.warning("read-model counting a legacy set key", extra={"key": key})
        return int(await redis.scard(key))  # type: ignore[misc,unused-ignore]
    evicted = await redis.get(_evicted_key(key))
    return live + (int(evicted) if evicted else 0)


async def _move(
    redis: Redis,
    tenant_id: str,
    user_id: str,
    job_id: str,
    new_status: str,
    score: float | None = None,
) -> None:
    """Remove job_id from any other status view, then add to the new one."""
    at = time.time() if score is None else score
    # Remove from every other status (idempotent: ZREM on a non-member is a
    # no-op). An id the trim already evicted is not here to be removed — its
    # `:evicted` contribution stays behind, which is the documented drift.
    for st in _TRACKED_STATUSES:
        if st == new_status:
            continue
        await _zcall(redis, "zrem", _tenant_key(tenant_id, st), job_id)
        await _zcall(redis, "zrem", _user_key(user_id, st), job_id)
    await _project(redis, _tenant_key(tenant_id, new_status), job_id, at)
    await _project(redis, _user_key(user_id, new_status), job_id, at)


class ReadModelProjector(BaseKafkaConsumer):
    """Consumer group `read-model` — keeps the Redis job-status views in step
    with the lifecycle topics, so the admin overview never aggregates the jobs
    table."""

    def __init__(self, redis: Redis) -> None:
        settings = get_settings()
        super().__init__(
            topics=[
                settings.kafka_topic_job_progress,
                settings.kafka_topic_job_completed,
                settings.kafka_topic_job_failed,
                settings.kafka_topic_job_cancelled,
                settings.kafka_topic_job_dlq,
            ],
            group_id=settings.kafka_consumer_group_read_model,
        )
        self.redis = redis

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **_kafka_meta: Any,
    ) -> None:
        """Move one job id into the status this event implies, ignoring a late
        or repeated event that would drag a finished job backwards."""
        if not isinstance(value, dict):
            return

        job_id = value.get("job_id")
        user_id = value.get("user_id")
        tenant_id = value.get("tenant_id")
        event_name = value.get("event")
        if not (job_id and user_id and tenant_id and event_name):
            return

        # Determine the new status.
        if event_name == "job.failed":
            new_status = "dead_letter" if value.get("dead_lettered") is True else "failed"
        else:
            mapped = _EVENT_TO_STATUS.get(event_name)
            if mapped is None:
                return
            new_status = mapped

        # Terminal-state guard: a late non-terminal event must not drag a finished job back, but
        # terminal→terminal stays allowed so a DLQ replay's job.completed lands.
        if new_status not in _TERMINAL_STATUSES:
            tid = str(tenant_id)
            jid = str(job_id)
            in_terminal = False
            for terminal in _TERMINAL_STATUSES:
                score = await _zcall(
                    self.redis, "zscore", _tenant_key(tid, terminal), jid
                )
                if score is not None:
                    in_terminal = True
                    break
            if in_terminal:
                logger.debug(
                    "read-model ignoring non-terminal event for terminal job",
                    extra={"event": event_name, "job_id": jid, "tenant_id": tid},
                )
                return

        await _move(
            self.redis, str(tenant_id), str(user_id), str(job_id), new_status
        )


async def read_global_stats(redis: Redis, tenant_id: str) -> dict[str, int]:
    """Status → count for one tenant. The name is historical; callers pass the effective
    tenant_id."""
    out: dict[str, int] = {}
    for st in _TRACKED_STATUSES:
        out[st] = await _member_count(redis, _tenant_key(tenant_id, st))
    return out


async def read_user_stats(redis: Redis, user_id: str) -> dict[str, int]:
    """Status → count for one user, read straight from the projection."""
    out: dict[str, int] = {}
    for st in _TRACKED_STATUSES:
        out[st] = await _member_count(redis, _user_key(user_id, st))
    return out


# ---------------------------------------------------------------------------
# Rebuild from Postgres (WO-R2-56)
# ---------------------------------------------------------------------------


def _score_for(moment: datetime | None) -> float:
    """Projection score for a rebuilt member: when the row last moved, so a rebuilt key trims like
    a grown one and later live events sort above it."""
    if moment is None:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


async def _scan_delete(redis: Redis, pattern: str) -> int:
    """Delete every key matching `pattern`, in batches, and say how many."""
    deleted = 0
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor=cursor, match=pattern, count=100)
        if batch:
            deleted += int(await redis.delete(*batch))
        if cursor == 0:
            return deleted


async def _windowed_members(
    session: AsyncSession, scope_col: Any, tenant_id: uuid.UUID | None
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """The `READ_MODEL_WINDOW` most recently updated job ids per (scope, status)."""
    ranked = func.row_number().over(
        partition_by=(scope_col, Job.status),
        order_by=(Job.updated_at.desc(), Job.id.desc()),
    ).label("rank")
    inner = select(
        scope_col.label("scope"),
        Job.status.label("status"),
        Job.id.label("job_id"),
        Job.updated_at.label("updated_at"),
        ranked,
    ).where(Job.status.in_(_TRACKED_STATUSES))
    if tenant_id is not None:
        inner = inner.where(Job.tenant_id == tenant_id)
    sub = inner.subquery()

    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    rows = await session.execute(
        select(sub.c.scope, sub.c.status, sub.c.job_id, sub.c.updated_at).where(
            sub.c.rank <= READ_MODEL_WINDOW
        )
    )
    for scope, status, job_id, updated_at in rows.all():
        out.setdefault((str(scope), str(status)), []).append(
            (str(job_id), _score_for(updated_at))
        )
    return out


async def _status_counts(
    session: AsyncSession, scope_col: Any, tenant_id: uuid.UUID | None
) -> dict[tuple[str, str], int]:
    """Exact (scope, status) → count from the jobs table, for the rebuild."""
    stmt = (
        select(scope_col, Job.status, func.count())
        .where(Job.status.in_(_TRACKED_STATUSES))
        .group_by(scope_col, Job.status)
    )
    if tenant_id is not None:
        stmt = stmt.where(Job.tenant_id == tenant_id)
    rows = await session.execute(stmt)
    return {(str(scope), str(status)): int(n) for scope, status, n in rows.all()}


async def rebuild_read_model(
    session: AsyncSession,
    redis: Redis,
    *,
    tenant_id: uuid.UUID | str | None = None,
) -> dict[str, int]:
    """Recompute every read-model key from the `jobs` table — the projection's only repair path.

    Exact GROUP BY counts, `READ_MODEL_WINDOW`-windowed membership (the rest credited to
    `:evicted`), and NOT atomic against a running projector. `tenant_id` scopes it to one tenant.
    """
    scope = uuid.UUID(str(tenant_id)) if tenant_id is not None else None

    tenant_counts = await _status_counts(session, Job.tenant_id, scope)
    user_counts = await _status_counts(session, Job.user_id, scope)
    tenant_members = await _windowed_members(session, Job.tenant_id, scope)
    user_members = await _windowed_members(session, Job.user_id, scope)

    if scope is None:
        await _scan_delete(redis, "jobs:tenant:*")
        await _scan_delete(redis, "jobs:user:*")
    else:
        await _scan_delete(redis, f"jobs:tenant:{scope}:status:*")
        # Per-user keys are not tenant-prefixed, so a single-tenant rebuild can
        # only clear the users it is about to rewrite.
        for scope_id, _status in user_counts:
            await _scan_delete(redis, f"jobs:user:{scope_id}:status:*")

    written = 0
    members_written = 0
    evicted_total = 0
    for counts, members, key_for in (
        (tenant_counts, tenant_members, _tenant_key),
        (user_counts, user_members, _user_key),
    ):
        for (scope_id, status), total in counts.items():
            key = key_for(scope_id, status)
            retained = members.get((scope_id, status), [])
            if retained:
                await redis.zadd(key, {jid: score for jid, score in retained})
                await redis.expire(key, READ_MODEL_TTL_SECONDS)
            evicted = max(0, total - len(retained))
            if evicted:
                await redis.set(
                    _evicted_key(key), evicted, ex=READ_MODEL_TTL_SECONDS
                )
            written += 1
            members_written += len(retained)
            evicted_total += evicted

    logger.info(
        "read-model rebuilt from postgres",
        extra={
            "tenant_id": str(scope) if scope else "all",
            "keys": written,
            "members": members_written,
            "evicted": evicted_total,
        },
    )
    return {
        "keys": written,
        "members": members_written,
        "evicted": evicted_total,
    }
