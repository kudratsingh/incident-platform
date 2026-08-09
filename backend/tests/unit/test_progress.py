"""Unit tests for the Redis progress bridge (E1-10).

Redis Pub/Sub is at-most-once. A client that subscribes after the terminal
event was published receives nothing at all — and because subscribe()'s
terminal detection only ever fired on a *live* message, the generator sat on a
silent channel forever, holding the SSE connection open with it.

The fix is a retained snapshot: publish() SETs the last event under
`job:progress:last:{job_id}` before publishing it, and subscribe() reads that
snapshot as its first event (after subscribing, never before) and ends
immediately when it is terminal.

Every test here fakes Redis; nothing touches a server. The listen() stand-in
blocks forever on purpose — that is the silent channel, and the asyncio
timeouts are what turn "hangs forever" into a failing test instead of a hung
suite.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable
from typing import Any

import pytest
from app.workers.progress import (
    LAST_EVENT_PREFIX,
    LAST_EVENT_TTL_SECONDS,
    ProgressEvent,
    publish,
    read_last_event,
    subscribe,
)

JOB_ID = "11111111-1111-1111-1111-111111111111"


def _event_json(status: str, progress: int = 0, message: str = "x") -> str:
    return ProgressEvent(
        job_id=JOB_ID, status=status, progress=progress, message=message
    ).to_json()


class _FakePubSub:
    """Pub/Sub double whose listen() replays `messages`, then blocks forever.

    Blocking (rather than ending) is the point: a real Pub/Sub connection with
    no traffic never returns from listen(), so any test that finishes here
    finished because subscribe() decided to stop, not because the fake ran dry.
    """

    def __init__(
        self, messages: Iterable[dict[str, Any]], calls: list[str]
    ) -> None:
        self._messages = list(messages)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False
        # Shared with the owning _FakeRedis: the snapshot GET must land after
        # SUBSCRIBE, which is only visible in one combined ordering log.
        self.calls = calls

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)
        self.calls.append("subscribe")

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True

    async def listen(self) -> AsyncGenerator[dict[str, Any], None]:
        for message in self._messages:
            yield message
        await asyncio.Event().wait()  # silent channel — never returns
        yield {}  # pragma: no cover — unreachable


class _FakeRedis:
    """Just enough Redis for progress.py: get / set / publish / pubsub."""

    def __init__(
        self,
        last_event: str | None = None,
        messages: Iterable[dict[str, Any]] = (),
    ) -> None:
        self.store: dict[str, str] = {}
        if last_event is not None:
            self.store[f"{LAST_EVENT_PREFIX}:{JOB_ID}"] = last_event
        self.published: list[tuple[str, str]] = []
        self.sets: list[tuple[str, str, int | None]] = []
        self.calls: list[str] = []
        self.pubsub_obj = _FakePubSub(messages, self.calls)

    def pubsub(self) -> _FakePubSub:
        return self.pubsub_obj

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


async def _drain(gen: AsyncGenerator[ProgressEvent, None]) -> list[ProgressEvent]:
    """Consume a subscribe() generator to exhaustion."""
    return [event async for event in gen]


# ---------------------------------------------------------------------------
# subscribe() — the retained snapshot and the terminal short-circuit
# ---------------------------------------------------------------------------


async def test_late_subscriber_gets_terminal_snapshot_and_stream_ends() -> None:
    """THE E1-10 assertion: the job finished before anyone subscribed.

    The channel is silent (listen() never yields), so pre-fix this generator
    never produced an event and never returned — the SSE connection hung until
    the client gave up. With the snapshot it yields exactly one terminal event
    and terminates. asyncio.wait_for is the proof: at HEAD this times out.
    """
    redis = _FakeRedis(last_event=_event_json("completed", 100, "Done"))

    events = await asyncio.wait_for(_drain(subscribe(redis, JOB_ID)), timeout=2)  # type: ignore[arg-type]

    assert len(events) == 1
    assert events[0].status == "completed"
    assert events[0].progress == 100
    # The finally block still ran: no leaked subscription.
    assert redis.pubsub_obj.unsubscribed == [f"job:progress:{JOB_ID}"]
    assert redis.pubsub_obj.closed is True


async def test_snapshot_is_read_after_subscribing_not_before() -> None:
    """Ordering guard: SUBSCRIBE must precede the snapshot GET.

    Reading the snapshot first re-creates the bug in miniature — an event
    published in the gap is in neither the snapshot nor the subscription.
    """
    redis = _FakeRedis(last_event=_event_json("completed", 100))

    await asyncio.wait_for(_drain(subscribe(redis, JOB_ID)), timeout=2)  # type: ignore[arg-type]

    assert redis.calls.index("subscribe") < redis.calls.index("get")


async def test_cancelled_snapshot_is_terminal() -> None:
    """Saga rollbacks cancel jobs; those streams must close too."""
    redis = _FakeRedis(last_event=_event_json("cancelled", 0, "Saga rolled back"))

    events = await asyncio.wait_for(_drain(subscribe(redis, JOB_ID)), timeout=2)  # type: ignore[arg-type]

    assert [e.status for e in events] == ["cancelled"]


async def test_non_terminal_snapshot_yields_first_then_live_messages() -> None:
    """A mid-flight job: the snapshot brings the late subscriber up to date,
    then the live channel takes over and still closes on its terminal event."""
    redis = _FakeRedis(
        last_event=_event_json("running", 40, "Working"),
        messages=[
            {"type": "subscribe", "data": 1},
            {"type": "message", "data": _event_json("running", 80, "Nearly")},
            {"type": "message", "data": _event_json("completed", 100, "Done")},
        ],
    )

    events = await asyncio.wait_for(_drain(subscribe(redis, JOB_ID)), timeout=2)  # type: ignore[arg-type]

    assert [(e.status, e.progress) for e in events] == [
        ("running", 40),
        ("running", 80),
        ("completed", 100),
    ]


async def test_no_snapshot_falls_back_to_live_stream_only() -> None:
    """Eviction (or a job that has never published) degrades to old behaviour."""
    redis = _FakeRedis(
        messages=[{"type": "message", "data": _event_json("failed", 0, "boom")}]
    )

    events = await asyncio.wait_for(_drain(subscribe(redis, JOB_ID)), timeout=2)  # type: ignore[arg-type]

    assert [e.status for e in events] == ["failed"]


async def test_malformed_snapshot_does_not_break_the_stream() -> None:
    """A corrupt retained key is ignored, not raised at the subscriber."""
    redis = _FakeRedis(
        last_event="{not json",
        messages=[{"type": "message", "data": _event_json("completed", 100)}],
    )

    events = await asyncio.wait_for(_drain(subscribe(redis, JOB_ID)), timeout=2)  # type: ignore[arg-type]

    assert [e.status for e in events] == ["completed"]


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
