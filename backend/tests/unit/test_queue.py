"""Unit tests for the Redis job queue — Redis is fully mocked."""

import time
from unittest.mock import AsyncMock, MagicMock

from app.workers import queue


def _mock_redis() -> AsyncMock:
    r = AsyncMock()
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[1, 1, 1, 1])
    # pipeline() is a sync call that returns a pipeline object (not a coroutine)
    r.pipeline = MagicMock(return_value=pipe)
    return r


async def test_push_calls_zadd() -> None:
    redis = _mock_redis()
    await queue.push(redis, "job-1", priority=5)
    redis.zadd.assert_awaited_once_with(queue.QUEUE_KEY, {"job-1": 5})


async def test_pop_returns_job_id() -> None:
    redis = _mock_redis()
    redis.zpopmax.return_value = [("job-abc", 10.0)]
    result = await queue.pop(redis)
    assert result == "job-abc"


async def test_pop_returns_none_when_empty() -> None:
    redis = _mock_redis()
    redis.zpopmax.return_value = []
    result = await queue.pop(redis)
    assert result is None


async def test_push_delayed_uses_future_timestamp() -> None:
    redis = _mock_redis()
    before = time.time()
    await queue.push_delayed(redis, "job-2", delay_seconds=10.0)
    after = time.time()

    redis.zadd.assert_awaited_once()
    _, kwargs = redis.zadd.call_args
    score = list(redis.zadd.call_args[0][1].values())[0]
    assert before + 9 < score < after + 11


async def test_promote_delayed_moves_ready_jobs() -> None:
    redis = _mock_redis()
    # Two jobs are ready (score <= now)
    redis.zrangebyscore.return_value = [("job-x", 100.0), ("job-y", 200.0)]
    pipe = redis.pipeline.return_value
    pipe.execute = AsyncMock(return_value=[1, 1, 1, 1])

    promoted = await queue.promote_delayed(redis)
    assert promoted == 2


async def test_promote_delayed_does_nothing_when_empty() -> None:
    redis = _mock_redis()
    redis.zrangebyscore.return_value = []
    promoted = await queue.promote_delayed(redis)
    assert promoted == 0
