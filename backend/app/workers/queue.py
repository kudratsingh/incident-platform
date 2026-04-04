"""
Redis-backed priority job queue.

Two sorted sets:
  - jobs:queue   — ready jobs, score = priority (ZPOPMAX → highest priority first)
  - jobs:delayed — jobs waiting for retry, score = unix timestamp when ready

The worker calls `promote_delayed()` each tick to move ready delayed jobs into
the main queue, then calls `pop()` to get the next job to process.
"""

import time

from redis.asyncio import Redis

QUEUE_KEY = "jobs:queue"
DELAYED_KEY = "jobs:delayed"


async def push(redis: Redis, job_id: str, priority: int = 0) -> None:
    """Enqueue a job for immediate processing."""
    await redis.zadd(QUEUE_KEY, {job_id: priority})


async def push_delayed(redis: Redis, job_id: str, delay_seconds: float) -> None:
    """Enqueue a job to be retried after `delay_seconds`."""
    run_at = time.time() + delay_seconds
    await redis.zadd(DELAYED_KEY, {job_id: run_at})


async def pop(redis: Redis) -> str | None:
    """Pop the highest-priority ready job. Returns job_id or None."""
    result = await redis.zpopmax(QUEUE_KEY, count=1)
    if not result:
        return None
    # zpopmax returns list of (member, score) tuples
    job_id: str = result[0][0]
    return job_id


async def promote_delayed(redis: Redis) -> int:
    """Move all delayed jobs whose run_at has passed into the main queue.

    Returns the number of jobs promoted.
    """
    now = time.time()
    # Fetch all jobs with score <= now (i.e. ready to run)
    ready: list[tuple[str, float]] = await redis.zrangebyscore(
        DELAYED_KEY, "-inf", now, withscores=True
    )
    if not ready:
        return 0

    pipe = redis.pipeline()
    for job_id, _score in ready:
        pipe.zrem(DELAYED_KEY, job_id)
        # Re-enqueue with priority 0 (retries don't get boosted priority)
        pipe.zadd(QUEUE_KEY, {job_id: 0})
    await pipe.execute()
    return len(ready)


async def queue_length(redis: Redis) -> int:
    return int(await redis.zcard(QUEUE_KEY))


async def delayed_length(redis: Redis) -> int:
    return int(await redis.zcard(DELAYED_KEY))
