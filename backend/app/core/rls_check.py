"""Boot-time RLS posture probe (F1-01, widened by WO-R2-26).

A loud boot check for row-level security not actually constraining this
connection — what production ran with before migration a7e3d9c41f28 (ADR 0015).
Three independent ways the boundary is down: superuser, RLS off
(`relrowsecurity`), and owner-exemption (owner with FORCE off); plus a missing
`tenant_isolation` policy. All four, on every tenant-scoped table — the old probe
read one term on `jobs` alone, which for the non-owner role was always False and
so reported ok whatever the server had. The table list is derived from the ORM
(`tenant_scoped_tables`), shared with `test_rls_coverage.py` /
`test_rls_enforcement.py`. Raises only in production: local compose connects as
superuser and MUST keep booting, so elsewhere it logs ERROR. No-op on SQLite.
"""

from typing import TYPE_CHECKING

from app.core.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger(__name__)

# The one `tenant_id` table with no policy: auth reads users before
# `app.tenant_id` is set. Bootstrap trade-off, ADR 0003.
RLS_EXEMPT_TABLES = frozenset({"users"})

# The policy every tenant-scoped table carries. Created by the RLS
# migrations (c4f8e9a52340, a7e3d9c41f28) under this exact name.
TENANT_POLICY_NAME = "tenant_isolation"


class RlsPostureError(RuntimeError):
    """Raised at startup when the DB connection would silently bypass RLS."""


def tenant_scoped_tables() -> frozenset[str]:
    """Every table row-level security has to cover, from the ORM.

    Derived, not listed, so a new tenant-scoped table cannot slip past the probe
    (F1-05). Imported lazily — the lifespans import this module.
    """
    import app.models  # noqa: F401  # registers every model on Base.metadata
    from app.models.base import Base

    return (
        frozenset(
            table.name
            for table in Base.metadata.tables.values()
            if "tenant_id" in table.columns
        )
        - RLS_EXEMPT_TABLES
    )


_SUPERUSER_SQL = text("SELECT current_setting('is_superuser') = 'on' AS su")

_PROBE_SQL = text(
    """
    SELECT c.relname AS table_name,
           (SELECT rolname FROM pg_roles WHERE oid = c.relowner) = current_user
               AS is_owner,
           c.relrowsecurity AS enabled,
           c.relforcerowsecurity AS forced,
           EXISTS (
               SELECT 1 FROM pg_policies p
               WHERE p.schemaname = n.nspname
                 AND p.tablename = c.relname
                 AND p.policyname = :policy_name
           ) AS has_policy
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND pg_table_is_visible(c.oid)
      AND c.relname = ANY(CAST(:names AS text[]))
    """
)


async def assert_rls_posture(
    session_factory: async_sessionmaker[AsyncSession],
    settings: "Settings",
) -> None:
    """Refuse to serve (in production) when RLS is inert for this connection.

    Raises :class:`RlsPostureError` only in production; logs ERROR always.
    """
    expected = sorted(tenant_scoped_tables())

    async with session_factory() as session:
        if session.get_bind().dialect.name != "postgresql":
            logger.info("rls posture check skipped (non-postgres engine)")
            return
        su = bool((await session.execute(_SUPERUSER_SQL)).scalar())
        rows = (
            await session.execute(
                _PROBE_SQL,
                {"names": expected, "policy_name": TENANT_POLICY_NAME},
            )
        ).all()

    problems: list[str] = []

    # Superuser first: it makes every per-table term moot.
    if su:
        problems.append(
            "connection is a superuser, so no policy ever applies"
        )

    seen = {str(row.table_name) for row in rows}
    for name in sorted(set(expected) - seen):
        problems.append(f"{name}: table not found (expected under RLS)")

    for row in sorted(rows, key=lambda r: str(r.table_name)):
        name = str(row.table_name)
        if not row.enabled:
            problems.append(f"{name}: row level security is DISABLED")
        elif row.is_owner and not row.forced:
            problems.append(
                f"{name}: owned by this role with FORCE off (owner exemption)"
            )
        if not row.has_policy:
            problems.append(f"{name}: no {TENANT_POLICY_NAME} policy")

    if not problems:
        logger.info(
            "rls posture ok",
            extra={"superuser": su, "tables_checked": len(rows)},
        )
        return

    message = (
        "RLS posture check failed: this connection would not be fully "
        "constrained by row-level security. "
        + "; ".join(problems)
        + ". Production must connect as the non-owner incident_app role "
        "— point DATABASE_URL at it (phase 2 of the ADR 0015 rollout) "
        "and keep migrations on the owner via ALEMBIC_DATABASE_URL. A "
        "table reported DISABLED or policy-less has drifted from the "
        "migration chain and needs re-applying, not a connection change. "
        "Local superuser stacks only get this logged."
    )
    logger.error(message)
    if settings.environment == "production":
        raise RlsPostureError(message)
