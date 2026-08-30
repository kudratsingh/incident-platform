"""The boot RLS probe must cover every tenant-scoped table (R2-26).

Sibling to `test_rls_coverage.py`, which checks that every tenant table
is named by an RLS *migration*. This one checks the runtime half: that
the boot-time posture probe actually looks at all of them.

It used to look at exactly one, `jobs`, chosen as "the representative"
on the reasoning that every tenant table got FORCE in the same
migration. That holds for the migration chain and says nothing about the
live database, which is the only thing a runtime probe is for: `ALTER
TABLE ... DISABLE ROW LEVEL SECURITY` on any other table was invisible
to it.

Both the probe and the two RLS test tiers now derive their table list
from one function, so a new tenant-scoped table is probed the moment its
model exists — no list to remember to extend.
"""

import app.models  # noqa: F401  # importing registers every model on Base.metadata
from app.core.rls_check import RLS_EXEMPT_TABLES, tenant_scoped_tables
from app.models.base import Base


def test_probe_covers_every_tenant_scoped_table() -> None:
    from_orm = {
        table.name
        for table in Base.metadata.tables.values()
        if "tenant_id" in table.columns
    }
    assert tenant_scoped_tables() == from_orm - RLS_EXEMPT_TABLES


def test_users_is_the_only_exemption() -> None:
    """ADR 0003 bootstrap: auth reads the users row before the request's
    `app.tenant_id` exists, so a policy on it would break login. Any
    other exemption is a hole, not a trade-off."""
    assert RLS_EXEMPT_TABLES == frozenset({"users"})


def test_probe_is_no_longer_a_single_representative_table() -> None:
    """Regression on the specific shape of the finding: the probe read
    one hardcoded table and generalised from the migration chain."""
    covered = tenant_scoped_tables()
    assert "jobs" in covered
    assert len(covered) > 1, "the probe is back to a single representative table"
    # Tables added long after the original RLS migration — the ones the
    # 'they all got FORCE together' reasoning never covered.
    assert {"alerts", "idempotency_records", "deploy_markers"} <= covered
