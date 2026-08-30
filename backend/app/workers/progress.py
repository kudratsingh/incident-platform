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

**Ordering (WO-R2-57).** The snapshot used to be written unconditionally, so
the last *write* won rather than the latest *event* — and the events reaching
this module come off Kafka, where at-least-once delivery means a consumer
rebalance or a failed handler redelivers a `job.progress` the stream has
already moved past. That redelivery overwrote a terminal snapshot with
`running`, and then nothing corrected it: no further event is coming for a
finished job, so for the snapshot's full hour every late subscriber was told
the job was still running and sat on a channel that would never speak again.
The exact hang the snapshot exists to prevent, produced by the snapshot.

`publish` therefore only writes a snapshot that supersedes the retained one
(`_supersedes`), on two rules that need no clock and no coordination:

  * A non-terminal event never replaces a terminal one. Terminal→terminal
    stays allowed, so a DLQ replay's eventual `job.completed` still lands.
  * Within one source topic, an event never replaces one with an equal or
    higher offset. Offsets are per-topic-partition and every event for a job
    shares a partition (the Kafka key is `{tenant}:{user}`), so within a topic
    they are exactly the order the producer wrote — and a redelivery is a
    replay of an offset already seen. Across topics they are incomparable,
    which is what the terminal rule is for.

The event's own `timestamp` is deliberately not the ordering key: it is
stamped when the event is *republished* here, so a redelivered stale event
carries the newest timestamp of all.

A superseded event is dropped entirely rather than published-but-not-retained.
Any subscriber that could still be listening has already been sent the
terminal snapshot and closed; delivering it a stale `running` afterwards would
only walk a progress bar backwards.

The inverse staleness — a terminal snapshot pinned in front of a job that a
DLQ replay put back in flight — is reconciled at the SSE endpoint, which holds
the `jobs` row and can see the disagreement (`api/streaming.py`).
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

# Statuses after which no further progress event can arrive, so the stream is
# closed. `cancelled` is here because saga rollbacks cancel jobs and their
# streams used to hang forever. It was aspirational until WO-R2-113: no
# producer published it, and the only thing that ever set it was the DB
# short-circuit in api/streaming.py, which a client had to reconnect to reach.
# `SseConsumer` now publishes it from the `job.cancelled` topic, so a stream
# already open closes on the event like every other terminal status.
TERMINAL_STATUSES = frozenset({"completed", "failed", "dead_letter", "cancelled"})


@dataclass
class ProgressEvent:
    job_id: str
    status: str       # running | completed | failed | dead_letter | retrying | cancelled
    progress: int     # 0-100
    message: str
    retry_count: int = 0
    timestamp: str = ""
    # Provenance of the event this was built from, carried so the snapshot
    # write can be ordered (see _supersedes). `source` is the Kafka topic and
    # `sequence` the offset within it; both are absent for events published
    # outside the consumer (tests, and any future direct publisher), which
    # leaves only the terminal rule in force.
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
    """Decode a retained snapshot. Returns None for anything unusable.

    Unknown keys are dropped rather than raising: a snapshot written by a
    newer version during a rolling deploy is still a usable event to every
    field this process knows about, and discarding it would strand late
    subscribers on the very hang the snapshot exists to prevent.
    """
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
    """Should `new` replace the retained snapshot `retained`?

    See the module docstring for why these two rules, and why the event's own
    timestamp is not one of them.
    """
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

# Floors on how often a processor's progress reaches Kafka (WO-R2-57). Each
# publish is a Kafka message and an immutable `job_events` row, so a processor
# that reports once per unit of work makes its event count a caller-chosen
# number: a csv_upload with chunk_size=1 over the 1,000,000-row maximum wrote
# a million of each, for one job, at the caller's discretion.
#
# Both floors must be met, which is what makes the bound hold in every
# direction: at most one event per whole percent (so the count can never
# exceed ~102 however many chunks there are) and at most one per half second
# (so a job that races through its work reports a handful of times rather than
# a hundred). What survives is a function of elapsed work, which is what a
# progress bar is for.
MIN_PROGRESS_INTERVAL_SECONDS = 0.5
MIN_PROGRESS_DELTA_PERCENT = 1


def rate_limited(
    publish: ProgressPublisher,
    *,
    min_interval: float = MIN_PROGRESS_INTERVAL_SECONDS,
    min_delta: int = MIN_PROGRESS_DELTA_PERCENT,
) -> ProgressPublisher:
    """Wrap a publisher so it drops updates that say too little, too soon.

    The first update and any update at 100% always go out: the first is what
    tells a subscriber the job started, and the last is the one a stream ends
    on — dropping either would trade a bounded event count for a hung stream.
    Terminal events are published by the dispatcher, not through this wrapper,
    so nothing here can swallow one.
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
    """Retain and fan out one progress event, unless it is stale.

    `source`/`sequence` are the Kafka topic and offset the event came from;
    passing them lets the retained snapshot be ordered rather than
    last-write-wins. Callers with no such provenance may omit them and keep
    the terminal-guard half of the protection.
    """
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
        # Reordered or redelivered. Dropping it keeps the snapshot on the
        # furthest-along event and keeps live subscribers from walking
        # backwards; nothing is lost, because a superseded event by
        # definition says less than what is already retained.
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


# Subscribing lives in `workers/progress_broker.py`, not here. It used to be a
# `subscribe(redis, job_id)` generator that opened its own `redis.pubsub()`
# per viewer, which made the process's open-stream count its held-connection
# count against a 20-slot shared pool (WO-R2-11). The broker keeps the
# snapshot-then-live-events semantics documented above and shares one Pub/Sub
# connection across every open stream.
