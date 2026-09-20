"""Where a circuit breaker's state is recorded so another process can read it.

The registry is in-process and the only breaker is created in the worker, while the MCP
reader is a separate process from the same image (ADR 0006) — so a reader walking the
registry reads its own empty one and reports every breaker closed (ADR 0030). The record is a
platform key outside `chaos:*`, so no `chaos:*` sweep carries it. The write fails open (a
diagnostic must never cost a call); the read fails *known*, so an unreachable store is a
reason and never an empty listing that reads as "nothing is open".

The second half of the file is how the environment reset undoes a breaker the lab opened
(WO-R3-311, ADR 0036). Clearing the record is not enough and deleting it is wrong: the
registry that opened the breaker would write the same failure back, and an absent record is
an unknown rather than a closed breaker. So the reset raises `breaker:reset:at` and rewrites
each record closed, and every breaker honours that signal before it publishes again — which
is what makes `make eval-reset` work without restarting the worker.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: One key per breaker. Plain platform namespace, catalogued in `docs/REDIS.md`.
BREAKER_STATE_KEY_PREFIX = "breaker:state:"

#: Where the environment reset says it happened, so a registry in another process can forget
#: what it remembers from before it (ADR 0036). Its value is a *time*, not a counter: the
#: question a breaker has to answer is whether its own remembered failure is older than the
#: reset, and a counter cannot say. One fixed key rather than a key per reset, so reading it
#: is a GET and not a scan, and so two resets cannot both look current.
BREAKER_RESET_AT_KEY = "breaker:reset:at"

#: The state a reset restores a breaker to. Pinned against `CircuitState.CLOSED.value` by
#: `tests/unit/test_breaker_reset.py`, because the reset writes this string into a record the
#: registry also writes.
BREAKER_STATE_CLOSED = "closed"

#: Generous like the relay heartbeat's (ADR 0028): an expired key downgrades a real state to
#: "absent", and a day covers any outage worth investigating.
BREAKER_STATE_TTL_SECONDS = 86_400

#: A record is rewritten on every state change and, while calls keep flowing, at most this
#: often — so Redis is not in the hot path of a loop whose job is to stop calling something.
BREAKER_REFRESH_INTERVAL_SECONDS = 60.0

#: The classes a failure is reduced to. A message can carry a job id, a URL or the name of
#: whatever injected the fault, and none of that may reach the agent (ADR 0012).
FAILURE_CLASS_TIMEOUT = "timeout"
FAILURE_CLASS_CONNECTION = "connection"
FAILURE_CLASS_OTHER = "other"
FAILURE_CLASSES = (
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_CONNECTION,
    FAILURE_CLASS_OTHER,
)

#: Why no breaker state is known, in the caller's words. Closed set, pinned by a test.
BREAKERS_UNKNOWN_NONE_PUBLISHED = (
    "no breaker has a state record: either this platform registers none, or none has "
    "reported for longer than the platform keeps"
)
BREAKERS_UNKNOWN_UNREACHABLE = (
    "the platform could not reach the store that holds breaker state, so no breaker "
    "state is known"
)
BREAKERS_UNKNOWN_UNREADABLE = (
    "breaker state records exist but could not be read, so no breaker state is known"
)

#: Bounded so one call cannot walk an unbounded keyspace; the namespace holds one key per
#: registered breaker, so a single pass is the normal case.
_SCAN_COUNT = 100


@dataclass(frozen=True, slots=True)
class BreakerRecord:
    """One breaker's state as the process that owns it last recorded it."""

    name: str
    state: str
    failure_count: int
    failure_threshold: int
    recovery_timeout_s: float
    last_state_change_at: datetime | None
    last_failure_at: datetime | None
    last_failure_reason_class: str | None
    recorded_at: datetime


def breaker_key_for(name: str) -> str:
    """The key one breaker's state lives under."""
    return f"{BREAKER_STATE_KEY_PREFIX}{name}"


def classify_failure(exc: BaseException) -> str:
    """Reduce a failure to one of `FAILURE_CLASSES` — never its message."""
    if isinstance(exc, TimeoutError):
        return FAILURE_CLASS_TIMEOUT
    if isinstance(exc, ConnectionError | OSError):
        return FAILURE_CLASS_CONNECTION
    return FAILURE_CLASS_OTHER


def _client() -> Any:
    """A Redis handle, imported at call time to avoid building a pool at import."""
    from app.core.redis import get_redis_client

    return get_redis_client()


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _parse(raw: Any) -> datetime | None:
    """A stored timestamp back to a datetime; a stamp with no offset is read as UTC."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def publish_breaker_state(
    redis: Any | None = None,
    *,
    name: str,
    state: str,
    failure_count: int,
    failure_threshold: int,
    recovery_timeout_s: float,
    last_state_change_at: datetime | None,
    last_failure_at: datetime | None,
    last_failure_reason_class: str | None,
    now: datetime | None = None,
) -> None:
    """Record one breaker's state where another process can read it.

    Best-effort by design: a failed write logs and is dropped, because a lost diagnostic is
    not a failed call. `redis` and `now` are injectable.
    """
    record = {
        "name": name,
        "state": state,
        "failure_count": failure_count,
        "failure_threshold": failure_threshold,
        "recovery_timeout_s": recovery_timeout_s,
        "last_state_change_at": _iso(last_state_change_at),
        "last_failure_at": _iso(last_failure_at),
        "last_failure_reason_class": last_failure_reason_class,
        "recorded_at": _iso(now or datetime.now(UTC)),
    }
    try:
        client = redis if redis is not None else _client()
        await client.set(
            breaker_key_for(name),
            json.dumps(record),
            ex=BREAKER_STATE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "breaker state not recorded",
            extra={"circuit": name, "error": str(exc)},
        )


async def publish_breaker_reset(
    redis: Any | None = None, *, now: datetime | None = None
) -> datetime | None:
    """Say that the environment was reset at this instant, and return that instant.

    The reset raises this before it touches a single state record: raised afterwards, a
    breaker that publishes in between writes its remembered failure over the clean record
    and the reset has made the world worse than leaving the key alone (WO-R3-311).

    Best-effort like the state write — `None` means nothing was recorded, and the caller
    reports having reset nothing rather than claiming a reset it could not signal.
    """
    at = now or datetime.now(UTC)
    try:
        client = redis if redis is not None else _client()
        await client.set(
            BREAKER_RESET_AT_KEY,
            at.isoformat(),
            ex=BREAKER_STATE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("breaker reset not signalled", extra={"error": str(exc)})
        return None
    return at


async def read_breaker_reset_at(redis: Any | None = None) -> datetime | None:
    """When the environment was last reset, or `None` when that is not known.

    Fails open in both directions — an absent key, an unparseable value and an unreachable
    store are all "no reset" — because a breaker that cannot ask must keep the state it
    has. The alternative is a diagnostic that closes a breaker during a Redis outage.
    """
    try:
        client = redis if redis is not None else _client()
        raw = await client.get(BREAKER_RESET_AT_KEY)
    except Exception as exc:
        logger.warning("breaker reset signal unreadable", extra={"error": str(exc)})
        return None
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    return _parse(raw)


def _is_reset_clean(record: BreakerRecord) -> bool:
    """Whether this record already reads as a breaker that has never failed.

    The shape a world audit asserts after a reset: closed, nothing counted, and every
    failure field null. A breaker that opened and closed again on its own is *not* clean —
    it still carries the failure that opened it.
    """
    return (
        record.state == BREAKER_STATE_CLOSED
        and record.failure_count == 0
        and record.last_failure_at is None
        and record.last_failure_reason_class is None
        and record.last_state_change_at is None
    )


async def reset_breaker_states(redis: Any | None = None) -> int:
    """Restore every published breaker to closed, and tell the owning processes to forget.

    Two halves, in this order. The signal goes first, for the reason
    `publish_breaker_reset` gives. Then every record that is not already clean is rewritten
    *closed with the failure fields null*, rather than deleted: a deleted record makes the
    breaker **absent** from `get_circuit_breakers`, and an absence is not a reading (ADR
    0030) — a world audit cannot assert "every breaker closed" about a breaker that is
    missing. The breaker's own yardsticks (`failure_threshold`, `recovery_timeout_s`) are
    carried over, because `failure_count` means nothing without them.

    Returns how many records had to be rewritten; 0 on a world already clean, and 0 when the
    store could not be reached — nothing is claimed that was not done. ADR 0036.
    """
    client = redis if redis is not None else _client()
    at = await publish_breaker_reset(client)
    if at is None:
        return 0

    records, _unknown = await read_breaker_states(client)
    reset = 0
    for record in records:
        if _is_reset_clean(record):
            continue
        await publish_breaker_state(
            client,
            name=record.name,
            state=BREAKER_STATE_CLOSED,
            failure_count=0,
            failure_threshold=record.failure_threshold,
            recovery_timeout_s=record.recovery_timeout_s,
            last_state_change_at=None,
            last_failure_at=None,
            last_failure_reason_class=None,
        )
        reset += 1
    return reset


async def read_breaker_states(
    redis: Any,
) -> tuple[tuple[BreakerRecord, ...], str | None]:
    """`(every breaker with a record, reason none is known)`.

    Exactly one side is populated, so a caller can tell "no breaker is open" from "nothing
    is known" without inventing a state. Sorted by name.
    """
    try:
        keys = await _scan_keys(redis)
    except Exception as exc:
        logger.warning("breaker state keys unreadable", extra={"error": str(exc)})
        return (), BREAKERS_UNKNOWN_UNREACHABLE

    if not keys:
        return (), BREAKERS_UNKNOWN_NONE_PUBLISHED

    records: list[BreakerRecord] = []
    for key in keys:
        try:
            raw = await redis.get(key)
        except Exception as exc:
            logger.warning("breaker state unreadable", extra={"error": str(exc)})
            return (), BREAKERS_UNKNOWN_UNREACHABLE
        record = _record_from(raw)
        if record is not None:
            records.append(record)

    if not records:
        return (), BREAKERS_UNKNOWN_UNREADABLE
    return tuple(sorted(records, key=lambda r: r.name)), None


async def _scan_keys(redis: Any) -> list[str]:
    """Every breaker-state key, walked with SCAN rather than KEYS."""
    cursor = 0
    found: list[str] = []
    while True:
        cursor, batch = await redis.scan(
            cursor, match=f"{BREAKER_STATE_KEY_PREFIX}*", count=_SCAN_COUNT
        )
        found.extend(
            k.decode(errors="replace") if isinstance(k, bytes | bytearray) else str(k)
            for k in batch
        )
        if int(cursor) == 0:
            return sorted(set(found))


def _record_from(raw: Any) -> BreakerRecord | None:
    """One stored record, or `None` when it cannot be read as one."""
    if raw is None:
        return None
    if isinstance(raw, bytes | bytearray):
        raw = raw.decode(errors="replace")
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None

    recorded_at = _parse(loaded.get("recorded_at"))
    name = loaded.get("name")
    state = loaded.get("state")
    if recorded_at is None or not isinstance(name, str) or not isinstance(state, str):
        return None

    return BreakerRecord(
        name=name,
        state=state,
        failure_count=int(loaded.get("failure_count") or 0),
        failure_threshold=int(loaded.get("failure_threshold") or 0),
        recovery_timeout_s=float(loaded.get("recovery_timeout_s") or 0.0),
        last_state_change_at=_parse(loaded.get("last_state_change_at")),
        last_failure_at=_parse(loaded.get("last_failure_at")),
        last_failure_reason_class=(
            loaded["last_failure_reason_class"]
            if isinstance(loaded.get("last_failure_reason_class"), str)
            else None
        ),
        recorded_at=recorded_at,
    )


__all__ = [
    "BREAKERS_UNKNOWN_NONE_PUBLISHED",
    "BREAKERS_UNKNOWN_UNREACHABLE",
    "BREAKERS_UNKNOWN_UNREADABLE",
    "BREAKER_REFRESH_INTERVAL_SECONDS",
    "BREAKER_RESET_AT_KEY",
    "BREAKER_STATE_CLOSED",
    "BREAKER_STATE_KEY_PREFIX",
    "BREAKER_STATE_TTL_SECONDS",
    "FAILURE_CLASSES",
    "FAILURE_CLASS_CONNECTION",
    "FAILURE_CLASS_OTHER",
    "FAILURE_CLASS_TIMEOUT",
    "BreakerRecord",
    "breaker_key_for",
    "classify_failure",
    "publish_breaker_reset",
    "publish_breaker_state",
    "read_breaker_reset_at",
    "read_breaker_states",
    "reset_breaker_states",
]
