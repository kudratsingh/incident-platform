"""
Redis sorted set for delayed DLQ replays.

Distinct from `app/workers/queue.py`'s `jobs:delayed` set. That one
holds jobs still in the retry cycle (fail-with-backoff); this one
holds explicit operator-initiated replays that should fire after a
wait window — the `wait_and_replay` remediation category.

Members are `{tenant_id}:{principal_id}:{job_id}` strings so the
promote loop knows *who* asked for the replay (audit + tenancy) and
which job to reset. Score is the epoch second the replay should
fire. Rescheduling the same triple updates the score in place — the
tool's idempotency wrapper already dedupes exact-repeat calls, so
this is only reached when the caller varies the delay.
"""

import time
import uuid

from app.workers.queue import _atomic_pop_ready
from redis.asyncio import Redis

SCHEDULED_KEY = "jobs:dlq_replay_delayed"


def _member(tenant_id: uuid.UUID, principal_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"{tenant_id}:{principal_id}:{job_id}"


def _parse(member: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_str, principal_str, job_str = member.split(":", 2)
    return uuid.UUID(tenant_str), uuid.UUID(principal_str), uuid.UUID(job_str)


async def schedule_replay(
    redis: Redis,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    job_id: uuid.UUID,
    delay_seconds: int,
) -> float:
    """Schedule a DLQ replay to fire `delay_seconds` from now.

    Returns the epoch second the replay is scheduled for so the
    caller can audit / echo it back in the tool response.
    """
    execute_at = time.time() + delay_seconds
    await redis.zadd(SCHEDULED_KEY, {_member(tenant_id, principal_id, job_id): execute_at})
    return execute_at


async def pop_ready(
    redis: Redis,
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Atomically remove and return every scheduled replay whose
    `execute_at` has passed. Malformed members (shouldn't happen but
    survive a bad manual write) are dropped from the set and skipped.

    Uses the shared Lua-backed atomic pop from `queue._atomic_pop_ready`
    so two concurrent readers can't process the same member twice —
    the previous read-then-pipeline-zrem shape had a real race that
    became a correctness bug the moment the worker scaled horizontally
    (FIX_PLAN #9)."""
    members = await _atomic_pop_ready(redis, SCHEDULED_KEY, time.time())
    parsed: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    for member in members:
        try:
            parsed.append(_parse(member))
        except (ValueError, AttributeError):
            continue
    return parsed


async def scheduled_length(redis: Redis) -> int:
    return int(await redis.zcard(SCHEDULED_KEY))
