"""Model-vs-policy completeness gate for row-level security."""

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
