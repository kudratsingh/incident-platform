"""Model-vs-policy completeness gate for row-level security.

Every table that carries a `tenant_id` column must be named in an RLS
migration's policy-table list. This guards against the recurring failure
mode that produced F1-05: the RLS migration (c4f8e9a52340) covered the six
tenant tables that existed at the time, and every tenant table added after
it (alerts, incident_summaries, service_accounts, idempotency_records,
deploy_markers) shipped without a policy.

The gate runs in plain CI — no Docker, no Postgres. It compares the ORM's
source of truth (`Base.metadata`, populated by importing `app.models`)
against the tables declared by the RLS migrations, which are loaded by
file path with importlib because `alembic/versions/` is not a package.

Convention: an RLS migration declares the tables its policies cover in
module-level list attributes named `_TABLES`, `_NEW_TABLES`, or
`_NULL_TENANT_TABLES`. A future migration that adds a policy for a new
tenant table must use one of those names (or extend this test).

The single allowed exclusion is `users`: authentication must read the
users row *before* `app.tenant_id` is set for the request, so an RLS
policy on it would break login — the deliberate bootstrap trade-off of
ADR 0003 (see its "Consequences -> bootstrap" section).
"""

import importlib.util
from pathlib import Path

import app.models  # noqa: F401  # importing registers every model on Base.metadata
from app.core.rls_check import RLS_EXEMPT_TABLES, tenant_scoped_tables
from app.models.base import Base

_VERSIONS_DIR = Path(__file__).resolve().parents[3] / "backend" / "alembic" / "versions"

# Module-level attributes an RLS migration uses to declare its policy tables.
_POLICY_LIST_ATTRS = ("_TABLES", "_NEW_TABLES", "_NULL_TENANT_TABLES")


def _rls_covered_tables() -> set[str]:
    """Union of policy-table lists declared by the migration modules."""
    covered: set[str] = set()
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"_rls_gate_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr in _POLICY_LIST_ATTRS:
            value = getattr(module, attr, None)
            if isinstance(value, list):
                covered.update(name for name in value if isinstance(name, str))
    return covered


def test_every_tenant_table_has_an_rls_policy() -> None:
    tenant_tables = {
        table.name for table in Base.metadata.tables.values() if "tenant_id" in table.columns
    }
    covered = _rls_covered_tables()
    # `users` is the single allowed exclusion (ADR 0003 bootstrap: auth reads
    # users before the request's app.tenant_id exists). Everything else with a
    # tenant_id column must be policy-covered by an RLS migration.
    assert tenant_tables - covered == RLS_EXEMPT_TABLES, (
        "tenant_id tables without an RLS policy (only 'users' may be exempt, "
        f"per ADR 0003): {sorted(tenant_tables - covered)}; "
        f"covered={sorted(covered)}"
    )


def test_the_migration_gate_and_the_boot_probe_agree_on_the_table_set() -> None:
    """The two halves of RLS coverage — "a migration declares a policy"
    and "the running database actually has one" — must be asking about
    the same tables (WO-R2-26). They were separate lists; the probe's had
    one entry in it.
    """
    assert tenant_scoped_tables() <= _rls_covered_tables()
