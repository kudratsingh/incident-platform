"""
Job progress pub/sub via Redis.

The worker publishes ProgressEvents to a per-job channel.
The SSE endpoint subscribes to that channel and forwards events to the browser.

Channel naming: job:progress:{job_id}
"""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

CHANNEL_PREFIX = "job:progress"


@dataclass
class ProgressEvent:
    job_id: str
    status: str       # running | completed | failed | dead_letter | retrying
    progress: int     # 0-100
    message: str
    retry_count: int = 0
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def _channel(job_id: str) -> str:
    return f"{CHANNEL_PREFIX}:{job_id}"


# Type alias for the publish callable passed into processors
ProgressPublisher = Callable[[int, str], Awaitable[None]]


async def publish(
    redis: Redis,
    job_id: str,
    status: str,
    progress: int,
    message: str,
    retry_count: int = 0,
) -> None:
    event = ProgressEvent(
        job_id=job_id,
        status=status,
        progress=progress,
        message=message,
        retry_count=retry_count,
    )
    await redis.publish(_channel(job_id), event.to_json())


async def subscribe(
    redis: Redis, job_id: str
) -> AsyncGenerator[ProgressEvent, None]:
    """Async generator that yields ProgressEvents until the job reaches a terminal state."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(_channel(job_id))
    terminal = {"completed", "failed", "dead_letter"}

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            event = ProgressEvent(**json.loads(message["data"]))
            yield event
            if event.status in terminal:
                break
    finally:
        await pubsub.unsubscribe(_channel(job_id))
        await pubsub.aclose()
