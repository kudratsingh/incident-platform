"""Idempotent boot-time password sync for the incident_app runtime role.

Runs at every boot BEFORE uvicorn — from scripts/entrypoint.sh in prod
and from the docker-compose `migrate` one-shot locally (the compose app
service's custom `command:` bypasses the entrypoint) — as
`python -m app.core.db_bootstrap`.

Why the password is here and NOT in the role-creating migration
(b8e4a1c92f35): migrations run exactly once, and the role is created on
a phase-1 deploy that predates the Terraform secret — at that moment
INCIDENT_APP_DB_PASSWORD does not exist, so a migration-owned password
would leave the role passwordless forever. A boot-time sync is the only
ordering-safe place, and it gives free rotation: change the secret and
bounce the service (see the rollout section of ADR 0015).

Connection: uses the owner-capable URL — ALEMBIC_DATABASE_URL first
(after the phase-2 flip DATABASE_URL is the non-owner incident_app
itself, which cannot ALTER its own password non-interactively here),
falling back to DATABASE_URL (phase-1 deploys, local compose). Strict
no-op unless the URL is Postgres AND INCIDENT_APP_DB_PASSWORD is set:
phase-1 boots and SQLite stacks must not attempt a connection.

Injection safety: ALTER ROLE is DDL and cannot take bind parameters, so
the password is bound into set_config() (a transaction-local GUC) and a
DO block reads it back with format('ALTER ROLE ... PASSWORD %L',
current_setting(...)) — %L quote-literals safely server-side. The
password never appears in a statement string. Both statements share one
transaction (set_config(..., true) is transaction-local).
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
    """Set incident_app's password to ``password``; True when synced.

    No-op (returns False, no connection attempted) when the URL is
    missing or not Postgres, or when no password is configured. Any
    connection/SQL failure propagates: the entrypoint runs under
    ``set -e``, and a boot that silently skipped the sync would
    crash-loop later on incident_app auth failures with no useful log.
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
