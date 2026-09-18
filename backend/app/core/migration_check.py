"""
Fail-fast startup check for schema drift.

Compares the alembic head on disk with `alembic_version` in the DB and
refuses to serve on divergence — v0.4.1 shipped without the
`jobs.remediation_hint` migration and every DLQ tool returned mystery 500s
(PR #67 postmortem). Never auto-migrates: only the `migrate` one-shot runs
`alembic upgrade head`. `SKIP_MIGRATION_CHECK` opts the SQLite suites out.
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
    """Raise `SchemaOutOfDateError` if the DB is behind head; `SKIP_MIGRATION_CHECK` opts out."""
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
