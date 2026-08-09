"""Boot-time RLS posture probe (F1-01).

Mirrors app/core/migration_check.assert_migrations_current: a loud boot
check for a misconfiguration that would otherwise fail silently. Here
the silent failure is row-level security being *inert* for the process's
own connection — exactly what production ran with before migration
a7e3d9c41f28: the app connected as the table owner with FORCE off, so
not one query was ever constrained by the tenant policies (ADR 0015).

RLS is inert for a connection iff it is a superuser (policies never
apply, FORCE or not), or it owns the tables and FORCE ROW LEVEL
SECURITY is off (owner exemption). The probe checks the `jobs` table as
the representative — every tenant table got FORCE in the same migration
(a7e3d9c41f28) and the unit gate test_rls_coverage.py keeps the set
complete.

Raise only in production: local docker-compose historically connects as
the postgres superuser and MUST keep booting — a hard fail outside prod
would brick local dev and, after the next release tag, the commander's
eval stack. Outside production the probe logs ERROR and continues
(`docker compose restart` skipping the migrate one-shot is a known
footgun — v0.4.1 postmortem — so the warning still earns its place).
No-op on SQLite: no roles, no RLS, nothing to probe.
"""

from typing import TYPE_CHECKING

from app.core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger(__name__)


class RlsPostureError(RuntimeError):
    """Raised at startup when the DB connection would silently bypass RLS."""


_PROBE_SQL = text(
    """
    SELECT current_setting('is_superuser') = 'on' AS su,
           (SELECT rolname FROM pg_roles WHERE oid = c.relowner) = current_user AS is_owner,
           c.relforcerowsecurity AS forced
    FROM pg_class c
    WHERE c.oid = 'jobs'::regclass
    """
)


async def assert_rls_posture(
    session_factory: async_sessionmaker[AsyncSession],
    settings: "Settings",
) -> None:
    """Refuse to serve (in production) when RLS is inert for this connection.

    Called from the app + MCP lifespans, right after
    ``assert_migrations_current`` (so the schema — including ``jobs`` —
    is known to exist). Logs ERROR always; raises
    :class:`RlsPostureError` only when ``settings.environment`` is
    ``production``.
    """
    async with session_factory() as session:
        if session.get_bind().dialect.name != "postgresql":
            logger.info("rls posture check skipped (non-postgres engine)")
            return
        row = (await session.execute(_PROBE_SQL)).one()

    su = bool(row.su)
    is_owner = bool(row.is_owner)
    forced = bool(row.forced)
    inert = su or (is_owner and not forced)

    if not inert:
        logger.info(
            "rls posture ok",
            extra={"superuser": su, "table_owner": is_owner, "force_rls": forced},
        )
        return

    message = (
        f"RLS posture check failed: this connection would silently bypass "
        f"row-level security (superuser={su}, table_owner={is_owner}, "
        f"force_rls={forced} on 'jobs'). Production must connect as the "
        f"non-owner incident_app role — point DATABASE_URL at it (phase 2 "
        f"of the ADR 0015 rollout) and keep migrations on the owner via "
        f"ALEMBIC_DATABASE_URL. Local superuser stacks only get this "
        f"logged."
    )
    logger.error(message)
    if settings.environment == "production":
        raise RlsPostureError(message)
