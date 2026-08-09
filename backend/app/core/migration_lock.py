"""Session-level Postgres advisory lock taken around the alembic run.

Lives in app.core (not alembic/env.py) so tests can import it: env.py
executes the migration runner at import time and must never be imported.

Every ECS backend task runs `alembic upgrade head` from
scripts/entrypoint.sh, so at any scale-out beyond one task the migrations
run concurrently and race on Postgres's `pg_type` uniqueness under
`CREATE TABLE` — the same race docker-compose already avoids by putting
migrations behind a single `migrate` one-shot. These helpers make the
entrypoint's long-standing "concurrent task startups are safe" promise
true: the second task blocks in `pg_advisory_lock` until the first is
done, then re-reads `alembic_version` and no-ops if it is already at head.

Session-level, not `pg_advisory_xact_lock`: alembic manages its own
transaction boundaries inside `run_migrations`, so a transaction-scoped
lock would be released at the first internal commit — in the middle of
the window it exists to protect. A session lock is held until it is
explicitly released or the connection closes, which also makes it
crash-safe: a task that dies mid-migration drops its connection and the
lock goes with it.

Both helpers are no-ops on non-Postgres dialects; alembic can be pointed
at SQLite ad-hoc and `SELECT pg_advisory_lock` would explode there.
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import TextClause

#: ASCII "alembic" (7 bytes, so it fits comfortably in the int64 the
#: pg_advisory_* one-argument form takes). Any process wanting to
#: serialize against the migration run must use this same key.
MIGRATION_LOCK_KEY: Final[int] = 0x616C656D626963

_ACQUIRE_SQL: Final[TextClause] = text("SELECT pg_advisory_lock(:key)")
_RELEASE_SQL: Final[TextClause] = text("SELECT pg_advisory_unlock(:key)")


def _execute_preserving_transaction_state(
    connection: Connection, statement: TextClause
) -> None:
    """Run ``statement`` and leave the connection's transaction state as found.

    This is load-bearing, not tidiness. ``MigrationContext`` snapshots
    ``connection.in_transaction()`` at ``context.configure()`` time; when it
    finds a transaction it did not open it flags it external and downgrades
    ``begin_transaction()`` to a null context, so nothing ever commits the
    migration. SQLAlchemy 2.0 autobegins on the first ``execute()``, so
    taking the lock on a fresh connection would silently trip exactly that
    and every migration would roll back on connection close.

    Committing the implicitly-opened transaction is safe for the lock
    itself: session-level advisory locks are independent of transaction
    boundaries. When the caller already had a transaction open we leave it
    alone — ending someone else's transaction would be worse.
    """
    caller_had_transaction = connection.in_transaction()
    connection.execute(statement, {"key": MIGRATION_LOCK_KEY})
    if not caller_had_transaction and connection.in_transaction():
        connection.commit()


def acquire_migration_lock(connection: Connection) -> bool:
    """Block until this connection holds the migration advisory lock.

    Returns True when the lock was taken (and therefore must be released),
    False on non-Postgres dialects, where nothing is executed at all.
    """
    if connection.dialect.name != "postgresql":
        return False
    _execute_preserving_transaction_state(connection, _ACQUIRE_SQL)
    return True


def release_migration_lock(connection: Connection) -> None:
    """Release the migration advisory lock held by this connection.

    Only meaningful after :func:`acquire_migration_lock` returned True on
    the *same* connection — advisory locks are session-scoped.
    """
    if connection.dialect.name != "postgresql":
        return
    _execute_preserving_transaction_state(connection, _RELEASE_SQL)
