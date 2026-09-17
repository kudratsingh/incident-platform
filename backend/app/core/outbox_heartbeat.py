"""When the outbox relay last completed a pass, recorded where another process
can read it.

The relay is a loop inside the worker process (`_outbox_relay_loop`). From
outside that process its work is visible only through what it has already
delivered — which is enough while events are flowing and useless when the queue
is empty. An idle relay and a stopped relay both publish nothing, so "nothing
was delivered recently" cannot tell them apart, and that ambiguity is exactly
what a caller investigating a queue that is not moving has to resolve. One key,
written by the relay on every pass it completes, separates the two. It is also
the only way a reader in the MCP process can observe the loop at all: the two
run in different processes from the same image (ADR 0006), so nothing
in-memory crosses between them.

Three deliberate choices, each of which the obvious alternative gets wrong:

  * **Written by the work, not by the loop around it.** The stamp goes inside
    `_outbox_relay_tick`, after the fetch that proves the queue was reachable.
    Written higher up — before the leader gate or before the per-iteration skip
    check — it would mean "the coroutine is alive", and a loop that spins
    without doing its pass is precisely the state this signal exists to expose.
  * **Derived from the pass, not from a gauge.** `_emit_outbox_gauges` runs at
    most once a minute, so a heartbeat read off it would have a 60-second
    resolution and could not distinguish a relay that stopped 5 seconds ago
    from one running normally. The relay polls every second, so a per-pass
    stamp gives the reading a one-second resolution instead.
  * **Not under `chaos:*`.** This is a platform signal with a platform writer.
    The reset script's `chaos:*` sweep must not clear it, and the name of the
    key that records *that* a relay is not running must not hint at *why*
    (ADR 0012).

Failure posture differs by direction, on purpose. The **write** fails open,
matching `control_loop_pause.loop_is_paused`: the relay exists to deliver
events, and a diagnostic write must never cost a tick. The **read** fails
*known*: an absent or unparseable record is reported as unknown together with
the reason, never as an age. A fabricated `0` would read as a relay that had
ticked at the instant the caller asked, which is the one answer this signal
must never give.
"""

from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Where the relay records the end of each pass. Plain platform namespace, and
#: listed in `docs/REDIS.md`'s key catalog alongside every other key.
RELAY_TICK_KEY = "outbox:relay:last_tick"

#: Generous on purpose. This value is evidence about a loop that may have been
#: stopped for a long time, and an expired key downgrades a real age to
#: "unknown" — honest, but weaker evidence than a number. A day covers any
#: stall worth investigating, and one ~32-byte key refreshed once a second
#: costs nothing.
RELAY_TICK_TTL_SECONDS = 86_400

#: Why a tick time is unknown, in the words the caller is handed. Constants
#: rather than inline strings so the set is closed, pinned by a test, and
#: cannot drift between the reader and the tool that reports it.
TICK_UNKNOWN_NO_RECORD = (
    "no completed relay pass is on record: either none has run since this "
    "platform started, or the last one is older than the platform keeps"
)
TICK_UNKNOWN_UNREADABLE = (
    "a relay pass is on record but its time could not be read, so its age is "
    "unknown"
)
TICK_UNKNOWN_UNREACHABLE = (
    "the platform could not reach the store that holds the relay pass time, so "
    "its age is unknown"
)


def _client() -> Any:
    """A Redis handle, imported at call time.

    Deferred for the same reason `loop_is_paused` defers it: a unit path that
    exercises a loop must not have to stand up a Redis client, and the worker
    package is imported in contexts where the pool has not been built.
    """
    from app.core.redis import get_redis_client

    return get_redis_client()


async def record_relay_tick(
    redis: Any | None = None, *, now: datetime | None = None
) -> None:
    """Stamp "the relay completed a pass", with the time it happened.

    Best-effort by design — see the module docstring on the split failure
    posture. `redis` and `now` are injectable so a test can drive both sides of
    the key without a live client or a real clock.
    """
    stamp = (now or datetime.now(UTC)).isoformat()
    try:
        client = redis if redis is not None else _client()
        await client.set(RELAY_TICK_KEY, stamp, ex=RELAY_TICK_TTL_SECONDS)
    except Exception as exc:
        # Warning, not error: the relay did its job, and the loop must not
        # treat a lost diagnostic as a failed pass.
        logger.warning(
            "outbox relay pass not recorded", extra={"error": str(exc)}
        )


async def read_relay_tick(redis: Any) -> tuple[datetime | None, str | None]:
    """`(time of the last completed relay pass, reason it is unknown)`.

    Exactly one side is populated: a time with no reason, or a reason with no
    time. That shape is what lets the caller-facing tool report "unknown, and
    here is which kind of unknown" without ever inventing an age.

    A stamp with no offset can only come from a writer that dropped it; the
    platform's clock is UTC everywhere, so it is read as UTC rather than an
    otherwise good record being discarded.
    """
    try:
        raw = await redis.get(RELAY_TICK_KEY)
    except Exception as exc:
        logger.warning(
            "outbox relay pass time unreadable", extra={"error": str(exc)}
        )
        return None, TICK_UNKNOWN_UNREACHABLE

    if raw is None:
        return None, TICK_UNKNOWN_NO_RECORD
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None, TICK_UNKNOWN_UNREADABLE

    return (
        parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    ), None


__all__ = [
    "RELAY_TICK_KEY",
    "RELAY_TICK_TTL_SECONDS",
    "TICK_UNKNOWN_NO_RECORD",
    "TICK_UNKNOWN_UNREACHABLE",
    "TICK_UNKNOWN_UNREADABLE",
    "read_relay_tick",
    "record_relay_tick",
]
