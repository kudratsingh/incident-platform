"""Unit tests for the alembic migration advisory lock (WO-P7-08 / F2-04).

scripts/entrypoint.sh has always claimed concurrent task startups are safe
because "alembic uses an advisory lock"; until this change nothing took
one. These tests pin the helper's contract *and* the fact that env.py
still calls it — deleting the lock from the migration path cannot be done
without deleting the module or the call site, and both are asserted here.
"""

from pathlib import Path
from typing import Any

from app.core.migration_lock import (
    MIGRATION_LOCK_KEY,
    acquire_migration_lock,
    release_migration_lock,
)

ENV_PY = Path(__file__).resolve().parents[2] / "alembic" / "env.py"


class _StubDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubResult:
    """The bit of `sqlalchemy.Result` the helper touches."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _StubConnection:
    """Records executed statements and mimics SQLAlchemy 2.0 autobegin."""

    def __init__(self, dialect_name: str, *, in_transaction: bool = False) -> None:
        self.dialect = _StubDialect(dialect_name)
        self.rows: list[Any] = []
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self._in_transaction = in_transaction

    def in_transaction(self) -> bool:
        return self._in_transaction

    def execute(self, statement: Any, parameters: Any = None) -> "_StubResult":
        self.executed.append((str(statement), parameters))
        self._in_transaction = True  # autobegin
        # A real Connection.execute returns a Result; the helper drains it
        # before committing, because a commit closes it.
        return _StubResult(self.rows)

    def commit(self) -> None:
        self.commits += 1
        self._in_transaction = False


def test_lock_key_is_ascii_alembic_and_fits_int64() -> None:
    assert MIGRATION_LOCK_KEY == 0x616C656D626963
    assert MIGRATION_LOCK_KEY.to_bytes(7, "big") == b"alembic"
    assert -(2**63) <= MIGRATION_LOCK_KEY < 2**63


def test_acquire_on_postgres_executes_session_lock_with_documented_key() -> None:
    conn = _StubConnection("postgresql")

    assert acquire_migration_lock(conn) is True  # type: ignore[arg-type]

    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "pg_advisory_lock" in sql
    # Session-level, NOT xact-level: alembic commits internally, so a
    # transaction-scoped lock would be dropped mid-migration.
    assert "pg_advisory_xact_lock" not in sql
    assert params == {"key": MIGRATION_LOCK_KEY}


def test_release_on_postgres_executes_unlock_with_documented_key() -> None:
    conn = _StubConnection("postgresql")

    release_migration_lock(conn)  # type: ignore[arg-type]

    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "pg_advisory_unlock" in sql
    assert params == {"key": MIGRATION_LOCK_KEY}


def test_acquire_on_sqlite_executes_nothing_and_reports_no_lock() -> None:
    conn = _StubConnection("sqlite")

    assert acquire_migration_lock(conn) is False  # type: ignore[arg-type]

    assert conn.executed == []
    assert conn.commits == 0


def test_release_on_sqlite_executes_nothing() -> None:
    conn = _StubConnection("sqlite")

    release_migration_lock(conn)  # type: ignore[arg-type]

    assert conn.executed == []
    assert conn.commits == 0


def test_acquire_leaves_no_open_transaction_on_a_fresh_connection() -> None:
    """The lock must not leave alembic looking at an "external" transaction.

    MigrationContext snapshots connection.in_transaction() at configure()
    time; if it sees a transaction it did not start, begin_transaction()
    becomes a null context and the migration is never committed. Since
    SQLAlchemy 2.0 autobegins on execute(), acquiring the lock has to
    commit the transaction it implicitly opened.
    """
    conn = _StubConnection("postgresql")

    acquire_migration_lock(conn)  # type: ignore[arg-type]

    assert conn.in_transaction() is False
    assert conn.commits == 1


def test_release_leaves_no_open_transaction_on_a_fresh_connection() -> None:
    conn = _StubConnection("postgresql")

    release_migration_lock(conn)  # type: ignore[arg-type]

    assert conn.in_transaction() is False
    assert conn.commits == 1


def test_acquire_does_not_commit_a_transaction_the_caller_opened() -> None:
    conn = _StubConnection("postgresql", in_transaction=True)

    acquire_migration_lock(conn)  # type: ignore[arg-type]

    assert conn.commits == 0
    assert conn.in_transaction() is True


def test_env_py_takes_and_releases_the_lock_around_run_migrations() -> None:
    """env.py is not importable (it runs migrations at import), so assert on
    its source: both online paths share do_run_migrations, and the lock must
    be acquired before context.configure and released in a finally."""
    source = ENV_PY.read_text()

    assert "from app.core.migration_lock import" in source
    assert "acquire_migration_lock(connection)" in source
    assert "release_migration_lock(connection)" in source

    body = source.split("def do_run_migrations", 1)[1]
    acquire_at = body.index("acquire_migration_lock(connection)")
    configure_at = body.index("context.configure")
    release_at = body.index("release_migration_lock(connection)")
    assert acquire_at < configure_at < release_at
    assert "finally:" in body[configure_at:release_at]
