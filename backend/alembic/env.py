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

Both online paths funnel through do_run_migrations, which takes a
session-level Postgres advisory lock on the migrating connection for the
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


def do_run_migrations(connection: object) -> None:
    """Run the migrations on a SYNC connection, holding the advisory lock.

    Shared by both online paths — the async engine reaches it through
    run_sync (hence a sync Connection and no await), the sync engine calls
    it directly. The lock is taken on this very connection and held for the
    entire run, so a second task blocks here until the first finishes and
    then no-ops on an already-current alembic_version. Non-Postgres
    dialects report locked=False and nothing is executed.
    """
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
