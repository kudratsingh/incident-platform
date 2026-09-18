"""When the outbox relay last completed a pass, recorded where another process
can read it.

An idle relay and a stopped relay both publish nothing, so delivery alone cannot
tell them apart, and the MCP reader is in another process (ADR 0006). So the stamp
goes inside `_outbox_relay_tick` — not around the loop, which would only mean the
coroutine is alive — once per one-second pass rather than off the once-a-minute
gauge, and outside `chaos:*` so the reset sweep cannot clear it (ADR 0012). The
write fails open (a diagnostic must never cost a tick); the read fails *known*, so
an absent record is reported as unknown with a reason and never as an age.
"""

from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Where the relay records the end of each pass. Plain platform namespace, and
#: listed in `docs/REDIS.md`'s key catalog alongside every other key.
RELAY_TICK_KEY = "outbox:relay:last_tick"

#: Generous on purpose: an expired key downgrades a real age to "unknown",
#: and a day covers any stall worth investigating.
RELAY_TICK_TTL_SECONDS = 86_400

#: Why a tick time is unknown, in the caller's words. Closed set, pinned by a test.
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
    """A Redis handle, imported at call time — like `loop_is_paused`, to avoid a live pool."""
    from app.core.redis import get_redis_client

    return get_redis_client()


async def record_relay_tick(
    redis: Any | None = None, *, now: datetime | None = None
) -> None:
    """Stamp "the relay completed a pass", with the time it happened.

    Best-effort by design; `redis` and `now` are injectable.
    """
    stamp = (now or datetime.now(UTC)).isoformat()
    try:
        client = redis if redis is not None else _client()
        await client.set(RELAY_TICK_KEY, stamp, ex=RELAY_TICK_TTL_SECONDS)
    except Exception as exc:
        # Warning: a lost diagnostic is not a failed pass.
        logger.warning(
            "outbox relay pass not recorded", extra={"error": str(exc)}
        )


async def read_relay_tick(redis: Any) -> tuple[datetime | None, str | None]:
    """`(time of the last completed relay pass, reason it is unknown)`.

    Exactly one side is populated, so the tool can report which kind of unknown
    it has without inventing an age. A stamp with no offset is read as UTC.
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
