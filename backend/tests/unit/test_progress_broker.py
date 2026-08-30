"""The SSE fan-out broker: one Pub/Sub connection for every open stream.

Before this module, `GET /jobs/{id}/stream` called `redis.pubsub()` per
viewer and held that subscription — and therefore one connection out of the
single process-wide 20-connection pool — for the whole life of the generator.
`worker_loop` runs in the same process on the same pool, so ~20 parked
dashboards starved the rate limiter, `check_backpressure` and the admin stats
loops that share those slots (WO-R2-11).

Three things are asserted here, in the order they matter:

  1. **Fan-out** — N viewers of one job, and M jobs, consume exactly ONE
     Pub/Sub connection. The viewers↔connections relationship is gone, not
     merely widened.
  2. **Capacity** — the per-process stream cap refuses the extra viewer with
     `StreamCapacityError` (503 + Retry-After) instead of letting it queue
     against a finite pool.
  3. **Liveness** — an idle timeout and a maximum duration end a stream that
     nobody is feeding, so a tab parked on a waiting job cannot pin a slot
     forever.

Plus the fail-open posture the streaming path has always had: a Redis error
ends the stream (the browser's EventSource reconnects), it never escapes as a
500 out of the API.

The snapshot/terminal semantics tests moved here with the code they cover —
`subscribe()` used to live in `workers/progress.py` and is now the broker's
method; `tests/unit/test_progress.py` keeps `publish`/`read_last_event`.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable
from typing import Any

import pytest
from app.core.exceptions import StreamCapacityError
from app.workers.progress import CHANNEL_PREFIX, LAST_EVENT_PREFIX, ProgressEvent
from app.workers.progress_broker import ProgressBroker

JOB_ID = "11111111-1111-1111-1111-111111111111"
OTHER_JOB_ID = "22222222-2222-2222-2222-222222222222"


def _event_json(status: str, progress: int = 0, message: str = "x", job_id: str = JOB_ID) -> str:
    return ProgressEvent(
        job_id=job_id, status=status, progress=progress, message=message
    ).to_json()


def _message(job_id: str, payload: str) -> dict[str, Any]:
    return {"type": "message", "channel": f"{CHANNEL_PREFIX}:{job_id}", "data": payload}


class _FakePubSub:
    """Pub/Sub double: records channel bookkeeping, replays queued messages.

    `get_message` blocks (rather than returning None immediately) when the
    inbox is empty, so a test that finishes finished because the broker
    decided to stop — not because the fake ran dry.
    """

    def __init__(self, owner: "_FakeRedis", messages: Iterable[dict[str, Any]]) -> None:
        self._owner = owner
        self.channels: list[str] = []
        self.subscribe_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []
        self.closed = False
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for message in messages:
            self.inbox.put_nowait(message)

    async def subscribe(self, channel: str) -> None:
        self.channels.append(channel)
        self.subscribe_calls.append(channel)
        self._owner.calls.append("subscribe")

    async def unsubscribe(self, channel: str) -> None:
        if channel in self.channels:
            self.channels.remove(channel)
        self.unsubscribe_calls.append(channel)

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = None
    ) -> dict[str, Any] | None:
        if self._owner.read_error is not None:
            raise self._owner.read_error
        try:
            return await asyncio.wait_for(self.inbox.get(), timeout)
        except TimeoutError:
            return None


class _FakeRedis:
    """Just enough Redis for the broker: get / pubsub, with a call log."""

    def __init__(
        self,
        last_event: str | None = None,
        messages: Iterable[dict[str, Any]] = (),
        get_error: Exception | None = None,
    ) -> None:
        self.store: dict[str, str] = {}
        if last_event is not None:
            self.store[f"{LAST_EVENT_PREFIX}:{JOB_ID}"] = last_event
        self.calls: list[str] = []
        self.get_error = get_error
        self.read_error: Exception | None = None
        self.pubsub_calls = 0
        self.pubsub_obj = _FakePubSub(self, messages)

    def pubsub(self) -> _FakePubSub:
        self.pubsub_calls += 1
        return self.pubsub_obj

    async def get(self, key: str) -> str | None:
        self.calls.append("get")
        if self.get_error is not None:
            raise self.get_error
        return self.store.get(key)


def _broker(redis: _FakeRedis, **overrides: Any) -> ProgressBroker:
    kwargs: dict[str, Any] = {
        "max_streams": 100,
        "idle_timeout_seconds": 30,
        "max_duration_seconds": 300,
        "retry_after_seconds": 5,
    }
    kwargs.update(overrides)
    return ProgressBroker(redis, **kwargs)  # type: ignore[arg-type]


async def _drain(gen: AsyncGenerator[ProgressEvent, None]) -> list[ProgressEvent]:
    return [event async for event in gen]


# ---------------------------------------------------------------------------
# 1. Fan-out — viewers no longer map 1:1 onto connections
# ---------------------------------------------------------------------------


async def test_many_viewers_on_one_job_share_a_single_pubsub_connection() -> None:
    """THE WO-R2-11 assertion: N viewers, one connection.

    Five dashboards on the same job used to mean five `redis.pubsub()` calls
    and five held pool slots. The broker opens one shared Pub/Sub and
    SUBSCRIBEs the channel once.
    """
    redis = _FakeRedis()
    broker = _broker(redis)

    streams = [broker.subscribe(JOB_ID) for _ in range(5)]
    # Prime each generator up to its first await on the fan-out queue.
    tasks = [asyncio.create_task(_drain(s)) for s in streams]
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert redis.pubsub_calls == 1
    assert redis.pubsub_obj.subscribe_calls == [f"{CHANNEL_PREFIX}:{JOB_ID}"]

    # One published event reaches every viewer off that single connection.
    redis.pubsub_obj.inbox.put_nowait(_message(JOB_ID, _event_json("completed", 100)))
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert [[e.status for e in events] for events in results] == [["completed"]] * 5


async def test_multiple_jobs_also_share_the_single_connection() -> None:
    """Two jobs, two channels, still one Pub/Sub connection."""
    redis = _FakeRedis()
    broker = _broker(redis)

    a = asyncio.create_task(_drain(broker.subscribe(JOB_ID)))
    b = asyncio.create_task(_drain(broker.subscribe(OTHER_JOB_ID)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert redis.pubsub_calls == 1
    assert sorted(redis.pubsub_obj.subscribe_calls) == sorted(
        [f"{CHANNEL_PREFIX}:{JOB_ID}", f"{CHANNEL_PREFIX}:{OTHER_JOB_ID}"]
    )

    redis.pubsub_obj.inbox.put_nowait(_message(JOB_ID, _event_json("failed")))
    redis.pubsub_obj.inbox.put_nowait(
        _message(OTHER_JOB_ID, _event_json("completed", 100, job_id=OTHER_JOB_ID))
    )
    events_a, events_b = await asyncio.wait_for(asyncio.gather(a, b), timeout=2)
    assert [e.status for e in events_a] == ["failed"]
    assert [e.status for e in events_b] == ["completed"]


async def test_an_event_is_only_delivered_to_its_own_jobs_viewers() -> None:
    """Fan-out is per-channel — one job's events never leak into another's stream."""
    redis = _FakeRedis()
    broker = _broker(redis)

    watcher = asyncio.create_task(_drain(broker.subscribe(JOB_ID)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    redis.pubsub_obj.inbox.put_nowait(
        _message(OTHER_JOB_ID, _event_json("completed", 100, job_id=OTHER_JOB_ID))
    )
    redis.pubsub_obj.inbox.put_nowait(_message(JOB_ID, _event_json("failed", 0, "mine")))

    events = await asyncio.wait_for(watcher, timeout=2)
    assert [(e.job_id, e.status) for e in events] == [(JOB_ID, "failed")]


async def test_last_viewer_leaving_unsubscribes_and_closes_the_connection() -> None:
    """The shared connection is released when the final stream ends."""
    redis = _FakeRedis(last_event=_event_json("completed", 100))
    broker = _broker(redis)

    await _drain(broker.subscribe(JOB_ID))

    assert redis.pubsub_obj.unsubscribe_calls == [f"{CHANNEL_PREFIX}:{JOB_ID}"]
    assert redis.pubsub_obj.closed is True


# ---------------------------------------------------------------------------
# 2. Capacity — the cap refuses rather than exhausting anything
# ---------------------------------------------------------------------------


async def test_stream_beyond_the_cap_is_refused_with_503_and_retry_after() -> None:
    redis = _FakeRedis()
    broker = _broker(redis, max_streams=2, retry_after_seconds=7)

    first = broker.acquire()
    broker.acquire()

    with pytest.raises(StreamCapacityError) as excinfo:
        broker.acquire()

    assert excinfo.value.status_code == 503
    assert excinfo.value.error_code == "stream_capacity"
    assert excinfo.value.headers == {"Retry-After": "7"}
    assert excinfo.value.details["limit"] == 2

    # A finished stream hands its slot back.
    first.release()
    broker.acquire()


async def test_releasing_a_slot_twice_frees_it_once() -> None:
    """The route releases in the generator's finally AND as a background task."""
    broker = _broker(_FakeRedis(), max_streams=1)

    slot = broker.acquire()
    slot.release()
    slot.release()

    assert broker.active_streams == 0
    broker.acquire()  # exactly one slot came back, not two


async def test_cap_of_zero_disables_the_limit() -> None:
    broker = _broker(_FakeRedis(), max_streams=0)
    for _ in range(50):
        broker.acquire()
    assert broker.active_streams == 50


# ---------------------------------------------------------------------------
# 3. Liveness — no stream lives forever
# ---------------------------------------------------------------------------


async def test_idle_stream_ends_at_the_idle_timeout() -> None:
    """A tab parked on a silent channel must not hold its slot indefinitely."""
    redis = _FakeRedis()
    broker = _broker(redis, idle_timeout_seconds=0.05)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert events == []
    assert redis.pubsub_obj.unsubscribe_calls == [f"{CHANNEL_PREFIX}:{JOB_ID}"]


async def test_busy_stream_still_ends_at_the_maximum_duration() -> None:
    """Traffic resets the idle timer; the hard duration cap is not resettable."""
    redis = _FakeRedis()
    broker = _broker(redis, idle_timeout_seconds=30, max_duration_seconds=0.05)

    async def _chatter() -> None:
        for _ in range(50):
            redis.pubsub_obj.inbox.put_nowait(_message(JOB_ID, _event_json("running", 1)))
            await asyncio.sleep(0.005)

    noise = asyncio.create_task(_chatter())
    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)
    noise.cancel()

    # It ended on the duration cap, not on a terminal event.
    assert all(e.status == "running" for e in events)


# ---------------------------------------------------------------------------
# Snapshot + terminal semantics (moved from test_progress.py with the code)
# ---------------------------------------------------------------------------


async def test_late_subscriber_gets_terminal_snapshot_and_stream_ends() -> None:
    """The job finished before anyone subscribed; the channel stays silent."""
    redis = _FakeRedis(last_event=_event_json("completed", 100, "Done"))
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["completed"]
    assert events[0].message == "Done"


async def test_cancelled_snapshot_is_terminal() -> None:
    """Saga rollbacks cancel jobs; those streams must close too."""
    redis = _FakeRedis(last_event=_event_json("cancelled", 0, "Saga rolled back"))
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["cancelled"]


async def test_snapshot_is_read_after_the_channel_subscription() -> None:
    """SUBSCRIBE then GET — the reverse order drops an event published between."""
    redis = _FakeRedis(last_event=_event_json("completed", 100))
    broker = _broker(redis)

    await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert redis.calls.index("subscribe") < redis.calls.index("get")


async def test_non_terminal_snapshot_is_followed_by_live_events() -> None:
    redis = _FakeRedis(
        last_event=_event_json("running", 10, "warming up"),
        messages=[_message(JOB_ID, _event_json("completed", 100, "done"))],
    )
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["running", "completed"]


async def test_no_snapshot_falls_through_to_the_live_channel() -> None:
    redis = _FakeRedis(messages=[_message(JOB_ID, _event_json("failed", 0, "boom"))])
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["failed"]


async def test_malformed_snapshot_is_ignored_not_fatal() -> None:
    redis = _FakeRedis(messages=[_message(JOB_ID, _event_json("completed", 100))])
    redis.store[f"{LAST_EVENT_PREFIX}:{JOB_ID}"] = "{not json"
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["completed"]


async def test_malformed_live_message_is_discarded_and_the_stream_survives() -> None:
    redis = _FakeRedis(
        messages=[
            _message(JOB_ID, json.dumps({"nonsense": True})),
            _message(JOB_ID, _event_json("completed", 100)),
        ]
    )
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["completed"]


# ---------------------------------------------------------------------------
# Fail-open — Redis trouble degrades the stream, it never 500s the API
# ---------------------------------------------------------------------------


async def test_snapshot_read_failure_does_not_break_the_stream() -> None:
    redis = _FakeRedis(
        get_error=ConnectionError("redis down"),
        messages=[_message(JOB_ID, _event_json("completed", 100))],
    )
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert [e.status for e in events] == ["completed"]


async def test_reader_failure_closes_open_streams_instead_of_raising() -> None:
    """Redis goes away mid-stream: every viewer is closed, nobody sees a 500.

    The browser's EventSource reconnects on its own — that is the documented
    degradation, and it is what the pre-broker code did by letting the
    generator die. What must NOT happen is the exception escaping into the
    request path.
    """
    redis = _FakeRedis()
    redis.read_error = ConnectionError("redis went away")
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert events == []


async def test_subscribe_failure_ends_that_stream_quietly() -> None:
    """SUBSCRIBE itself fails — the stream closes, the caller gets no exception."""
    redis = _FakeRedis()

    async def _boom(channel: str) -> None:
        raise ConnectionError("redis refused SUBSCRIBE")

    redis.pubsub_obj.subscribe = _boom  # type: ignore[method-assign]
    broker = _broker(redis)

    events = await asyncio.wait_for(_drain(broker.subscribe(JOB_ID)), timeout=2)

    assert events == []
