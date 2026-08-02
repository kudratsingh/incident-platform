"""Unit tests for the Redis delayed-retry sorted set — Redis is fully mocked.

Only the delayed set is in scope. The primary `jobs:queue` sorted set was
removed in the queue-leak fix — Kafka took over primary dispatch in Phase 7.
"""

import time
from unittest.mock import AsyncMock

from app.workers import queue


def _mock_redis() -> AsyncMock:
    return AsyncMock()


async def test_push_delayed_uses_future_timestamp() -> None:
    redis = _mock_redis()
    before = time.time()
    await queue.push_delayed(redis, "job-2", delay_seconds=10.0)
    after = time.time()

    redis.zadd.assert_awaited_once()
    score = list(redis.zadd.call_args[0][1].values())[0]
    assert before + 9 < score < after + 11


async def test_pop_ready_delayed_returns_ids_atomically_via_lua() -> None:
    """FIX_PLAN #9: pop is a single Lua EVAL, not a
    zrangebyscore-then-pipeline-zrem. Verifies the returned ids match
    what the script returned AND that the read-then-remove race no
    longer requires multiple round-trips."""
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=[b"job-x", b"job-y"])

    ready = await queue.pop_ready_delayed(redis)

    assert ready == ["job-x", "job-y"]
    redis.eval.assert_awaited_once()
    # Script gets the DELAYED_KEY, current time (as string).
    args = redis.eval.await_args.args
    assert args[1] == 1  # numkeys
    assert args[2] == queue.DELAYED_KEY
    # No pipeline path exercised — the old race is gone.
    redis.pipeline.assert_not_called()


async def test_pop_ready_delayed_returns_empty_when_lua_returns_empty() -> None:
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=[])
    assert await queue.pop_ready_delayed(redis) == []


async def test_atomic_pop_ready_decodes_str_and_bytes() -> None:
    """decode_responses varies per-deployment; the helper accepts both."""
    redis = _mock_redis()
    redis.eval = AsyncMock(return_value=["already-str", b"bytes-form"])
    result = await queue._atomic_pop_ready(redis, "someset", 100.0)
    assert result == ["already-str", "bytes-form"]


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
