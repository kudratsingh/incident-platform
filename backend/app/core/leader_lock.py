"""Single-writer gate for background loops, on a Postgres advisory lock.

`worker_loop` runs in every API replica, so without this every replica's outbox
relay republishes the same backlog (ADR 0020 — not row locks, not a lease column).
`pg_try_advisory_lock` is *session*-level because a relay tick spans three
transactions; the xact form would release at the first commit. THE trap: session
locks are per *connection* and the engine pools them, so the gate checks out one
`AsyncConnection` and runs acquire, the caller's work and release all on it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from app.core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.sql.elements import TextClause

logger = get_logger(__name__)

#: ASCII "outbox". Every single-outbox-writer process must use this key.
OUTBOX_RELAY_LOCK_KEY: Final[int] = 0x6F7574626F78

_TRY_ACQUIRE_SQL: Final[TextClause] = text("SELECT pg_try_advisory_lock(:key)")
_RELEASE_SQL: Final[TextClause] = text("SELECT pg_advisory_unlock(:key)")


def resolve_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncEngine | None:
    """The `AsyncEngine` a session factory is bound to, or None.

    The loops hold a factory, but the gate needs the engine to check out its own
    connection. Bind comes from `.kw`, else `AsyncSession.bind`; tests pin both.
    """
    bind = getattr(session_factory, "kw", {}).get("bind")
    if isinstance(bind, AsyncEngine):
        return bind
    try:
        bind = session_factory().bind
    except Exception:  # pragma: no cover - defensive; mocks land above
        return None
    return bind if isinstance(bind, AsyncEngine) else None


async def _release(conn: AsyncConnection, key: int) -> None:
    """Give the lock back, or throw the connection away trying.

    A pooled connection still holding the lock wedges every replica out of
    the relay; `invalidate()` drops it, and Postgres frees its locks.
    """
    try:
        await conn.execute(_RELEASE_SQL, {"key": key})
        await conn.commit()
    except BaseException:
        logger.error(
            "leader advisory lock release failed; discarding the connection",
            extra={"lock_key": key},
        )
        await conn.invalidate()
        raise


@asynccontextmanager
async def advisory_leader_lock(
    session_factory: async_sessionmaker[AsyncSession],
    key: int,
) -> AsyncIterator[bool]:
    """Yield True to exactly one holder of `key` at a time.

    Non-blocking: a loser gets False at once and skips this tick. Yields
    True unconditionally with no Postgres behind the factory (SQLite suites);
    the proof is `backend/tests/integration/test_outbox_relay_concurrency.py`.
    """
    engine = resolve_engine(session_factory)
    if engine is None:
        logger.warning(
            "leader lock disabled: no AsyncEngine behind the session factory"
        )
        yield True
        return
    if engine.dialect.name != "postgresql":
        yield True
        return

    conn = await engine.connect()
    try:
        result = await conn.execute(_TRY_ACQUIRE_SQL, {"key": key})
        acquired = bool(result.scalar())
        # SQLAlchemy autobegins on first execute; commit it — an open
        # transaction parks this connection `idle in transaction` all tick.
        await conn.commit()

        if not acquired:
            # No unlock: `pg_advisory_unlock` on a lock never taken warns.
            yield False
            return

        try:
            yield True
        finally:
            await _release(conn, key)
    finally:
        await conn.close()
