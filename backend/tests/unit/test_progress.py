"""Unit tests for the Redis progress bridge (E1-10) — the publish half.

Redis Pub/Sub is at-most-once. A client that subscribes after the terminal
event was published receives nothing at all — and because terminal detection
only ever fired on a *live* message, the stream sat on a silent channel
forever, holding the SSE connection open with it.

The fix is a retained snapshot: publish() SETs the last event under
`job:progress:last:{job_id}` **before** publishing it, and the subscriber
reads that snapshot as its first event (after subscribing, never before) and
ends immediately when it is terminal. The ordering assertions for the write
side live here.

The read side moved: subscribing is no longer a per-viewer generator in this
module but the process-wide fan-out broker in `workers/progress_broker.py`
(WO-R2-11), and the snapshot/terminal tests moved with it to
`tests/unit/test_progress_broker.py`.

Every test here fakes Redis; nothing touches a server.
"""

import json

import pytest
from app.workers.progress import (
    LAST_EVENT_PREFIX,
    LAST_EVENT_TTL_SECONDS,
    ProgressEvent,
    publish,
    rate_limited,
    read_last_event,
)

JOB_ID = "11111111-1111-1111-1111-111111111111"


class _FakeRedis:
    """Just enough Redis for progress.py's write side: get / set / publish."""

    def __init__(self, last_event: str | None = None) -> None:
        self.store: dict[str, str] = {}
        if last_event is not None:
            self.store[f"{LAST_EVENT_PREFIX}:{JOB_ID}"] = last_event
        self.published: list[tuple[str, str]] = []
        self.sets: list[tuple[str, str, int | None]] = []
        self.calls: list[str] = []

    async def get(self, key: str) -> str | None:
        self.calls.append("get")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.calls.append("set")
        self.sets.append((key, value, ex))
        self.store[key] = value

    async def publish(self, channel: str, payload: str) -> None:
        self.calls.append("publish")
        self.published.append((channel, payload))


# ---------------------------------------------------------------------------
# publish() / read_last_event() — the retained key itself
# ---------------------------------------------------------------------------


async def test_publish_retains_snapshot_before_publishing() -> None:
    """SET must precede PUBLISH, with the documented key and 1h TTL.

    Publishing first would leave a window in which a just-subscribed client
    misses the live event and then finds no snapshot — the exact hang.
    """
    redis = _FakeRedis()

    await publish(
        redis,  # type: ignore[arg-type]
        job_id=JOB_ID,
        status="completed",
        progress=100,
        message="Done",
        retry_count=2,
    )

    key, value, ex = redis.sets[0]
    assert key == f"job:progress:last:{JOB_ID}"
    assert ex == LAST_EVENT_TTL_SECONDS == 3600
    assert redis.calls.index("set") < redis.calls.index("publish")

    # Snapshot and published payload are the same event.
    assert json.loads(value) == json.loads(redis.published[0][1])
    assert redis.published[0][0] == f"job:progress:{JOB_ID}"
    snapshot = json.loads(value)
    assert snapshot["status"] == "completed"
    assert snapshot["retry_count"] == 2


@pytest.mark.parametrize("stored", [None, "{not json", '{"unexpected": 1}'])
async def test_read_last_event_returns_none_for_unusable_snapshots(
    stored: str | None,
) -> None:
    redis = _FakeRedis(last_event=stored)

    assert await read_last_event(redis, JOB_ID) is None  # type: ignore[arg-type]


async def test_read_last_event_round_trips_a_published_event() -> None:
    redis = _FakeRedis()

    await publish(
        redis,  # type: ignore[arg-type]
        job_id=JOB_ID,
        status="running",
        progress=55,
        message="Halfway",
    )
    event = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]

    assert event is not None
    assert (event.job_id, event.status, event.progress) == (JOB_ID, "running", 55)


# ---------------------------------------------------------------------------
# Snapshot ordering (WO-R2-57)
#
# Kafka is at-least-once, so a rebalance or a failed handler redelivers a
# `job.progress` the job has already moved past. Written unconditionally, that
# redelivery replaced a terminal snapshot with `running` — and nothing ever
# corrected it, because no further event is coming for a finished job. Every
# late subscriber for the snapshot's remaining hour was then told the job was
# still running and sat on a channel that would never speak again.
# ---------------------------------------------------------------------------


async def _publish_event(
    redis: _FakeRedis, status: str, progress: int, **kw: object
) -> None:
    await publish(
        redis,  # type: ignore[arg-type]
        job_id=JOB_ID,
        status=status,
        progress=progress,
        message=f"{status} {progress}",
        **kw,  # type: ignore[arg-type]
    )


async def test_redelivered_progress_does_not_overwrite_a_terminal_snapshot() -> None:
    redis = _FakeRedis()
    await _publish_event(redis, "completed", 100, source="job.completed", sequence=4)

    await _publish_event(redis, "running", 60, source="job.progress", sequence=11)

    retained = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.status == "completed"
    # And the stale event is not fanned out either: any subscriber still
    # listening was closed by the terminal event it already received.
    assert len(redis.published) == 1


async def test_late_subscriber_reads_the_terminal_state_after_a_redelivery() -> None:
    """The user-visible half of the same failure: what a stream opened after
    the redelivery is handed as its first event."""
    redis = _FakeRedis()
    await _publish_event(redis, "dead_letter", 0, source="job.dlq", sequence=2)
    await _publish_event(redis, "running", 25, source="job.progress", sequence=99)

    first_event = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]

    assert first_event is not None
    assert first_event.status == "dead_letter"


async def test_replayed_offset_within_a_topic_is_dropped() -> None:
    """Redelivery of a non-terminal event, with no terminal event involved:
    the offset is the producer's order and this one has already been seen."""
    redis = _FakeRedis()
    await _publish_event(redis, "running", 90, source="job.progress", sequence=42)

    await _publish_event(redis, "running", 30, source="job.progress", sequence=17)

    retained = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.progress == 90


async def test_forward_progress_within_a_topic_is_retained() -> None:
    redis = _FakeRedis()
    await _publish_event(redis, "running", 30, source="job.progress", sequence=17)

    await _publish_event(redis, "running", 90, source="job.progress", sequence=42)

    retained = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.progress == 90


async def test_terminal_may_replace_terminal() -> None:
    """A DLQ replay's eventual job.completed must still land — the guard
    refuses non-terminal events, not every event."""
    redis = _FakeRedis()
    await _publish_event(redis, "dead_letter", 0, source="job.dlq", sequence=2)

    await _publish_event(redis, "completed", 100, source="job.completed", sequence=1)

    retained = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.status == "completed"


async def test_offsets_from_different_topics_are_not_compared() -> None:
    """Offsets are per topic-partition. A `job.completed` at offset 1 says
    nothing about a `job.progress` at offset 900, and ordering them by number
    would drop live events wholesale."""
    redis = _FakeRedis()
    await _publish_event(redis, "running", 10, source="job.progress", sequence=900)

    await _publish_event(redis, "running", 20, source="job.other", sequence=1)

    retained = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.progress == 20


async def test_events_without_provenance_keep_the_terminal_guard() -> None:
    """Callers with no Kafka offset to pass (tests, direct publishers) lose
    the offset rule and keep the one that matters most."""
    redis = _FakeRedis()
    await _publish_event(redis, "completed", 100)

    await _publish_event(redis, "running", 50)

    retained = await read_last_event(redis, JOB_ID)  # type: ignore[arg-type]
    assert retained is not None
    assert retained.status == "completed"


# ---------------------------------------------------------------------------
# Publish-rate floors (WO-R2-57)
# ---------------------------------------------------------------------------


async def test_rate_limited_bounds_a_million_offers_to_about_a_hundred() -> None:
    """Each publish is a Kafka message and an immutable job_events row, and a
    processor that reports once per chunk made that count caller-chosen: a
    csv_upload with chunk_size=1 over the 1,000,000-row cap wrote a million of
    each, for one job. The wrapper is where that stops being possible."""
    sent: list[int] = []

    async def _sink(percent: int, message: str) -> None:
        sent.append(percent)

    limited = rate_limited(_sink)
    for i in range(1_000_000):
        await limited(i * 100 // 1_000_000, f"chunk {i}")

    # At most one per whole percent, plus the first — and no interval has
    # elapsed in this loop, so in practice far fewer.
    assert len(sent) <= 102
    assert sent[0] == 0


async def test_rate_limited_always_emits_the_first_and_final_updates() -> None:
    """Dropping either would trade a bounded event count for a hung stream."""
    sent: list[int] = []

    async def _sink(percent: int, message: str) -> None:
        sent.append(percent)

    limited = rate_limited(_sink)
    await limited(0, "start")
    await limited(50, "swallowed — same instant, and interval not met")
    await limited(100, "done")

    assert sent == [0, 100]


async def test_rate_limited_lets_slower_work_through() -> None:
    """The floor is on rate, not on count: once the interval has passed, an
    update that moved the bar is published."""
    sent: list[int] = []

    async def _sink(percent: int, message: str) -> None:
        sent.append(percent)

    limited = rate_limited(_sink, min_interval=0.0, min_delta=10)
    for percent in (0, 3, 6, 12, 15, 24):
        await limited(percent, "working")

    assert sent == [0, 12, 24]


async def test_published_payload_is_the_dataclass_wire_shape() -> None:
    """The frontend types mirror ProgressEvent — publish must not hand-roll JSON."""
    redis = _FakeRedis()

    await publish(
        redis,  # type: ignore[arg-type]
        job_id=JOB_ID,
        status="running",
        progress=10,
        message="Working",
    )

    payload = json.loads(redis.published[0][1])
    expected = ProgressEvent(
        job_id=JOB_ID, status="running", progress=10, message="Working"
    )
    assert set(payload) == set(vars(expected))
