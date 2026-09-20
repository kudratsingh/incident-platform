"""Where each process records its own connection pool, so another process can read it.

Every process builds its own engine and its own pool (ADR 0006), so a pool reading taken
inside the process that answered a call describes that process and nothing else — which is why
`saturate_db_pool` holds the worker's connections and `get_postgres_health` went on reporting a
healthy pool (ADR 0030 § what this does not close, ADR 0031's D1). Each process now records its
pool under `pool:state:<process>` on a fixed cadence, the way a breaker records its state: a
platform key outside `chaos:*`, so neither a `chaos:*` sweep nor the environment reset carries
it. The write fails open — a diagnostic must never cost a caller its request — and the read
fails *known*, so an unreachable store is a reason rather than an empty listing that would read
as "every pool is fine" (ADR 0033).

Two things differ from `breaker_state.py`, both because a pool reading is a sample where a
breaker state is a latched fact. The TTL is a minute rather than a day: a pool number from an
hour ago is not a reading, and a process that stops publishing has to drop out of the listing
instead of freezing at its last healthy number. And a publisher task per process is what writes
it, because a pool moves without anything happening that a state-change hook could hang off.
That task is not chaos-gated (unlike the holder it makes visible) and is deliberately not a
member of `ControlLoopName`: `pause_control_loop` must not be able to blind the one reading the
other hook exists to produce.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.db_pool_stats import read_pool_stats
from app.core.logging import get_logger

logger = get_logger(__name__)

#: One key per process. Plain platform namespace, catalogued in `docs/REDIS.md`.
POOL_STATE_KEY_PREFIX = "pool:state:"

#: The process that serves the REST API and runs the worker's consumers and loops. They share
#: one engine and therefore one pool, which is the pool `saturate_db_pool` holds.
PROCESS_API_WORKER = "api_worker"

#: The process that serves the MCP tool surface (ADR 0006), with its own pool.
PROCESS_MCP = "mcp"

#: Closed, and in listing order. A third process declares itself here rather than inventing a
#: name on the wire, because `process` is what a caller keys on.
POOL_PROCESSES = (PROCESS_API_WORKER, PROCESS_MCP)

#: How often each process rewrites its record. Short enough that a fault armed in one process
#: is visible from another inside one agent step, cheap enough to be one SET per process.
POOL_STATE_REFRESH_INTERVAL_SECONDS = 10.0

#: Six cadences. Generous enough to survive a slow pass or a brief Redis outage, short enough
#: that a wedged process stops being a reading rather than reporting its last healthy pool
#: forever — the opposite trade-off from `BREAKER_STATE_TTL_SECONDS`, for the reason the module
#: docstring gives.
POOL_STATE_TTL_SECONDS = 60

#: Why no pool gauge is known, in the caller's words. Closed set, pinned by a test.
POOL_GAUGES_UNKNOWN_NONE_PUBLISHED = (
    "no process has reported its connection pool: either this platform publishes none, or "
    "none has reported for longer than the platform keeps"
)
POOL_GAUGES_UNKNOWN_UNREACHABLE = (
    "the platform could not reach the store that holds per-process pool readings, so no "
    "process's pool is known"
)
POOL_GAUGES_UNKNOWN_UNREADABLE = (
    "per-process pool readings exist but could not be read, so no process's pool is known"
)

#: Bounded so one call cannot walk an unbounded keyspace; the namespace holds one key per
#: declared process, so a single pass is the normal case.
_SCAN_COUNT = 100

#: The one publisher this process runs. Module-level for the same reason the metrics emitter's
#: is: one process, one background task, started from a lifespan and stopped with it.
_gauge_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class PoolRecord:
    """One process's pool, as that process last reported it."""

    process: str
    size: int
    checked_out: int
    overflow: int
    max_overflow: int | None
    wait_timeouts_1m: int
    written_at: datetime


def pool_key_for(process: str) -> str:
    """The key one process's pool reading lives under.

    Refuses an undeclared process: a name that is not in `POOL_PROCESSES` would publish a
    reading no caller's vocabulary covers, and the failure belongs at the write.
    """
    if process not in POOL_PROCESSES:
        raise ValueError(
            f"unknown process {process!r}; declare it in POOL_PROCESSES first"
        )
    return f"{POOL_STATE_KEY_PREFIX}{process}"


def _client() -> Any:
    """A Redis handle, imported at call time to avoid building a pool at import."""
    from app.core.redis import get_redis_client

    return get_redis_client()


def _parse(raw: Any) -> datetime | None:
    """A stored timestamp back to a datetime; a stamp with no offset is read as UTC."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def publish_pool_state(
    redis: Any | None = None,
    *,
    process: str,
    size: int,
    checked_out: int,
    overflow: int,
    max_overflow: int | None,
    wait_timeouts_1m: int,
    now: datetime | None = None,
) -> None:
    """Record one process's pool where another process can read it.

    Best-effort by design: a failed write logs and is dropped, because a lost diagnostic is
    not a failed request. `redis` and `now` are injectable.
    """
    record = {
        "process": process,
        "size": size,
        "checked_out": checked_out,
        "overflow": overflow,
        "max_overflow": max_overflow,
        "wait_timeouts_1m": wait_timeouts_1m,
        "written_at": (now or datetime.now(UTC)).isoformat(),
    }
    try:
        client = redis if redis is not None else _client()
        await client.set(
            pool_key_for(process),
            json.dumps(record),
            ex=POOL_STATE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "pool state not recorded",
            extra={"pool_process": process, "error": str(exc)},
        )


async def record_process_pool(
    pool: Any,
    *,
    process: str,
    redis: Any | None = None,
    now: datetime | None = None,
) -> bool:
    """Publish one pass of this process's pool; `False` when the pool keeps no counters.

    A pool kind with no counters (`StaticPool` under tests, `NullPool` anywhere) publishes
    **nothing** rather than a record of nulls: a record nobody can fill is not a reading, and
    the read surface says an absent process is not a claim about that process.
    """
    stats, _unknown = read_pool_stats(pool)
    if stats is None:
        return False
    await publish_pool_state(
        redis,
        process=process,
        size=stats.size,
        checked_out=stats.checked_out,
        overflow=stats.overflow,
        max_overflow=stats.max_overflow,
        wait_timeouts_1m=stats.wait_timeouts_1m,
        now=now,
    )
    return True


async def read_pool_states(redis: Any) -> tuple[tuple[PoolRecord, ...], str | None]:
    """`(every process with a pool reading, reason none is known)`.

    Exactly one side is populated, so a caller can tell "no process reports a busy pool" from
    "nothing is known" without inventing a number. Sorted in `POOL_PROCESSES` order, with any
    name outside it last so a stale key from an older release cannot reorder the list.
    """
    try:
        keys = await _scan_keys(redis)
    except Exception as exc:
        logger.warning("pool state keys unreadable", extra={"error": str(exc)})
        return (), POOL_GAUGES_UNKNOWN_UNREACHABLE

    if not keys:
        return (), POOL_GAUGES_UNKNOWN_NONE_PUBLISHED

    records: list[PoolRecord] = []
    for key in keys:
        try:
            raw = await redis.get(key)
        except Exception as exc:
            logger.warning("pool state unreadable", extra={"error": str(exc)})
            return (), POOL_GAUGES_UNKNOWN_UNREACHABLE
        record = _record_from(raw)
        if record is not None:
            records.append(record)

    if not records:
        return (), POOL_GAUGES_UNKNOWN_UNREADABLE
    return tuple(sorted(records, key=_listing_order)), None


def _listing_order(record: PoolRecord) -> tuple[int, str]:
    """`POOL_PROCESSES` order, then name — an unknown name sorts last rather than raising."""
    try:
        return POOL_PROCESSES.index(record.process), record.process
    except ValueError:
        return len(POOL_PROCESSES), record.process


async def _scan_keys(redis: Any) -> list[str]:
    """Every pool-state key, walked with SCAN rather than KEYS."""
    cursor = 0
    found: list[str] = []
    while True:
        cursor, batch = await redis.scan(
            cursor, match=f"{POOL_STATE_KEY_PREFIX}*", count=_SCAN_COUNT
        )
        found.extend(
            k.decode(errors="replace") if isinstance(k, bytes | bytearray) else str(k)
            for k in batch
        )
        if int(cursor) == 0:
            return sorted(set(found))


def _record_from(raw: Any) -> PoolRecord | None:
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

    process = loaded.get("process")
    written_at = _parse(loaded.get("written_at"))
    if not isinstance(process, str) or written_at is None:
        # No timestamp means no age can be stated, and an age is what tells a reader whether
        # to trust the number — so this is unreadable rather than a reading with a made-up time.
        return None

    max_overflow = loaded.get("max_overflow")
    return PoolRecord(
        process=process,
        size=int(loaded.get("size") or 0),
        checked_out=int(loaded.get("checked_out") or 0),
        overflow=int(loaded.get("overflow") or 0),
        max_overflow=int(max_overflow) if isinstance(max_overflow, int) else None,
        wait_timeouts_1m=int(loaded.get("wait_timeouts_1m") or 0),
        written_at=written_at,
    )


async def _gauge_loop(
    process: str,
    pool_getter: Callable[[], Any],
    redis: Any | None,
    interval: float,
) -> None:
    """Publish this process's pool now, then once per `interval`, until cancelled.

    Now rather than after the first sleep: a gauge that appears one interval into boot is
    absent for exactly as long as a scenario takes to arm a fault. Every error is swallowed —
    a task that dies on the first Redis blip is a gauge that never comes back without a
    restart.
    """
    while True:
        try:
            await record_process_pool(pool_getter(), process=process, redis=redis)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "pool gauge pass failed",
                extra={"pool_process": process, "error": str(exc)},
            )
        await asyncio.sleep(interval)


async def start_pool_gauge(
    *,
    process: str,
    pool_getter: Callable[[], Any],
    redis: Any | None = None,
    interval: float = POOL_STATE_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Start this process's pool publisher. Idempotent.

    `pool_getter` is called on every pass rather than resolved once, so a process whose engine
    is built after boot still publishes. The process name is validated here, before the task
    exists, so an undeclared one fails at boot instead of leaving a gauge that never appears.
    """
    global _gauge_task

    pool_key_for(process)
    if _gauge_task is not None and not _gauge_task.done():
        return

    _gauge_task = asyncio.create_task(_gauge_loop(process, pool_getter, redis, interval))
    logger.info(
        "pool gauge started",
        extra={"pool_process": process, "interval_seconds": interval},
    )


async def stop_pool_gauge() -> None:
    """Cancel this process's pool publisher. Safe on one that never started."""
    global _gauge_task

    task, _gauge_task = _gauge_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = [
    "POOL_GAUGES_UNKNOWN_NONE_PUBLISHED",
    "POOL_GAUGES_UNKNOWN_UNREACHABLE",
    "POOL_GAUGES_UNKNOWN_UNREADABLE",
    "POOL_PROCESSES",
    "POOL_STATE_KEY_PREFIX",
    "POOL_STATE_REFRESH_INTERVAL_SECONDS",
    "POOL_STATE_TTL_SECONDS",
    "PROCESS_API_WORKER",
    "PROCESS_MCP",
    "PoolRecord",
    "pool_key_for",
    "publish_pool_state",
    "read_pool_states",
    "record_process_pool",
    "start_pool_gauge",
    "stop_pool_gauge",
]
