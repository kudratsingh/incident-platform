"""strict tenant_isolation: an unscoped statement is refused, not admitted

Revision ID: e2a9c4f70b31
Revises: d1f6a2b940c7
Create Date: 2026-08-30 10:00:00.000000

WO-R2-129 / ADR 0026. Closes the fail-open default that plat #192 proved
live: an unscoped connection wrote *and* read another tenant's rows, with
nothing raised and nothing logged.

What was wrong
==============
Every `tenant_isolation` policy created by c4f8e9a52340 and a7e3d9c41f28
opened with the ADR 0003 bootstrap branch::

    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true)::uuid

The first two disjuncts are satisfied by *any* statement that has not set
`app.tenant_id`, on all eleven tenant tables — so the policy admitted
everything, for both USING and WITH CHECK, whenever the setting was
missing. RLS is the backstop for a forgotten `WHERE tenant_id = …`, and
the backstop was off by default.

ADR 0003 introduced that branch for one narrow reason: authentication has
to read a principal row *before* the request's tenant is known. But the
table that need names — `users` — carries no policy at all (it is the
single exemption in `rls_check.RLS_EXEMPT_TABLES`), so on the other ten
tables the branch was serving no bootstrap purpose whatsoever. It was
load-bearing only by accident, for paths that had simply never been asked
to declare themselves.

What this migration does
========================
1. **The tenant match becomes the only way in**, and it is NULL-safe::

       tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid

   `nullif` folds the two hazards into one deny. Unset yields NULL, and
   `tenant_id = NULL` is NULL — never true — so the row is filtered on
   read and rejected by WITH CHECK on write. The empty string yields NULL
   too, which also fixes the secondary defect in the same finding: a
   pooled connection whose GUC had reset to `''` used to reach
   `''::uuid` and raise `invalid_text_representation` outright. Both
   now fail closed and quietly.

2. **Cross-tenant work declares itself** via a second GUC,
   `app.tenant_scope = 'platform'`. The worker loops, the migration
   runner and the seed/reset scripts are legitimately mixed-tenant
   (ADR 0003 argued this and it is still true); what changes is that
   they now *say so* instead of being admitted for having forgotten.
   `app/core/tenant_scope.py` is the only thing that sets it, always
   transaction-locally — the runtime shares one pool between requests
   and workers (ADR 0015), so a session-level SET would leak platform
   scope onto the next request to check that connection out.

   The direction is the entire point. Before, silence granted full
   access; now silence is refused and access is a declaration you can
   grep for.

3. **`service_accounts` keeps a narrow, SELECT-only bootstrap read.**
   This is the one genuine non-`users` bootstrap consumer, and it is
   real: `get_current_principal` calls `verify_token`, which reads
   `service_accounts` two statements *before* `_apply_tenant_context`
   issues `set_config` on the same transaction (backend/app/
   dependencies.py:200-214, backend/app/services/service_account.py:292).
   It cannot set the tenant first — the row it is fetching is what tells
   it which tenant this is. A separate permissive policy
   `service_accounts_bootstrap_read` restores the unscoped read for
   `FOR SELECT` only, so a machine principal can still authenticate
   while every INSERT/UPDATE/DELETE on the table stays strictly scoped.
   Permissive policies OR together, so this widens SELECT alone.

4. **`deploy_markers` keeps `OR tenant_id IS NULL`** in USING and WITH
   CHECK. Deliberate and unchanged (ADR 0015): `tenant_id` is nullable
   by design because deploys are platform-wide, and hiding NULL rows
   from tenant-scoped sessions would silently degrade
   `get_deploy_history` to its env-var fallback. Note what this is *not*
   — an unscoped session can now write only a NULL-tenant marker; it can
   no longer forge a row for a named tenant.

`audit_logs`' RESTRICTIVE `audit_logs_block_update` / `_block_delete`
policies are untouched: RESTRICTIVE policies AND with the permissive one,
so tightening the permissive side cannot weaken them.

Ordering note
=============
Two already-merged migrations run their DML *after* a7e3d9c41f28 turned
FORCE on, and therefore depend on the branch this migration removes:
`b1f39d7c2a84` (UPDATE jobs SET heartbeat_at), `d1f6a2b940c7` (the
saga_step_index backfill), plus `c9e41a7b62d5`'s downgrade (DELETE FROM
idempotency_records) and `a5c19d3f7e42`'s audit_logs backfill. Under a
strict policy an owner-connected UPDATE matches no rows and reports
`UPDATE 0` — a *silent* no-op, the worst possible failure for a backfill.
They are frozen history and are not edited; instead `alembic/env.py`
declares platform scope for the whole migration run, which covers every
past and future migration in one place. The integration fixture runs the
entire chain from empty, so this is exercised on every CI run.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e2a9c4f70b31"
down_revision: str | Sequence[str] | None = "d1f6a2b940c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Named `_TABLES` / `_NULL_TENANT_TABLES` deliberately: the plain-CI gate
# backend/tests/unit/test_rls_coverage.py discovers policy coverage by
# unioning module attributes with exactly these names across
# alembic/versions/*.py. A rename here would read as "these tables lost
# their policy".
_TABLES = [
    "jobs",
    "audit_logs",
    "outbox_events",
    "job_events",
    "sagas",
    "job_triages",
    "incident_summaries",
    "service_accounts",
    "alerts",
    "idempotency_records",
]

_NULL_TENANT_TABLES = ["deploy_markers"]

_ALL_TABLES = _TABLES + _NULL_TENANT_TABLES

# The strict predicate. `nullif(..., '')` makes unset and empty-string
# both deny rather than one admitting everything and the other raising.
_MATCH = (
    "current_setting('app.tenant_scope', true) = 'platform'"
    " OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
)

_NULL_TENANT_MATCH = (
    "current_setting('app.tenant_scope', true) = 'platform'"
    " OR tenant_id IS NULL"
    " OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
)

# The permissive pre-a7e3d9c41f28 text, restored verbatim by downgrade().
_LEGACY_MATCH = (
    "current_setting('app.tenant_id', true) IS NULL"
    " OR current_setting('app.tenant_id', true) = ''"
    " OR tenant_id = current_setting('app.tenant_id', true)::uuid"
)

_LEGACY_NULL_TENANT_MATCH = (
    "current_setting('app.tenant_id', true) IS NULL"
    " OR current_setting('app.tenant_id', true) = ''"
    " OR tenant_id IS NULL"
    " OR tenant_id = current_setting('app.tenant_id', true)::uuid"
)


def _replace_policy(table: str, match: str) -> None:
    """Swap `tenant_isolation` for one with the given predicate.

    DROP + CREATE rather than ALTER POLICY: ALTER cannot be expressed for
    both USING and WITH CHECK in one statement on every supported server,
    and a drop/create pair reads the same in the downgrade. Both run
    inside the migration's transaction, so no window exists where the
    table is policy-less.
    """
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table}"
        f"  USING ({match})"
        f"  WITH CHECK ({match})"
    )


def upgrade() -> None:
    # Skip on non-Postgres dialects (the SQLite unit/API DB has no RLS).
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _TABLES:
        _replace_policy(table, _MATCH)

    for table in _NULL_TENANT_TABLES:
        _replace_policy(table, _NULL_TENANT_MATCH)

    # The one genuine non-`users` bootstrap consumer: the pre-auth
    # service-account lookup, which runs before the tenant it would scope
    # to is known. SELECT only — writes stay strictly scoped.
    op.execute(
        "CREATE POLICY service_accounts_bootstrap_read ON service_accounts"
        "  FOR SELECT"
        "  USING (nullif(current_setting('app.tenant_id', true), '') IS NULL)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(
        "DROP POLICY IF EXISTS service_accounts_bootstrap_read ON service_accounts"
    )

    for table in _TABLES:
        _replace_policy(table, _LEGACY_MATCH)

    for table in _NULL_TENANT_TABLES:
        _replace_policy(table, _LEGACY_NULL_TENANT_MATCH)
