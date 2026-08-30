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
