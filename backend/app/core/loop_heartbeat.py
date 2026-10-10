"""When each of the worker's background loops last completed a pass, recorded where another
process can read it (WO-R3-371, ADR 0041).

The outbox relay has had this since ADR 0028 (`outbox:relay:last_tick`), and for the same
reason the other ten need it: a loop that has stopped and a loop with nothing to do both do
nothing, so the absence of their work cannot tell them apart, and the MCP reader is in another
process (ADR 0006). ADR 0027's amendment named the gap — "how long the sweep has been idle" —
when it made the resume sweep pausable; INC-008 is what it cost.

Each loop stamps AFTER its own pause check and after its work, never around the loop body: a
paused loop keeps turning while it skips its work, and a stamp written on every turn would
report it running. Keys are outside `chaos:*`, so the reset's sweep cannot clear them and their
name cannot hint at why a loop stopped (ADR 0012). The write fails open (a diagnostic must never
cost a pass); the read raises, and the tool turns that into an explicit unknown.

Core, not workers: loop names arrive as plain strings (`ControlLoopName` is a `StrEnum`), so this
module imports nothing from the worker package and the MCP process can read it freely.
"""

import time
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.outbox_heartbeat import RELAY_TICK_KEY

logger = get_logger(__name__)

#: `worker:loop:<loop>:last_run`. Plain platform namespace, listed in `docs/REDIS.md`.
LOOP_RUN_KEY_PREFIX = "worker:loop:"
LOOP_RUN_KEY_SUFFIX = ":last_run"

#: Three days: three of the longest default interval (the daily digest), so a loop on any sane
#: schedule is always on record between two passes, and a record nothing refreshes still goes.
LOOP_RUN_TTL_SECONDS = 3 * 86_400

#: A lost stamp is logged at most this often per loop. The fastest loops pass twice a second,
#: and a Redis that refuses writes would otherwise add four warnings a second to an outage.
LOST_STAMP_WARN_INTERVAL_SECONDS = 60.0
_last_warned: dict[str, float] = {}

#: The relay already records every pass it completes (ADR 0028). One pass, one record: its
#: loop reading IS that stamp, so `get_outbox_status` and the loop reading cannot disagree.
_EXISTING_RECORDS = {"outbox_relay": RELAY_TICK_KEY}


def loop_run_key(loop: str) -> str:
    name = str(loop)
    return _EXISTING_RECORDS.get(name, f"{LOOP_RUN_KEY_PREFIX}{name}{LOOP_RUN_KEY_SUFFIX}")


def _client() -> Any:
    """A Redis handle, imported at call time — like `loop_is_paused`, to avoid a live pool."""
    from app.core.redis import get_redis_client

    return get_redis_client()


async def record_loop_run(
    loop: str, redis: Any | None = None, *, now: datetime | None = None
) -> None:
    """Stamp "this loop completed a pass", with the time it happened. Never raises."""
    stamp = (now or datetime.now(UTC)).isoformat()
    try:
        client = redis if redis is not None else _client()
        await client.set(loop_run_key(loop), stamp, ex=LOOP_RUN_TTL_SECONDS)
    except Exception as exc:
        # A lost diagnostic is not a failed pass: the reading it feeds degrades to an older
        # age, which is what an unrecorded pass looks like. Warned, but not on every pass.
        name = str(loop)
        now_mono = time.monotonic()
        if now_mono - _last_warned.get(name, float("-inf")) >= LOST_STAMP_WARN_INTERVAL_SECONDS:
            _last_warned[name] = now_mono
            logger.warning(
                "loop pass not recorded", extra={"loop": name, "error": str(exc)}
            )


async def read_loop_run(redis: Any, loop: str) -> datetime | None:
    """When `loop` last completed a pass, or None when no readable record exists.

    Raises when the store cannot be reached: "no record" and "could not look" are different
    answers, and only the caller can say which one it is giving.
    """
    raw = await redis.get(loop_run_key(loop))
    if raw is None:
        return None
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = [
    "LOST_STAMP_WARN_INTERVAL_SECONDS",
    "LOOP_RUN_KEY_PREFIX",
    "LOOP_RUN_KEY_SUFFIX",
    "LOOP_RUN_TTL_SECONDS",
    "loop_run_key",
    "read_loop_run",
    "record_loop_run",
]
