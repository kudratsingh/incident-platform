"""Boot-time RLS posture probe (F1-01, widened by WO-R2-26).

Mirrors app/core/migration_check.assert_migrations_current: a loud boot
check for a misconfiguration that would otherwise fail silently. Here
the silent failure is row-level security not actually constraining this
process's connection — exactly what production ran with before migration
a7e3d9c41f28: the app connected as the table owner with FORCE off, so
not one query was ever constrained by the tenant policies (ADR 0015).

Three ways the boundary can be down, and the probe has to see all of
them because they are not variations of one thing:

  * **Superuser** — policies never apply, FORCE or not.
  * **RLS switched off** (`relrowsecurity`) — one `ALTER TABLE ...
    DISABLE ROW LEVEL SECURITY` and the table is wide open to everyone.
  * **Owner exemption** — this role owns the table and FORCE is off.

The probe used to check only the third, against only the `jobs` table,
which made it blind in both directions at once. `jobs` was picked as
"the representative" because every tenant table got FORCE in the same
migration — true of the migration chain, and beside the point for a
*runtime* probe, whose whole job is to catch a live database that has
drifted from it. And for the intended non-owner production role the
`is_owner and not forced` term is always False, so the calculation
short-circuited to "not inert" and reported ok no matter what the server
actually had. A dropped `tenant_isolation` policy was invisible for the
same reason: nothing looked.

So: every tenant-scoped table, every term. The table list is derived
from the ORM (`tenant_scoped_tables`) rather than written down, and
`test_rls_coverage.py` / `test_rls_enforcement.py` share the same
derivation, so a new tenant-scoped table is probed the moment its model
exists.

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

# The one table with a `tenant_id` that must NOT carry a policy:
# authentication reads the users row before `app.tenant_id` is set for
# the request, so a policy on it would break login. Deliberate
# bootstrap trade-off, ADR 0003 ("Consequences -> bootstrap").
RLS_EXEMPT_TABLES = frozenset({"users"})

# The policy every tenant-scoped table carries. Created by the RLS
# migrations (c4f8e9a52340, a7e3d9c41f28) under this exact name.
TENANT_POLICY_NAME = "tenant_isolation"


class RlsPostureError(RuntimeError):
    """Raised at startup when the DB connection would silently bypass RLS."""


def tenant_scoped_tables() -> frozenset[str]:
    """Every table row-level security has to cover, from the ORM.

    Derived rather than listed so a new tenant-scoped table cannot slip
    past the probe by nobody remembering to add it — the failure mode
    that produced F1-05, where five tables shipped after the original
    RLS migration with no policy at all.

    Imported lazily: `app.models` pulls in the whole model graph, and
    this module is imported by the lifespans.
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

    Called from the app + MCP lifespans, right after
    ``assert_migrations_current`` (so the schema is known to exist and
    match head). Logs ERROR always; raises :class:`RlsPostureError` only
    when ``settings.environment`` is ``production``.
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

    # Superuser first: it makes every per-table term moot, so reporting
    # it alongside a list of tables would misdescribe the cause.
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
