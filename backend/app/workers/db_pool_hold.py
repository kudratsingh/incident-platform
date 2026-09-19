"""The `chaos:db_pool:hold` mechanism: a lab task holds pooled connections open (WO-R3-219).

One key, one task, in the worker process — so the pool that starves is the one the API and the
eleven loops share, not the MCP process's own ([ADR 0031]). The clamp always leaves
`MIN_FREE_CONNECTIONS` acquirable, so a held pool slows the loops instead of stopping them.
Teardown is the TTL, the reset's `chaos:*` sweep, or a worker restart.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)

#: The one key. A repeat call replaces the hold rather than stacking a second one on top.
HOLD_KEY = "chaos:db_pool:hold"

#: Ceiling on how many connections one hold may take, and on the hook's `connections` field.
MAX_HELD_CONNECTIONS = 10

#: Connections the runtime clamp always leaves acquirable: the outbox relay every job depends
#: on, the dispatcher's claim, and two spare. A held pool must slow the loops, not stop them.
MIN_FREE_CONNECTIONS = 4

#: How long the task takes to notice a new key, a changed count, or an expiry.
POLL_INTERVAL_SECONDS = 1.0


def hold_key() -> str:
    """The key `saturate_db_pool` writes and this task reads."""
    return HOLD_KEY


async def requested_hold(redis: Any) -> int:
    """How many connections the key asks for: 0 when chaos is off, the key is absent, or the
    value is not a count."""
    if not get_settings().chaos_enabled:
        return 0
    try:
        raw = await redis.get(HOLD_KEY)
    except Exception:
        # Fail open: an unreadable flag releases the hold rather than keeping it.
        logger.warning("db pool hold flag unreadable", exc_info=True)
        return 0
    if raw is None:
        return 0
    try:
        wanted = int(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        logger.warning("db pool hold flag is not a count", extra={"value": str(raw)})
        return 0
    return max(0, min(wanted, MAX_HELD_CONNECTIONS))


def pool_capacity(session_factory: async_sessionmaker[AsyncSession]) -> int | None:
    """Connections this factory's engine can have checked out at once, or `None` if the pool
    cannot say (SQLite in the unit tier)."""
    bind = getattr(session_factory, "kw", {}).get("bind")
    pool = getattr(bind, "pool", None)
    size = getattr(pool, "size", None)
    if not callable(size):
        return None
    try:
        return int(size()) + int(getattr(pool, "_max_overflow", 0) or 0)
    except (TypeError, ValueError):
        return None


def clamp_to_pool(wanted: int, capacity: int | None) -> int:
    """Never hold the last `MIN_FREE_CONNECTIONS`; an unknown capacity clamps to the static cap."""
    bounded = max(0, min(wanted, MAX_HELD_CONNECTIONS))
    if capacity is None:
        return bounded
    return max(0, min(bounded, capacity - MIN_FREE_CONNECTIONS))


async def _acquire_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncSession:
    """One session with its transaction open — one pool connection checked out.

    The statement is a bare `SELECT 1`; the factory the worker hands in declares
    `app.tenant_scope` on begin, so the hold is not refused under the strict policies (ADR 0026).
    """
    session = session_factory()
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        await session.close()
        raise
    return session


async def _release(sessions: list[AsyncSession]) -> None:
    """Give every held connection back. Idempotent: the list is emptied as it goes."""
    while sessions:
        session = sessions.pop()
        try:
            await session.close()
        except Exception:
            logger.warning("held connection would not close", exc_info=True)


async def reconcile_held(
    sessions: list[AsyncSession],
    wanted: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Move the number of held connections towards `wanted`; returns how many are held."""
    while len(sessions) > wanted:
        await _release([sessions.pop()])
    while len(sessions) < wanted:
        try:
            sessions.append(await _acquire_one(session_factory))
        except Exception:
            # The pool refused one; hold what we have rather than spinning on it.
            logger.warning("could not take another connection to hold", exc_info=True)
            break
    return len(sessions)


async def hold_db_pool(
    session_factory: async_sessionmaker[AsyncSession], redis: Any
) -> None:
    """Hold as many pool connections as `chaos:db_pool:hold` asks for, while it asks (ADR 0031).

    Not one of the eleven background loops and deliberately not in `ControlLoopName`: it exists
    only under `CHAOS_ENABLED`, and its off switch is its own key, not `pause_control_loop`.
    """
    if not get_settings().chaos_enabled:
        return
    held: list[AsyncSession] = []
    capacity = pool_capacity(session_factory)
    try:
        while True:
            wanted = clamp_to_pool(await requested_hold(redis), capacity)
            before = len(held)
            now = await reconcile_held(held, wanted, session_factory)
            if now != before:
                logger.warning(
                    "db pool hold changed",
                    extra={
                        "held": now,
                        "requested": wanted,
                        "pool_capacity": capacity,
                    },
                )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await _release(held)


__all__ = [
    "HOLD_KEY",
    "MAX_HELD_CONNECTIONS",
    "MIN_FREE_CONNECTIONS",
    "POLL_INTERVAL_SECONDS",
    "clamp_to_pool",
    "hold_db_pool",
    "hold_key",
    "pool_capacity",
    "reconcile_held",
    "requested_hold",
]
