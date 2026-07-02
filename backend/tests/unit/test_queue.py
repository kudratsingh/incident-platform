"""Unit tests for the Redis delayed-retry sorted set — Redis is fully mocked.

Only the delayed set is in scope. The primary `jobs:queue` sorted set was
removed in the queue-leak fix — Kafka took over primary dispatch in Phase 7.
"""

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


async def test_push_delayed_uses_future_timestamp() -> None:
    redis = _mock_redis()
    before = time.time()
    await queue.push_delayed(redis, "job-2", delay_seconds=10.0)
    after = time.time()

    redis.zadd.assert_awaited_once()
    score = list(redis.zadd.call_args[0][1].values())[0]
    assert before + 9 < score < after + 11


async def test_pop_ready_delayed_returns_and_removes_ready_ids() -> None:
    redis = _mock_redis()
    redis.zrangebyscore.return_value = [("job-x", 100.0), ("job-y", 200.0)]
    pipe = redis.pipeline.return_value

    ready = await queue.pop_ready_delayed(redis)

    assert ready == ["job-x", "job-y"]
    # One zrem per ready id; pipeline.execute awaited once.
    assert pipe.zrem.call_count == 2
    pipe.execute.assert_awaited_once()


async def test_pop_ready_delayed_returns_empty_when_none_ready() -> None:
    redis = _mock_redis()
    redis.zrangebyscore.return_value = []
    assert await queue.pop_ready_delayed(redis) == []


async def test_delayed_length_returns_zcard() -> None:
    redis = _mock_redis()
    redis.zcard.return_value = 7
    assert await queue.delayed_length(redis) == 7
    redis.zcard.assert_awaited_once_with(queue.DELAYED_KEY)


def test_primary_queue_functions_removed() -> None:
    """Regression: `push`, `pop`, `promote_delayed`, `queue_length`, and
    `QUEUE_KEY` were removed in the queue-leak fix. If any of them come
    back, they need to be paired with an actual consumer — the last time
    they were pushed to without one, jobs:queue grew forever with no TTL
    (a code reviewer walking the docs against the code caught it)."""
    assert not hasattr(queue, "push")
    assert not hasattr(queue, "pop")
    assert not hasattr(queue, "promote_delayed")
    assert not hasattr(queue, "queue_length")
    assert not hasattr(queue, "QUEUE_KEY")
