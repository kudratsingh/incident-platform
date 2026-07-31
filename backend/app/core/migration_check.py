"""
Fail-fast startup check for schema drift.

Compares the alembic head on disk (i.e. what the running image
believes the schema should be) against `alembic_version` in the
DB (what the schema actually is). If they diverge, refuse to
serve — a loud boot failure is strictly better than the silent
run we hit in v0.4.1: the migration for `jobs.remediation_hint`
was never applied, and every DLQ tool returned mystery 500s
until an operator noticed. See PR #67 postmortem.

Deliberately not auto-migrating here. The `migrate` compose
service exists precisely so exactly one process runs
`alembic upgrade head`; if the API/MCP tried it at boot they'd
race on Postgres `pg_type` uniqueness under
`CREATE TABLE`.

Gated by `SKIP_MIGRATION_CHECK` so tests (SQLite in-memory,
never migrated with alembic) don't trip it.
"""

import os
from pathlib import Path

from app.core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = get_logger(__name__)


class SchemaOutOfDateError(RuntimeError):
    """Raised at startup when the DB schema is behind the code's expectations."""


def _repo_root() -> Path:
    # /app/backend/app/core/migration_check.py → /app in the container,
    # /.../incident-platform on a dev machine. Both host alembic.ini.
    return Path(__file__).resolve().parents[3]


def _expected_head() -> str:
    """Read the head revision from the alembic scripts on disk."""
    # Import inside the function so unit tests that patch this module
    # don't need alembic installed in their environment.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_repo_root() / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("alembic script directory has no head revision")
    return head


async def _current_revision(
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> str | None:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT version_num FROM alembic_version")
        )
        row = result.scalar_one_or_none()
    return str(row) if row is not None else None


async def assert_migrations_current(
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
) -> None:
    """Raise `SchemaOutOfDateError` if the DB is behind the code's head.

    Called from the app + MCP lifespans. `SKIP_MIGRATION_CHECK=1`
    (or `true`) opts out — used by CI and local dev workflows that
    don't run alembic.
    """
    if os.getenv("SKIP_MIGRATION_CHECK", "").lower() in ("1", "true"):
        logger.info("migration check skipped via SKIP_MIGRATION_CHECK")
        return

    expected = _expected_head()
    current = await _current_revision(session_factory)

    if current == expected:
        logger.info("schema check ok", extra={"revision": current})
        return

    raise SchemaOutOfDateError(
        f"Database schema is behind: DB at {current!r}, code expects "
        f"{expected!r}. Run `docker compose up migrate` (or "
        f"`alembic upgrade head`) before restarting this service. Set "
        f"SKIP_MIGRATION_CHECK=1 to bypass (do not use in production)."
    )
