"""Single-writer gate for background loops, on a Postgres advisory lock.

`worker_loop` runs inside *every* API replica's lifespan (main.py:117), so
the number of outbox relays running at any moment equals the number of
replicas — two during every ECS rolling deploy, permanently more once
Phase 8 turns on horizontal scaling. The relay's fetch is a plain SELECT,
so each of them reads the same unpublished rows and publishes the whole
backlog again. See ADR 0020 for why this gate, and not row locks or a
lease column.

The lock is *session*-level (`pg_try_advisory_lock`), not transaction
level: a relay tick spans three transactions with Kafka round-trips in
between (fetch / publish / mark), and `pg_advisory_xact_lock` would be
released at the first commit — in the middle of the window it exists to
protect. `app.core.migration_lock` makes the same call for the same
reason, and the two keys are disjoint so the migration run and the relay
never contend.

THE trap this module exists to contain: session-level advisory locks are
scoped to a *connection*, and the async engine hands out pooled ones. Take
the lock on whatever connection `session.execute()` happened to check out
and the next statement may run on a different connection — releasing a
lock the process never held while the real one leaks until that pooled
connection is recycled. So the gate checks out one `AsyncConnection`
explicitly and holds that object for the whole critical section; acquire,
the caller's work, and release all provably run on it.
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

#: ASCII "outbox" (6 bytes — comfortably inside the int64 the
#: one-argument `pg_advisory_*` form takes). Every process that wants to
#: be the single outbox writer must use this exact key.
OUTBOX_RELAY_LOCK_KEY: Final[int] = 0x6F7574626F78

_TRY_ACQUIRE_SQL: Final[TextClause] = text("SELECT pg_try_advisory_lock(:key)")
_RELEASE_SQL: Final[TextClause] = text("SELECT pg_advisory_unlock(:key)")


def resolve_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncEngine | None:
    """The `AsyncEngine` a session factory is bound to, or None.

    The background loops are handed a session factory, never the engine,
    and the gate needs the engine because it must check out a connection
    of its own (a `Session` returns its connection to the pool at every
    commit — see the module docstring). `async_sessionmaker` keeps the
    bind in `.kw`; `AsyncSession.bind` is the documented equivalent and
    is used as the fallback. Unit tests pin both paths so a SQLAlchemy
    upgrade that moves them fails loudly instead of silently disabling
    the gate.
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

    A connection returned to the pool while it still holds the lock would
    wedge every replica out of the relay until the pool happened to
    recycle it. `invalidate()` closes the underlying DBAPI connection, and
    Postgres drops every advisory lock a backend held when that backend
    goes away — so discarding the connection is the safe failure mode.
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

    Non-blocking: a caller that loses the race gets False immediately and
    is expected to skip this tick rather than queue behind the winner —
    the loops poll on an interval, so waiting would only pile ticks up.

    Yields True unconditionally when there is no Postgres behind the
    factory (the SQLite unit/API suites, where the process is alone
    anyway). That keeps the gate invisible to those tiers; the real
    mutual-exclusion proof is
    `backend/tests/integration/test_outbox_relay_concurrency.py`.
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
        # SQLAlchemy 2.0 autobegins on the first execute. Commit it: the
        # lock is session-scoped and outlives transaction boundaries, and
        # leaving the transaction open would park this connection in
        # `idle in transaction` — holding a snapshot back from vacuum —
        # for the whole tick.
        await conn.commit()

        if not acquired:
            # Deliberately no unlock: `pg_advisory_unlock` on a lock this
            # session never took logs a Postgres warning and returns false.
            yield False
            return

        try:
            yield True
        finally:
            await _release(conn, key)
    finally:
        await conn.close()
