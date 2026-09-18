"""Session-level Postgres advisory lock taken around the alembic run.

Lives in app.core (not alembic/env.py) so tests can import it: env.py runs the
migration runner at import time. Every ECS task runs `alembic upgrade head`, so
past one task they race on `pg_type` uniqueness; the second now blocks and then
no-ops at head. Session-level, not `pg_advisory_xact_lock`, because alembic
commits inside `run_migrations` — and that also makes it crash-safe. Both
helpers no-op on non-Postgres dialects.
"""

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Connection, Row
from sqlalchemy.sql.elements import TextClause

#: ASCII "alembic". Any process serializing against the migration run
#: must use this same key.
MIGRATION_LOCK_KEY: Final[int] = 0x616C656D626963

_ACQUIRE_SQL: Final[TextClause] = text("SELECT pg_advisory_lock(:key)")
_RELEASE_SQL: Final[TextClause] = text("SELECT pg_advisory_unlock(:key)")


def execute_preserving_transaction_state(
    connection: Connection,
    statement: TextClause,
    params: dict[str, Any] | None = None,
) -> Sequence[Row[Any]]:
    """Run ``statement`` and leave the connection's transaction state as found.

    Load-bearing: ``MigrationContext`` snapshots ``in_transaction()`` at
    ``context.configure()`` and treats a transaction it did not open as external,
    so nothing ever commits the migration. Public because the migration env needs
    the same guarantee for its role preflight (WO-R2-67).
    """
    caller_had_transaction = connection.in_transaction()
    # Rows are drained before any commit: committing closes the result.
    rows = connection.execute(statement, params or {}).all()
    if not caller_had_transaction and connection.in_transaction():
        connection.commit()
    return rows


def acquire_migration_lock(connection: Connection) -> bool:
    """Block until this connection holds the migration advisory lock.

    Returns True when the lock was taken (and therefore must be released),
    False on non-Postgres dialects, where nothing is executed at all.
    """
    if connection.dialect.name != "postgresql":
        return False
    execute_preserving_transaction_state(
        connection, _ACQUIRE_SQL, {"key": MIGRATION_LOCK_KEY}
    )
    return True


def release_migration_lock(connection: Connection) -> None:
    """Release the migration advisory lock held by this connection.

    Only meaningful after :func:`acquire_migration_lock` returned True on
    the *same* connection — advisory locks are session-scoped.
    """
    if connection.dialect.name != "postgresql":
        return
    execute_preserving_transaction_state(
        connection, _RELEASE_SQL, {"key": MIGRATION_LOCK_KEY}
    )
