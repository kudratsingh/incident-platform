# ADR 0003 — Postgres row-level security as defense-in-depth, not primary tenant isolation

**Status:** Accepted (Phase 12 PR #37) · **Date:** 2026 Q2 · **Owner:** Platform

## Context

After Phase 12 PR B, every tenant-scoped query at the application layer passes through a repository method that explicitly filters by `tenant_id`. That works for code-reviewed paths. But it has one mode of failure: a future contributor writes a new query, forgets the filter, and ships it. The bug is silent — endpoints return rows; admins see them; until someone notices the count is "wrong" or a tenant complains about seeing another tenant's job ID.

The platform stores customer job payloads, error messages, and saga state. A single forgotten `WHERE tenant_id = :tid` is a cross-tenant data leak. The application-layer filter is necessary but not sufficient.

We need a second line of defense at a layer the application code can't bypass even by accident.

## Decision

Enable Postgres **row-level security (RLS)** on every tenant-scoped table:

```sql
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON jobs
  USING (
    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true)::uuid
  )
  WITH CHECK (...);
```

The `get_current_user` dependency issues `SELECT set_config('app.tenant_id', :tid, true)` after authentication, so every query inside that request is automatically scoped — even one that forgot a WHERE clause.

Tables covered: `jobs`, `audit_logs`, `outbox_events`, `job_events`, `sagas`, `job_triages`. (Not `users` — auth needs to read users *before* the tenant context is set; see "Consequences → bootstrap" below.)

## The policy is intentionally permissive when the setting is unset

The `IS NULL OR = ''` branch means "if `app.tenant_id` is unset, all rows are visible." This looks like it defeats the policy. It's deliberate:

- **Workers** carry mixed-tenant traffic. The outbox relay publishes events for all tenants in the same loop; the dispatcher's `_run_job` processes one tenant's job per call but the surrounding loops don't pin to any tenant. Forcing them to set the context per-statement would be invasive and offer no real defense (workers are trusted code paths).
- **Migrations** run as the table owner; with `FORCE ROW LEVEL SECURITY` they'd need a setting before every statement, breaking Alembic.
- **Boot-time queries** (e.g. health checks) have no user context.

The permissive escape preserves correctness for these trusted paths. The defense kicks in for the API process specifically.

## Why this is still meaningful even with the escape hatch

Two reasons:

1. **The defense applies where the risk is highest.** The API process is the surface most exposed to query bugs — it's where new endpoints are added, where the surface area churns, where one careless join becomes a leak. Workers are stable, narrow code paths reviewed against a small failure surface.
2. **A future hardening step is mechanical.** Add a non-owner DB role (`incident_app`), grant it CRUD on the tables but not table ownership, connect the API process as that role; the owner escape no longer applies to the API. Workers continue to connect as the owner. No application code changes needed.

The integration test `test_rls_enforcement.py` (Testcontainers, gated on `RUN_RLS_TEST=1`) demonstrates this future state: it creates `app_role` explicitly, grants it CRUD, and proves the policy blocks cross-tenant reads when the setting is wrong tenant + permits them when right + falls through (the escape) when unset.

## Alternatives considered

### Application-layer only

What we had before Phase 12 PR C. Necessary but, as argued above, not sufficient against the "forgot the WHERE clause" failure mode.

### Schema-per-tenant

Each tenant in its own Postgres schema (`SET search_path TO tenant_acme`). True isolation at the catalog level.

**Why not:**
- **Schema explosion.** 1000 tenants → 1000 copies of every table, every index, every constraint. DDL changes become 1000 `ALTER TABLE`s.
- **Cross-tenant analytics impossible.** Platform admin's "total jobs across all tenants" becomes a 1000-way UNION ALL.
- **Migration deployment becomes O(tenants).** Currently Alembic runs once.

### Database-per-tenant

Even stronger isolation; even worse operational story. Reserved for genuinely-regulated multi-tenancy (e.g. healthcare PHI). Not our trade-off.

### Application-layer ORM hook

A SQLAlchemy event listener that rewrites every query to inject `WHERE tenant_id = ?`. Tempting because it's "free."

**Why not:**
- It can be bypassed by raw SQL (we use SQLAlchemy core in a few places).
- It can't be applied to the rare query that *should* span tenants (admin analytics).
- It's a layer of magic between the developer and the SQL. Mistakes here are harder to find than mistakes in plain WHERE clauses.
- RLS is the same idea but enforced at the DB instead of the ORM, which means it survives even when someone reaches for raw SQL.

## Consequences

### Positive

- **Defense in depth.** A query bug in a new endpoint can't leak across tenants.
- **No application-code changes per query.** Once policies are in place, new endpoints get the protection automatically.
- **Postgres-native, no extension required.** RLS is in core since 9.5; no third-party dependency.
- **Provable.** The integration test demonstrates the policy fires under realistic conditions.

### Negative

- **Bootstrap complication.** `users` table can't have RLS because auth reads it before setting the context. Documented; tested.
- **SQLite test environment doesn't see RLS.** All unit + API tests run against SQLite, which has no concept of RLS. The integration test covers the gap, but day-to-day development can't catch RLS-specific bugs.
- **Performance.** Each query's WHERE clause grows by the policy predicate. The predicate is on an indexed column (`tenant_id`); EXPLAIN ANALYZE shows it folds into existing index scans. Measured negligible.
- **Operational footgun.** Forgetting to set `app.tenant_id` in a new code path means *all rows visible*, not "no rows visible". The fallout is silent. Mitigated by: only `get_current_user` is supposed to set it; new code paths inherit from that dependency.
- **Production deployment still requires a non-owner DB role to fully enforce.** Documented in the migration; not yet wired in Terraform. Tracked.

### Reversibility

`ALTER TABLE jobs DISABLE ROW LEVEL SECURITY` is one DDL statement per table. The application code that sets the variable is unconditional and harmless on SQLite (no-op) so removing it is purely cleanup.

## Verification

- `backend/tests/integration/test_rls_enforcement.py` — Testcontainers Postgres, non-superuser role, three assertions: scoped to A sees A's rows only; scoped to B sees B's only; unset sees both (proves the bootstrap escape works).
- `backend/alembic/versions/c4f8e9a52340_row_level_security.py` — the migration with inline documentation.

## Pointers

- `backend/app/dependencies.py` — `get_current_user` issues `set_config('app.tenant_id', …)`
- `backend/alembic/versions/c4f8e9a52340_row_level_security.py` — policy definitions
- `backend/tests/integration/test_rls_enforcement.py` — integration verification
