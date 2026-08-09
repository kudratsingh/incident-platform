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
# Boundedness (E1-12). The pre-fix script called `unpack(due)` on the
# entire ZRANGEBYSCORE result. Lua's `unpack` is capped by LUAI_MAXCSTACK
# (8000 by default), so the moment ~8000 members came due the EVAL failed
# with "too many results to unpack" — on every subsequent tick, because
# nothing was ever removed. Both readers of this script (`jobs:delayed`
# and `jobs:dlq_replay_delayed`, via `_atomic_pop_ready`) wedged
# permanently, and a wedged set only grows. Two independent guards:
#
#   1. LIMIT on the ZRANGEBYSCORE — the result set can never reach the
#      unpack ceiling in the first place.
#   2. Chunked ZREM — single-pass today because the LIMIT is below the
#      chunk size, but it keeps the script correct for whoever raises
#      the LIMIT later.
#
# Script contract:
#   KEYS[1] : sorted-set key
#   ARGV[1] : max score (as string; Redis parses it)
#   returns : list of member strings ready to process, AT MOST 1000 per
#             call. Empty list when nothing is due — caller need not test
#             emptiness before looping. A backlog larger than the limit
#             drains across successive calls: both callers tick every
#             POLL_INTERVAL (0.5s), i.e. 2000 members/s per set.
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
    """Atomically ZRANGEBYSCORE(-inf, max_score) + ZREM the same members
    in a single Redis round-trip. Returns the member strings that were
    both ready AND successfully removed by this call — no other client
    can pop the same members concurrently.

    Bounded: at most 1000 members per call (see `_POP_READY_LUA`).
    Callers must not assume they received every due member."""
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
    """Atomically remove and return delayed jobs whose run_at has passed,
    at most 1000 per call. Used by the dispatcher's
    `_promote_delayed_loop` to republish ready retries to `job.submitted`
    (via the outbox).

    Popped members are gone from Redis before the caller has done
    anything with them, so the caller owns them: dropping one loses the
    retry. `_promote_delayed_once` isolates each item and re-pushes on
    failure for exactly that reason."""
    return await _atomic_pop_ready(redis, DELAYED_KEY, time.time())


async def delayed_length(redis: Redis) -> int:
    return int(await redis.zcard(DELAYED_KEY))
