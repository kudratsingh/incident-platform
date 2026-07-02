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
from typing import cast

from redis.asyncio import Redis

DELAYED_KEY = "jobs:delayed"


async def push_delayed(redis: Redis, job_id: str, delay_seconds: float) -> None:
    """Enqueue a job to be retried after `delay_seconds`."""
    run_at = time.time() + delay_seconds
    await redis.zadd(DELAYED_KEY, {job_id: run_at})


async def pop_ready_delayed(redis: Redis) -> list[str]:
    """Atomically remove and return all delayed jobs whose run_at has passed.

    Used by the dispatcher's `_promote_delayed_loop` to republish ready
    retries to the `job.submitted` Kafka topic (via the outbox).
    """
    now = time.time()
    raw = await redis.zrangebyscore(DELAYED_KEY, "-inf", now, withscores=True)
    ready = cast(list[tuple[str, float]], raw)
    if not ready:
        return []

    pipe = redis.pipeline()
    for job_id, _score in ready:
        pipe.zrem(DELAYED_KEY, job_id)
    await pipe.execute()
    return [job_id for job_id, _score in ready]


async def delayed_length(redis: Redis) -> int:
    return int(await redis.zcard(DELAYED_KEY))
