"""Alembic migration environment.

URL selection — the two-URL scheme (ADR 0015): migrations need the table
*owner* (DDL rights), but since WO-P2-03 the runtime DATABASE_URL points
at the non-owner incident_app role. ALEMBIC_DATABASE_URL therefore takes
precedence: in production ECS it carries the owner (RDS master) URL, so
`alembic upgrade head` keeps working after the runtime flip. When it is
unset — phase-1 deploys, the local compose migrate one-shot, tests —
DATABASE_URL is used unchanged, and the alembic.ini value is the last
resort.

Online migrations are routed on the configured database URL's dialect:
async dialects (e.g. postgresql+asyncpg) run through an async engine,
sync dialects (e.g. postgresql / postgresql+psycopg2) through a plain
sync engine. Setting the RUN_ALEMBIC_SYNC environment variable to any
non-empty value is an explicit operator override that forces the sync
path regardless of the URL's dialect.

Both online paths funnel through do_run_migrations, which first refuses
outright if the connected role cannot create tables (WO-R2-67), then takes
a session-level Postgres advisory lock on the migrating connection for the
whole run (see app/core/migration_lock.py) — that is what makes
concurrent ECS task startups serialize instead of racing on pg_type.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Make sure the backend package is importable when running alembic from the
# project root (e.g. `alembic -c alembic.ini upgrade head`).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# Import all models so their tables are registered on Base.metadata before
# autogenerate compares against the live database schema.
import app.models.audit  # noqa: E402, F401
import app.models.job  # noqa: E402, F401
import app.models.user  # noqa: E402, F401
from app.core.db_url import is_async_url  # noqa: E402
from app.core.migration_lock import (  # noqa: E402
    acquire_migration_lock,
    execute_preserving_transaction_state,
    release_migration_lock,
)
from app.models.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    """Prefer ALEMBIC_DATABASE_URL (owner URL — see module docstring),
    then DATABASE_URL, then the alembic.ini value."""
    return (
        os.environ.get("ALEMBIC_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or config.get_main_option("sqlalchemy.url", "")
    )


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without a live DB connection."""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


class MigrationRoleError(RuntimeError):
    """The connected role cannot apply migrations."""


def assert_role_can_migrate(connection: object) -> None:
    """Refuse to start a migration the connected role cannot finish.

    The two-URL scheme (ADR 0015) means there are now two credentials in
    play and only one of them can run DDL, so "which role am I?" became a
    question with a wrong answer. `make migrate` had the wrong one: it
    exec'd alembic inside the `app` container, which connects as the
    non-owner `incident_app` role and cannot CREATE (WO-R2-67).

    Without this check that failure arrives as a `permission denied for
    schema public` from somewhere in the middle of whichever revision first
    creates a table — after earlier revisions have already applied, on a
    connection that had every appearance of working. The message names
    neither the role nor the variable that would fix it. Checking up front
    costs one round-trip and turns it into a sentence.

    Postgres only: the privilege model being asserted is Postgres's. Other
    dialects (the SQLite unit harness) pass through untouched.
    """
    from sqlalchemy import text

    dialect = getattr(getattr(connection, "dialect", None), "name", "")
    if dialect != "postgresql":
        return

    # Through the lock module's helper, not a bare execute: SQLAlchemy 2.0
    # autobegins on the first statement, and MigrationContext downgrades
    # `begin_transaction()` to a null context when it finds a transaction it
    # did not open — so a plain `connection.execute()` here would leave every
    # migration to roll back on close. Verified the hard way: the preflight
    # applied all 11 revisions and left 0 tables behind.
    rows = execute_preserving_transaction_state(
        connection,  # type: ignore[arg-type]
        text(
            "SELECT current_user AS role_name, "
            "has_schema_privilege(current_user, 'public', 'CREATE') AS can_create"
        ),
    )
    row = rows[0]
    if not row.can_create:
        raise MigrationRoleError(
            f"role {row.role_name!r} cannot CREATE in schema public, so it "
            "cannot apply migrations. Migrations run as the database owner: "
            "set ALEMBIC_DATABASE_URL to the owner DSN (in compose, "
            "`make migrate` runs the dedicated `migrate` service, which "
            "already has it; in ECS it is the database-url-owner secret). "
            "The runtime DATABASE_URL is deliberately the non-owner "
            "incident_app role — see ADR 0015."
        )


def do_run_migrations(connection: object) -> None:
    """Run the migrations on a SYNC connection, holding the advisory lock.

    Shared by both online paths — the async engine reaches it through
    run_sync (hence a sync Connection and no await), the sync engine calls
    it directly. The lock is taken on this very connection and held for the
    entire run, so a second task blocks here until the first finishes and
    then no-ops on an already-current alembic_version. Non-Postgres
    dialects report locked=False and nothing is executed.
    """
    # Before the lock: a role that cannot migrate should not make every
    # other task queue behind it while it finds that out.
    assert_role_can_migrate(connection)
    locked = acquire_migration_lock(connection)  # type: ignore[arg-type]
    try:
        context.configure(
            connection=connection,  # type: ignore[arg-type]
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if locked:
            release_migration_lock(connection)  # type: ignore[arg-type]


async def run_migrations_online() -> None:
    """Run migrations against a live DB using an async engine."""
    engine = create_async_engine(_get_url(), poolclass=None)  # type: ignore[arg-type]
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online_sync() -> None:
    """Run migrations against a live DB using a sync engine.

    Taken when the URL's dialect is sync (e.g. postgresql+psycopg2) or
    when the RUN_ALEMBIC_SYNC operator override is set.
    """
    engine = create_engine(_get_url(), poolclass=NullPool)
    with engine.connect() as connection:
        do_run_migrations(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif os.environ.get("RUN_ALEMBIC_SYNC") or not is_async_url(_get_url()):
    run_migrations_online_sync()
else:
    asyncio.run(run_migrations_online())
