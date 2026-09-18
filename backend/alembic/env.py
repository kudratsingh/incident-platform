"""Alembic migration environment.

URL precedence (ADR 0015): ALEMBIC_DATABASE_URL (owner) > DATABASE_URL (non-owner
incident_app since WO-P2-03) > alembic.ini; RUN_ALEMBIC_SYNC forces the sync path. Both
online paths reach do_run_migrations: refuse a non-CREATE role (WO-R2-67), hold the lock.
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
    """ALEMBIC_DATABASE_URL > DATABASE_URL > alembic.ini."""
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
    """Refuse a migration the connected role cannot finish — otherwise it fails
    mid-revision as `permission denied for schema public`, naming neither the role
    nor the variable that would fix it (WO-R2-67). Postgres only.
    """
    from sqlalchemy import text

    dialect = getattr(getattr(connection, "dialect", None), "name", "")
    if dialect != "postgresql":
        return

    # Through the lock module's helper, not a bare execute: SQLAlchemy 2.0 autobegins,
    # and MigrationContext then nulls out `begin_transaction()`, rolling back on close.
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


def _declare_platform_scope(connection: object) -> None:
    """Let this migration run touch every tenant's rows (ADR 0026); without it the
    already-merged cross-tenant backfills silently `UPDATE 0`. Session-level SET,
    issued inside `context.begin_transaction()` — earlier would autobegin and null it.
    """
    dialect = getattr(getattr(connection, "dialect", None), "name", "")
    if dialect != "postgresql":
        return
    connection.exec_driver_sql(  # type: ignore[attr-defined]
        "SET app.tenant_scope = 'platform'"
    )


def do_run_migrations(connection: object) -> None:
    """Run the migrations on a SYNC connection, holding the advisory lock for the
    whole run, so a second task blocks here then no-ops on a current
    alembic_version. Shared by both online paths.
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
            _declare_platform_scope(connection)
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
    """Run migrations with a sync engine: sync dialect, or RUN_ALEMBIC_SYNC set."""
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
