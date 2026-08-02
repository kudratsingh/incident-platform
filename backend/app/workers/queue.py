"""
Redis sorted set for delayed retries.

Historical note
---------------
Before Phase 7 this module also held the primary job queue (`jobs:queue`,
scored by priority, popped by the worker). Phase 7 replaced that path with
Kafka: jobs are now dispatched via `job.submitted` and consumed by
`JobDispatcherConsumer`. The `jobs:queue` sorted set was orphaned — pushed
to on every create/replay but never consumed. Removed in PR fixing the
"write-only queue leak" (a code-review engineer caught it walking the
codebase against the docs).

What survives
-------------
The delayed set is still needed for the retry path. When a job fails and
still has retries, the dispatcher pushes it into `jobs:delayed` scored by
`time.time() + backoff_seconds`. The `_promote_delayed_loop` polls, pops
the ready entries, and republishes them via the outbox → `job.submitted`.
"""

import time

from redis.asyncio import Redis

DELAYED_KEY = "jobs:delayed"

# Atomic ZRANGEBYSCORE + ZREM via Lua (FIX_PLAN #9).
#
# The pre-v0.4.6 shape was two separate round-trips: read the ready set,
# then pipeline a per-member ZREM. Between those two, a second reader on
# the same key would see the same members and process them a second
# time. Naturally deduped downstream by the DLQ status filter for
# delayed retries, but the race exists and becomes a real correctness
# bug the moment the worker horizontally scales (Phase 8). Making the
# pop atomic converts a "theoretical, will bite later" into
# "impossible-by-construction".
#
# Script contract:
#   KEYS[1] : sorted-set key
#   ARGV[1] : max score (as string; Redis parses it)
#   returns : list of member strings ready to process. Empty list when
#             nothing is due — caller need not test emptiness before
#             looping.
_POP_READY_LUA = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if #due > 0 then
    redis.call('ZREM', KEYS[1], unpack(due))
end
return due
"""


async def _atomic_pop_ready(redis: Redis, key: str, max_score: float) -> list[str]:
    """Atomically ZRANGEBYSCORE(-inf, max_score) + ZREM the same members
    in a single Redis round-trip. Returns the member strings that were
    both ready AND successfully removed by this call — no other client
    can pop the same members concurrently."""
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
    """Atomically remove and return all delayed jobs whose run_at has
    passed. Used by the dispatcher's `_promote_delayed_loop` to
    republish ready retries to `job.submitted` (via the outbox)."""
    return await _atomic_pop_ready(redis, DELAYED_KEY, time.time())


async def delayed_length(redis: Redis) -> int:
    return int(await redis.zcard(DELAYED_KEY))
