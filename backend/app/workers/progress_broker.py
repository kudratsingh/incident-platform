"""
One Redis Pub/Sub connection for every open SSE stream in the process.

The problem this replaces
------------------------
`GET /jobs/{id}/stream` used to call `redis.pubsub()` per viewer and hold that
subscription for the life of the generator.  A Pub/Sub subscription owns its
connection while it is subscribed, so the process's open-stream count *was*
its held-connection count — and those connections came out of the single
20-slot pool that `worker_loop`, the rate limiter, `check_backpressure`, the
job cache and the admin stats loops all share in the same process.  The
practical viewer ceiling was well under 20, and past it the failures landed
everywhere except on the viewer who caused them: the rate limiter failed open
silently, `check_backpressure` 500'd `POST /jobs`, admin stats errored.  The
frontend reconnects every 2s until terminal, so one tab parked on a waiting
job pinned a slot indefinitely (WO-R2-11).

The shape of the fix
--------------------
**Fan-out.**  The broker owns exactly ONE Pub/Sub connection for the whole
process.  It SUBSCRIBEs a job's channel when that job's first viewer arrives
and UNSUBSCRIBEs when its last one leaves; a single reader task pumps messages
off that connection into a per-viewer `asyncio.Queue`.  N viewers of one job,
and M jobs, cost one connection — the linear relationship between viewers and
connections is gone rather than widened, which is what makes the ceiling a
policy choice instead of an accident of pool size.

**A dedicated pool.**  That one connection comes from `get_sse_redis_client()`
(see `core/redis.py`), not the shared pool, so even a broker bug cannot reach
the request path's 20 slots.

**A cap.**  `acquire()` reserves a slot up front and raises
`StreamCapacityError` (503 + Retry-After) when the process is full.  The
refusal is addressed to the viewer asking for the stream, which is the whole
point: the old failure mode charged the cost to unrelated callers.

**Timeouts.**  A stream with no event for `SSE_STREAM_IDLE_TIMEOUT_SECONDS`
ends, and no stream outlives `SSE_STREAM_MAX_DURATION_SECONDS`.  EventSource
reconnects on its own, so ending a stream costs a live viewer one reconnect
and reclaims the slot from a dead one.

Fail-open, unchanged
--------------------
Every Redis failure here degrades the stream and nothing else: a snapshot read
that raises is treated as "no snapshot", a SUBSCRIBE that raises closes that
one stream, and a reader that dies closes the streams it was feeding.  None of
them escape into the request path as a 500, and none of them touch the durable
path — job state lives in Postgres, and `api/streaming.py` still short-circuits
a finished job off its `jobs` row.
"""

import asyncio
import json
from collections.abc import AsyncGenerator

from app.config import get_settings
from app.core.exceptions import StreamCapacityError
from app.core.logging import get_logger
from app.core.redis import get_sse_redis_client
from app.workers.progress import (
    CHANNEL_PREFIX,
    TERMINAL_STATUSES,
    ProgressEvent,
    read_last_event,
)
from redis.asyncio import Redis

logger = get_logger(__name__)

# Per-viewer hand-off queue depth. A viewer that cannot keep up loses its
# OLDEST pending events, never the newest — a progress bar is last-write-wins,
# and an unbounded queue would turn a slow client into a memory leak.
QUEUE_MAXSIZE = 64

# How long the reader blocks on the shared connection before looping. Only
# affects how promptly a cancelled reader notices; not a delivery latency.
READ_POLL_SECONDS = 1.0

# Pushed into a viewer's queue to end its stream from the reader side.
_CLOSED = object()

_QueueItem = ProgressEvent | object


def _channel(job_id: str) -> str:
    return f"{CHANNEL_PREFIX}:{job_id}"


def _job_id_from_channel(channel: str | bytes) -> str:
    text = channel.decode() if isinstance(channel, bytes) else channel
    return text.removeprefix(f"{CHANNEL_PREFIX}:")


class StreamSlot:
    """One reserved unit of the per-process stream budget.

    `release()` is idempotent because the route releases from two places: the
    generator's `finally` (the normal path) and a response background task
    (the path where the generator is never driven at all, e.g. the client
    vanishes between the handler returning and the first byte). Whichever runs
    first frees the slot; the second is a no-op.
    """

    __slots__ = ("_broker", "_released")

    def __init__(self, broker: "ProgressBroker") -> None:
        self._broker = broker
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._broker._release_slot()


class ProgressBroker:
    """Shares one Pub/Sub connection across every open stream in the process."""

    def __init__(
        self,
        redis: Redis,
        *,
        max_streams: int,
        idle_timeout_seconds: float,
        max_duration_seconds: float,
        retry_after_seconds: int,
    ) -> None:
        self._redis = redis
        self._max_streams = max_streams
        self._idle_timeout = idle_timeout_seconds
        self._max_duration = max_duration_seconds
        self._retry_after = retry_after_seconds

        self._subscribers: dict[str, set[asyncio.Queue[_QueueItem]]] = {}
        self._pubsub: object | None = None
        self._reader: asyncio.Task[None] | None = None
        # Serialises channel bookkeeping. The reader deliberately does NOT
        # take it: it only reads `_subscribers` and puts into queues, both of
        # which are atomic between awaits on a single event loop, and taking
        # the lock there would deadlock teardown.
        self._lock = asyncio.Lock()
        self._active = 0

    # -- capacity ---------------------------------------------------------

    @property
    def active_streams(self) -> int:
        return self._active

    def acquire(self) -> StreamSlot:
        """Reserve a stream slot, or refuse with 503 + Retry-After.

        Synchronous and allocation-free on purpose: it runs in the request
        handler before any Redis work, so a refusal costs nothing and cannot
        itself be starved by the condition it is refusing.
        """
        if self._max_streams > 0 and self._active >= self._max_streams:
            logger.warning(
                "sse_stream_capacity_rejected",
                extra={"active": self._active, "limit": self._max_streams},
            )
            raise StreamCapacityError(
                f"This process is already streaming its maximum of "
                f"{self._max_streams} concurrent job progress streams; "
                f"retry in {self._retry_after}s.",
                details={
                    "limit": self._max_streams,
                    "retry_after_seconds": self._retry_after,
                },
                headers={"Retry-After": str(self._retry_after)},
            )
        self._active += 1
        return StreamSlot(self)

    def _release_slot(self) -> None:
        self._active = max(0, self._active - 1)

    # -- the stream -------------------------------------------------------

    async def subscribe(self, job_id: str) -> AsyncGenerator[ProgressEvent, None]:
        """Yield this job's progress events until it ends, times out or closes.

        The first event is the retained snapshot (if any), read AFTER the
        channel subscription is live. That ordering is load-bearing and
        unchanged from the per-viewer implementation: subscribe-then-read can
        at worst deliver one event twice (harmless — the UI is last-write-wins
        on a progress bar), while read-then-subscribe leaves a gap in which an
        event published between the two is lost, which is the silent-hang this
        snapshot exists to prevent.
        """
        queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        await self._register(job_id, queue)
        try:
            snapshot = await self._read_snapshot(job_id)
            if snapshot is not None:
                yield snapshot
                if snapshot.status in TERMINAL_STATUSES:
                    return

            loop = asyncio.get_running_loop()
            deadline = (
                loop.time() + self._max_duration if self._max_duration > 0 else None
            )
            while True:
                timeout = self._idle_timeout if self._idle_timeout > 0 else None
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        logger.info(
                            "sse_stream_closed_max_duration",
                            extra={"job_id": job_id, "seconds": self._max_duration},
                        )
                        return
                    timeout = remaining if timeout is None else min(timeout, remaining)

                try:
                    item = await asyncio.wait_for(queue.get(), timeout)
                except TimeoutError:
                    logger.info(
                        "sse_stream_closed_idle",
                        extra={"job_id": job_id, "seconds": self._idle_timeout},
                    )
                    return

                if not isinstance(item, ProgressEvent):
                    return  # the reader closed this fan-out
                yield item
                if item.status in TERMINAL_STATUSES:
                    return
        finally:
            await self._unregister(job_id, queue)

    async def _read_snapshot(self, job_id: str) -> ProgressEvent | None:
        try:
            return await read_last_event(self._redis, job_id)
        except Exception as exc:
            # No snapshot is a supported state (the key has a 1h TTL), so a
            # failed read is just another way of having none.
            logger.warning(
                "sse_snapshot_read_failed",
                extra={"job_id": job_id, "error_type": type(exc).__name__},
            )
            return None

    # -- channel bookkeeping ---------------------------------------------

    async def _register(self, job_id: str, queue: asyncio.Queue[_QueueItem]) -> None:
        async with self._lock:
            first_for_job = job_id not in self._subscribers
            self._subscribers.setdefault(job_id, set()).add(queue)
            try:
                if self._pubsub is None:
                    self._pubsub = self._redis.pubsub()
                if first_for_job:
                    await self._pubsub.subscribe(_channel(job_id))  # type: ignore[attr-defined]
                self._ensure_reader()
            except Exception as exc:
                # Redis is unreachable. Close this one stream; the client
                # reconnects. Never raise — this runs inside the response
                # generator and an exception here is a broken stream at best.
                logger.warning(
                    "sse_subscribe_failed",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )
                self._offer(queue, _CLOSED)

    async def _unregister(self, job_id: str, queue: asyncio.Queue[_QueueItem]) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(job_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    del self._subscribers[job_id]
                    if self._pubsub is not None:
                        try:
                            await self._pubsub.unsubscribe(_channel(job_id))  # type: ignore[attr-defined]
                        except Exception as exc:
                            logger.warning(
                                "sse_unsubscribe_failed",
                                extra={
                                    "job_id": job_id,
                                    "error_type": type(exc).__name__,
                                },
                            )
            if not self._subscribers:
                await self._teardown()

    async def _teardown(self) -> None:
        """Drop the shared connection once nobody is watching anything.

        Caller holds `_lock`. The reader never takes it, so cancelling here
        cannot deadlock.
        """
        reader, self._reader = self._reader, None
        pubsub, self._pubsub = self._pubsub, None
        if reader is not None:
            reader.cancel()
        if pubsub is not None:
            try:
                await pubsub.aclose()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning(
                    "sse_pubsub_close_failed",
                    extra={"error_type": type(exc).__name__},
                )

    # -- the single reader -------------------------------------------------

    def _ensure_reader(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read_loop(self._pubsub))

    async def _read_loop(self, pubsub: object) -> None:
        """Pump the one shared connection into every registered queue."""
        try:
            while True:
                try:
                    message = await pubsub.get_message(  # type: ignore[attr-defined]
                        ignore_subscribe_messages=True, timeout=READ_POLL_SECONDS
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # The connection died. Close every stream it was feeding
                    # rather than leaving them silently open forever; the
                    # browser reconnects and rebuilds the subscription.
                    logger.warning(
                        "sse_broker_read_failed",
                        extra={
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:200],
                        },
                    )
                    self._close_all()
                    return
                if message is None:
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            pass

    def _dispatch(self, message: dict[str, object]) -> None:
        if message.get("type") != "message":
            return
        channel = message.get("channel")
        if not isinstance(channel, str | bytes):
            return
        subscribers = self._subscribers.get(_job_id_from_channel(channel))
        if not subscribers:
            return
        try:
            event = ProgressEvent(**json.loads(message["data"]))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            logger.warning(
                "sse_discarded_malformed_event", extra={"error": str(exc)[:200]}
            )
            return
        for queue in subscribers:
            self._offer(queue, event)

    def _close_all(self) -> None:
        for subscribers in self._subscribers.values():
            for queue in subscribers:
                self._offer(queue, _CLOSED)

    @staticmethod
    def _offer(queue: asyncio.Queue[_QueueItem], item: _QueueItem) -> None:
        """Never block the reader on one slow viewer; drop its oldest event."""
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover — full then empty
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:  # pragma: no cover — single-threaded loop
            pass


# ---------------------------------------------------------------------------
# Process-wide broker
# ---------------------------------------------------------------------------

_broker: ProgressBroker | None = None


def get_broker() -> ProgressBroker:
    """The one broker for this process, built on the dedicated SSE pool."""
    global _broker
    if _broker is None:
        settings = get_settings()
        _broker = ProgressBroker(
            get_sse_redis_client(),
            max_streams=settings.sse_max_concurrent_streams,
            idle_timeout_seconds=settings.sse_stream_idle_timeout_seconds,
            max_duration_seconds=settings.sse_stream_max_duration_seconds,
            retry_after_seconds=settings.sse_retry_after_seconds,
        )
    return _broker


def reset_broker() -> None:
    """Drop the process broker (shutdown; tests that install their own)."""
    global _broker
    _broker = None


def acquire_stream_slot() -> StreamSlot:
    """Reserve a slot, or raise StreamCapacityError (503 + Retry-After)."""
    return get_broker().acquire()


def subscribe(job_id: str) -> AsyncGenerator[ProgressEvent, None]:
    """Stream a job's progress events off the shared Pub/Sub connection."""
    return get_broker().subscribe(job_id)
