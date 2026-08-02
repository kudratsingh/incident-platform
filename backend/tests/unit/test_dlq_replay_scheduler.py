"""Unit tests for the DLQ-replay scheduler ZSET.

Redis is fully mocked. Mirrors the pattern in `test_queue.py` —
same fixtures, same shape — since this is a sibling module with
identical Redis semantics.
"""

import time
import uuid
from unittest.mock import AsyncMock

from app.workers import dlq_replay_scheduler


def _mock_redis() -> AsyncMock:
    return AsyncMock()


async def test_schedule_replay_uses_future_timestamp() -> None:
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_id = uuid.uuid4()

    before = time.time()
    execute_at = await dlq_replay_scheduler.schedule_replay(
        redis,
        tenant_id=tenant_id,
        principal_id=principal_id,
        job_id=job_id,
        delay_seconds=30,
    )
    after = time.time()

    redis.zadd.assert_awaited_once()
    # zadd(key, {member: score}) — inspect score
    call_key = redis.zadd.call_args[0][0]
    call_mapping = redis.zadd.call_args[0][1]
    assert call_key == dlq_replay_scheduler.SCHEDULED_KEY
    ((member, score),) = call_mapping.items()
    assert f"{tenant_id}:{principal_id}:{job_id}" == member
    assert before + 29 < score < after + 31
    assert execute_at == score


async def test_pop_ready_parses_and_returns_members_via_atomic_eval() -> None:
    """FIX_PLAN #9: pop goes through the shared Lua-atomic helper,
    which returns just member strings. Members parse back to their
    tenant/principal/job triple."""
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_a = uuid.uuid4()
    job_b = uuid.uuid4()
    redis.eval = AsyncMock(
        return_value=[
            f"{tenant_id}:{principal_id}:{job_a}".encode(),
            f"{tenant_id}:{principal_id}:{job_b}".encode(),
        ]
    )

    ready = await dlq_replay_scheduler.pop_ready(redis)

    assert ready == [
        (tenant_id, principal_id, job_a),
        (tenant_id, principal_id, job_b),
    ]
    # Single atomic call — no pipeline path.
    redis.pipeline.assert_not_called()


async def test_pop_ready_returns_empty_when_none_due() -> None:
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=[])
    assert await dlq_replay_scheduler.pop_ready(redis) == []


async def test_pop_ready_skips_malformed_members() -> None:
    """A malformed member (from a bad manual write, say) is dropped
    by the atomic pop but its parse fails and it's skipped in the
    returned list. The promote loop won't blow up on it."""
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=[b"not-a-triple"])
    assert await dlq_replay_scheduler.pop_ready(redis) == []


async def test_scheduled_length_returns_zcard() -> None:
    redis = _mock_redis()
    redis.zcard.return_value = 4
    assert await dlq_replay_scheduler.scheduled_length(redis) == 4
    redis.zcard.assert_awaited_once_with(dlq_replay_scheduler.SCHEDULED_KEY)
