"""
Redis sorted set for delayed retries. A failing job with retries left goes into `jobs:delayed`
scored by `time.time() + backoff_seconds`, and `_promote_delayed_loop` pops the ready entries and
republishes them through the outbox onto `job.submitted`.

The primary `jobs:queue` set this module used to hold went away with Phase 7's move to Kafka.
"""

import time

from redis.asyncio import Redis

DELAYED_KEY = "jobs:delayed"

# Atomic ZRANGEBYSCORE + ZREM via Lua (FIX_PLAN #9): the old read-then-ZREM pair let a second reader
# take the same members. Bounded (E1-12) because `unpack` over a whole result hits Lua's
# LUAI_MAXCSTACK ceiling at ~8000 and wedges the set — hence the LIMIT and the 1000-member cap.
_POP_READY_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 1000)
if #due > 0 then
    for i = 1, #due, 1000 do
        redis.call('ZREM', KEYS[1], unpack(due, i, math.min(i + 999, #due)))
    end
end
return due
"""


async def _atomic_pop_ready(redis: Redis, key: str, max_score: float) -> list[str]:
    """Atomically ZRANGEBYSCORE(-inf, max_score) + ZREM in one round-trip, so no other client can
    pop the same members. At most 1000 per call, so this is not every due member."""
    raw = await redis.eval(_POP_READY_LUA, 1, key, str(max_score))
    # decode_responses is True in production; keep byte-safety for tests
    # that pass raw bytes back.
    return [
        item.decode() if isinstance(item, bytes) else str(item) for item in raw
    ]


async def push_delayed(redis: Redis, job_id: str, delay_seconds: float) -> None:
    """Enqueue a job to be retried after `delay_seconds`."""
    run_at = time.time() + delay_seconds
    await redis.zadd(DELAYED_KEY, {job_id: run_at})


async def pop_ready_delayed(redis: Redis) -> list[str]:
    """Remove and return delayed jobs whose run_at has passed, at most 1000 per call. Popped
    members are gone from Redis, so dropping one loses the retry."""
    return await _atomic_pop_ready(redis, DELAYED_KEY, time.time())


async def delayed_length(redis: Redis) -> int:
    return int(await redis.zcard(DELAYED_KEY))
