"""The `chaos:db_query:slow` mechanism: a lab task keeps a long query running (WO-R3-218).

One key, one task, in the worker process — so the slow queries run where the platform's own work
runs, and the read surface sees them in `pg_stat_activity` rather than in any pool counter
([ADR 0034]). Two sleepers offset by half a chunk keep the oldest query in flight past the slow
threshold at every instant. Teardown is the TTL, the reset's `chaos:*` sweep, or a restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from app.workers.db_pool_hold import MIN_FREE_CONNECTIONS, pool_capacity
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)

#: The one key. A repeat call replaces the fault rather than stacking a second one on top.
SLOW_QUERY_KEY = "chaos:db_query:slow"

#: The declared scope: which of the platform's hot read relations is the slow one. A target
#: outside this map is refused by the tool and reads as "off" here, never matched against nothing.
TARGET_RELATIONS: dict[str, str] = {
    "job_reads": "jobs",
    "audit_reads": "audit_logs",
    "outbox_reads": "outbox_events",
}

#: Two sleepers, offset by half a chunk. Not a caller dial: one sleeper makes the reading a
#: sawtooth through the threshold, and more would eat into the pool's free floor.
SLEEPER_COUNT = 2

#: Bounds on one chunk, in milliseconds. The floor is above twice `SLOW_QUERY_THRESHOLD_MS` with
#: margin for scheduling jitter, which is what makes the reading continuous; the ceiling is the
#: residue an in-flight sleep leaves behind once the flag has gone.
MIN_QUERY_MS = 1100
MAX_QUERY_MS = 10_000
DEFAULT_QUERY_MS = 2000

#: How long the task takes to notice a new key, a changed target, or an expiry.
POLL_INTERVAL_SECONDS = 1.0


def slow_query_key() -> str:
    """The key `slow_db_queries` writes and this task reads."""
    return SLOW_QUERY_KEY


@dataclass(frozen=True)
class SlowQueryRequest:
    """What the flag asks for: one declared relation, one chunk length in milliseconds."""

    target: str
    query_ms: int


def encode_request(target: str, query_ms: int) -> str:
    """The flag's value, written by the hook and parsed by `parse_request`."""
    return f"{target}:{query_ms}"


def parse_request(raw: Any) -> SlowQueryRequest | None:
    """The flag's value, or `None` for absent, unreadable, unknown-target or out-of-bounds.

    The target is looked up in `TARGET_RELATIONS` and never interpolated from the value, so a
    flag written by hand cannot reach the statement text.
    """
    if raw is None:
        return None
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    target, _, chunk = value.partition(":")
    if target not in TARGET_RELATIONS:
        logger.warning("slow query flag names no declared target", extra={"value": value})
        return None
    try:
        query_ms = int(chunk)
    except (TypeError, ValueError):
        logger.warning("slow query flag carries no chunk length", extra={"value": value})
        return None
    if not MIN_QUERY_MS <= query_ms <= MAX_QUERY_MS:
        logger.warning("slow query flag is out of bounds", extra={"query_ms": query_ms})
        return None
    return SlowQueryRequest(target=target, query_ms=query_ms)


async def requested_slow_query(redis: Any) -> SlowQueryRequest | None:
    """What the key asks for: `None` when chaos is off, the key is absent, or the value is not one
    this module wrote."""
    if not get_settings().chaos_enabled:
        return None
    try:
        raw = await redis.get(SLOW_QUERY_KEY)
    except Exception:
        # Fail open: an unreadable flag ends the fault rather than keeping it.
        logger.warning("slow query flag unreadable", exc_info=True)
        return None
    return parse_request(raw)


def sleeper_budget(capacity: int | None) -> int:
    """`SLEEPER_COUNT`, or 0 when the pool cannot spare that many above the free floor."""
    if capacity is None:
        return SLEEPER_COUNT
    return SLEEPER_COUNT if capacity - MIN_FREE_CONNECTIONS >= SLEEPER_COUNT else 0


def statement_for(target: str) -> TextClause:
    """The slow statement for one declared target: a real read of that relation, held open by a
    server-side sleep so its age is what `pg_stat_activity` reports."""
    relation = TARGET_RELATIONS[target]
    return text(
        "SELECT pg_sleep(CAST(:seconds AS double precision)) AS waited, "
        f"(SELECT count(*) FROM {relation}) AS rows_read"
    )


async def run_one_slow_query(
    session_factory: async_sessionmaker[AsyncSession], request: SlowQueryRequest
) -> bool:
    """One chunk: open a session, hold one query open for `query_ms`, give the connection back.

    Its own session, opened and closed here. A swallowed DB error must never reach a caller's
    transaction (R2-59, `CLAUDE.md`), and the way to guarantee that is never to borrow one.
    """
    session = session_factory()
    try:
        await session.execute(
            statement_for(request.target), {"seconds": request.query_ms / 1000}
        )
        return True
    except Exception:
        logger.warning("slow query would not run", exc_info=True)
        return False
    finally:
        try:
            await session.close()
        except Exception:
            logger.warning("slow query session would not close", exc_info=True)


async def _sleeper(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Any,
    *,
    index: int,
    budget: int,
) -> None:
    """One continuous sleeper; `index` is both its phase within a chunk and its place in the
    budget."""
    armed = False
    while True:
        request = await requested_slow_query(redis)
        if request is None or index >= budget:
            if armed:
                logger.warning("slow queries stopped", extra={"sleeper": index})
            armed = False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        if not armed:
            armed = True
            logger.warning(
                "slow queries started",
                extra={
                    "sleeper": index,
                    "target": request.target,
                    "query_ms": request.query_ms,
                },
            )
            # Phase this sleeper within the chunk: with `SLEEPER_COUNT` sleepers evenly offset,
            # the oldest query in flight is never younger than a chunk's (n-1)/n.
            offset = request.query_ms / 1000 * index / SLEEPER_COUNT
            if offset:
                await asyncio.sleep(offset)
        if not await run_one_slow_query(session_factory, request):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def run_slow_queries(
    session_factory: async_sessionmaker[AsyncSession], redis: Any
) -> None:
    """Keep a long query running while `chaos:db_query:slow` asks for one (ADR 0034).

    Not one of the eleven background loops and deliberately not in `ControlLoopName`: it exists
    only under `CHAOS_ENABLED`, and its off switch is its own key, not `pause_control_loop`.
    """
    if not get_settings().chaos_enabled:
        return
    budget = sleeper_budget(pool_capacity(session_factory))
    if budget == 0:
        logger.warning(
            "slow query task idle: the pool cannot spare a connection above the free floor",
            extra={"min_free_connections": MIN_FREE_CONNECTIONS},
        )
        return
    sleepers = [
        asyncio.create_task(_sleeper(session_factory, redis, index=i, budget=budget))
        for i in range(SLEEPER_COUNT)
    ]
    try:
        await asyncio.gather(*sleepers)
    finally:
        for task in sleepers:
            task.cancel()
        await asyncio.gather(*sleepers, return_exceptions=True)


__all__ = [
    "DEFAULT_QUERY_MS",
    "MAX_QUERY_MS",
    "MIN_QUERY_MS",
    "POLL_INTERVAL_SECONDS",
    "SLEEPER_COUNT",
    "SLOW_QUERY_KEY",
    "TARGET_RELATIONS",
    "SlowQueryRequest",
    "encode_request",
    "parse_request",
    "requested_slow_query",
    "run_one_slow_query",
    "run_slow_queries",
    "sleeper_budget",
    "slow_query_key",
    "statement_for",
]
