"""
Job progress pub/sub via Redis.

The worker publishes ProgressEvents to a per-job channel.
The SSE endpoint reads that channel through the process-wide fan-out broker in
`workers/progress_broker.py` and forwards events to the browser.  This module
owns the wire format, the channel naming and the retained snapshot; the broker
owns the connection.

Channel naming: job:progress:{job_id}
Retained snapshot: job:progress:last:{job_id} (string, 1h TTL)

Pub/Sub is at-most-once: a subscriber that connects after the terminal event
was published sees nothing at all and would hold a silent connection forever.
Every publish therefore also SETs the event as a retained snapshot, and every
subscriber reads that snapshot as its first event — so a late subscriber to a
finished job is told the job finished instead of waiting on a dead channel.
The snapshot is a convenience, never a source of truth: if Redis evicts it the
behaviour degrades to exactly the pre-snapshot stream (durable state lives in
the `jobs` table, and the SSE endpoint short-circuits off it).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "job:progress"
LAST_EVENT_PREFIX = "job:progress:last"
LAST_EVENT_TTL_SECONDS = 3600

# Statuses after which no further progress event can arrive, so the stream is
# closed. `cancelled` is here because saga rollbacks cancel jobs and their
# streams used to hang forever; no producer publishes it as an event status
# today, but the DB short-circuit in api/streaming.py synthesizes one.
TERMINAL_STATUSES = frozenset({"completed", "failed", "dead_letter", "cancelled"})


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


def _last_key(job_id: str) -> str:
    return f"{LAST_EVENT_PREFIX}:{job_id}"


def _parse_event(raw: Any) -> ProgressEvent | None:
    """Decode a retained snapshot. Returns None for anything unusable."""
    try:
        return ProgressEvent(**json.loads(raw))
    except (TypeError, ValueError) as exc:
        logger.warning("discarding malformed progress snapshot: %s", exc)
        return None


async def read_last_event(redis: Redis, job_id: str) -> ProgressEvent | None:
    """The most recent published event for a job, or None if nothing is retained."""
    raw = await redis.get(_last_key(job_id))
    if raw is None:
        return None
    return _parse_event(raw)


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
    payload = event.to_json()
    # Retain BEFORE publishing: a subscriber that has just subscribed must
    # never be able to miss the live event AND find no snapshot. The reverse
    # order leaves exactly that window open.
    await redis.set(_last_key(job_id), payload, ex=LAST_EVENT_TTL_SECONDS)
    await redis.publish(_channel(job_id), payload)


# Subscribing lives in `workers/progress_broker.py`, not here. It used to be a
# `subscribe(redis, job_id)` generator that opened its own `redis.pubsub()`
# per viewer, which made the process's open-stream count its held-connection
# count against a 20-slot shared pool (WO-R2-11). The broker keeps the
# snapshot-then-live-events semantics documented above and shares one Pub/Sub
# connection across every open stream.
