"""
Job progress pub/sub via Redis: channel `job:progress:{job_id}` plus snapshot
`job:progress:last:{job_id}` (1h TTL), which every subscriber reads first so a late one to a
finished job is not stranded. `progress_broker.py` owns the connection. Ordering (WO-R2-57):
`publish` retains only an event that `_supersedes` the retained one, because a redelivered
`job.progress` once overwrote terminal snapshots; the `timestamp` is NOT the ordering key.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "job:progress"
LAST_EVENT_PREFIX = "job:progress:last"
LAST_EVENT_TTL_SECONDS = 3600

# Statuses after which no further progress event can arrive, so the stream closes. `cancelled` is
# for saga rollbacks, and got a producer only with WO-R2-113 (`SseConsumer` on `job.cancelled`).
TERMINAL_STATUSES = frozenset({"completed", "failed", "dead_letter", "cancelled"})


@dataclass
class ProgressEvent:
    """One progress update for a job, as it travels over Redis pub/sub and as
    the retained snapshot is stored."""

    job_id: str
    status: str       # running | completed | failed | dead_letter | retrying | cancelled
    progress: int     # 0-100
    message: str
    retry_count: int = 0
    timestamp: str = ""
    # Provenance for `_supersedes`: `source` is the Kafka topic, `sequence` its offset. Absent
    # outside the consumer.
    source: str = ""
    sequence: int | None = None

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
    """Decode a retained snapshot, or None if unusable. Unknown keys are dropped rather than
    raising, so a newer version's snapshot mid-deploy stays usable."""
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise TypeError(f"snapshot is {type(decoded).__name__}, not an object")
        known = {f.name for f in fields(ProgressEvent)}
        return ProgressEvent(**{k: v for k, v in decoded.items() if k in known})
    except (TypeError, ValueError) as exc:
        logger.warning("discarding malformed progress snapshot: %s", exc)
        return None


def _supersedes(new: ProgressEvent, retained: ProgressEvent | None) -> bool:
    """Should `new` replace the retained snapshot `retained`? (Rules: module docstring.)"""
    if retained is None:
        return True
    # A finished job does not go back to running because Kafka said it twice.
    if retained.status in TERMINAL_STATUSES and new.status not in TERMINAL_STATUSES:
        return False
    # Within one topic the offset is the producer's order; a redelivery
    # replays an offset already retained.
    if (
        new.source
        and new.source == retained.source
        and new.sequence is not None
        and retained.sequence is not None
        and new.sequence <= retained.sequence
    ):
        return False
    return True


async def read_last_event(redis: Redis, job_id: str) -> ProgressEvent | None:
    """The most recent published event for a job, or None if nothing is retained."""
    raw = await redis.get(_last_key(job_id))
    if raw is None:
        return None
    return _parse_event(raw)


# Type alias for the publish callable passed into processors
ProgressPublisher = Callable[[int, str], Awaitable[None]]

# Floors on how often progress reaches Kafka (WO-R2-57): each publish is a Kafka message and an
# immutable `job_events` row, and chunk_size=1 over a 1,000,000-row csv wrote a million of each.
MIN_PROGRESS_INTERVAL_SECONDS = 0.5
MIN_PROGRESS_DELTA_PERCENT = 1


def rate_limited(
    publish: ProgressPublisher,
    *,
    min_interval: float = MIN_PROGRESS_INTERVAL_SECONDS,
    min_delta: int = MIN_PROGRESS_DELTA_PERCENT,
) -> ProgressPublisher:
    """Wrap a publisher so it drops updates that say too little, too soon.

    The first update and any at 100% always go out, or a bounded event count buys a hung stream.
    """
    last_at: float | None = None
    last_percent = 0

    async def _publish(percent: int, message: str) -> None:
        nonlocal last_at, last_percent
        now = time.monotonic()
        due = (
            last_at is None
            or percent >= 100
            or (
                now - last_at >= min_interval
                and percent - last_percent >= min_delta
            )
        )
        if not due:
            return
        last_at = now
        last_percent = percent
        await publish(percent, message)

    return _publish


async def publish(
    redis: Redis,
    job_id: str,
    status: str,
    progress: int,
    message: str,
    retry_count: int = 0,
    source: str = "",
    sequence: int | None = None,
) -> None:
    """Retain and fan out a progress event unless it is stale. `source`/`sequence` (the Kafka
    topic and offset) order the snapshot instead of last-write-wins."""
    event = ProgressEvent(
        job_id=job_id,
        status=status,
        progress=progress,
        message=message,
        retry_count=retry_count,
        source=source,
        sequence=sequence,
    )
    retained = await read_last_event(redis, job_id)
    if not _supersedes(event, retained):
        # Reordered or redelivered: dropping it keeps the snapshot furthest-along.
        logger.info(
            "dropping superseded progress event",
            extra={
                "job_id": job_id,
                "status": status,
                "retained_status": retained.status if retained else None,
                "source": source,
                "sequence": sequence,
                "retained_sequence": retained.sequence if retained else None,
            },
        )
        return
    payload = event.to_json()
    # Retain BEFORE publishing: a subscriber that has just subscribed must
    # never be able to miss the live event AND find no snapshot. The reverse
    # order leaves exactly that window open.
    await redis.set(_last_key(job_id), payload, ex=LAST_EVENT_TTL_SECONDS)
    await redis.publish(_channel(job_id), payload)


# Subscribing lives in `workers/progress_broker.py`: a per-viewer `redis.pubsub()` made the
# open-stream count the held-connection count against a 20-slot pool (WO-R2-11).
