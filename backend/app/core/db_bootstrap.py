"""Idempotent boot-time password sync for the incident_app runtime role.

Runs before uvicorn as `python -m app.core.db_bootstrap`, not in the role-creating
migration (b8e4a1c92f35) — that runs once, before INCIDENT_APP_DB_PASSWORD exists,
leaving the role passwordless forever (ADR 0015 rollout). ALTER ROLE takes no bind
params, so the password rides a transaction-local set_config() GUC a DO block reads
back via format(... %L), never a statement string.
"""

import asyncio
import os

from app.core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

logger = get_logger(__name__)

_SET_PASSWORD_GUC = text("SELECT set_config('app.role_pw', :pwd, true)")

_ALTER_ROLE_FROM_GUC = text(
    """
    DO $$
    BEGIN
        EXECUTE format(
            'ALTER ROLE incident_app LOGIN PASSWORD %L',
            current_setting('app.role_pw')
        );
    END
    $$
    """
)


def _bootstrap_url() -> str | None:
    """Owner-capable URL: ALEMBIC_DATABASE_URL first (post-flip deploys),
    then DATABASE_URL (phase-1 deploys, local compose migrate)."""
    return os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _is_postgres_url(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


async def sync_incident_app_password(url: str | None, password: str | None) -> bool:
    """Set incident_app's password; True when synced.

    No-op without a Postgres URL or a password. Failures propagate: a
    skipped sync crash-loops later on auth errors with no useful log.
    """
    if not url:
        logger.info("db bootstrap skipped: no database URL configured")
        return False
    if not _is_postgres_url(url):
        logger.info("db bootstrap skipped: non-postgres URL")
        return False
    if not password:
        logger.info(
            "db bootstrap skipped: INCIDENT_APP_DB_PASSWORD not set "
            "(expected on phase-1 deploys — see ADR 0015)"
        )
        return False

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(_SET_PASSWORD_GUC, {"pwd": password})
            await conn.execute(_ALTER_ROLE_FROM_GUC)
    finally:
        await engine.dispose()
    logger.info("db bootstrap: incident_app role password synced")
    return True


def main() -> int:
    """Entry point for `python -m app.core.db_bootstrap`."""
    asyncio.run(
        sync_incident_app_password(
            _bootstrap_url(), os.environ.get("INCIDENT_APP_DB_PASSWORD")
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
