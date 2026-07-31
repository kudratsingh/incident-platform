"""Unit tests for the DLQ-replay scheduler ZSET.

Redis is fully mocked. Mirrors the pattern in `test_queue.py` —
same fixtures, same shape — since this is a sibling module with
identical Redis semantics.
"""

import time
import uuid
from unittest.mock import AsyncMock, MagicMock

from app.workers import dlq_replay_scheduler


def _mock_redis() -> AsyncMock:
    r = AsyncMock()
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[1, 1])
    r.pipeline = MagicMock(return_value=pipe)
    return r


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


async def test_pop_ready_parses_and_removes_members() -> None:
    redis = _mock_redis()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    job_a = uuid.uuid4()
    job_b = uuid.uuid4()
    redis.zrangebyscore.return_value = [
        (f"{tenant_id}:{principal_id}:{job_a}", 100.0),
        (f"{tenant_id}:{principal_id}:{job_b}", 200.0),
    ]

    ready = await dlq_replay_scheduler.pop_ready(redis)

    assert ready == [
        (tenant_id, principal_id, job_a),
        (tenant_id, principal_id, job_b),
    ]
    pipe = redis.pipeline.return_value
    assert pipe.zrem.call_count == 2
    pipe.execute.assert_awaited_once()


async def test_pop_ready_returns_empty_when_none_due() -> None:
    redis = _mock_redis()
    redis.zrangebyscore.return_value = []
    assert await dlq_replay_scheduler.pop_ready(redis) == []


async def test_pop_ready_skips_malformed_members() -> None:
    """A malformed member (from a bad manual write, say) is removed
    but not returned — the promote loop won't blow up on it."""
    redis = _mock_redis()
    redis.zrangebyscore.return_value = [
        ("not-a-triple", 100.0),
    ]
    assert await dlq_replay_scheduler.pop_ready(redis) == []
    pipe = redis.pipeline.return_value
    pipe.zrem.assert_called()  # still purged


async def test_scheduled_length_returns_zcard() -> None:
    redis = _mock_redis()
    redis.zcard.return_value = 4
    assert await dlq_replay_scheduler.scheduled_length(redis) == 4
    redis.zcard.assert_awaited_once_with(dlq_replay_scheduler.SCHEDULED_KEY)
