# ADR 0015 — FORCE row-level security, the non-owner `incident_app` runtime role, and DB-level audit_logs immutability

**Status:** Accepted · **Date:** 2026-08-09 · **Owner:** Platform

> **Amends [ADR 0003](0003-rls-as-defense-in-depth.md).** ADR 0003's design — permissive
> `tenant_isolation` policies with a deliberate unset-tenant escape hatch — stands unchanged.
> What this ADR adds: the policies now *bind the table owner* (`FORCE ROW LEVEL SECURITY`),
> every tenant table is covered (not just the six that existed in Phase 12), and `audit_logs`
> is immutable at the database layer. It also corrects one factual claim in ADR 0003 — the
> assertion that FORCE would break Alembic — which was the stated reason FORCE was omitted.

## Context

The 2026-08-08 audit found that RLS, as shipped, was inert in production (F1-01), incomplete
(F1-05), and that audit immutability existed only as prose (F1-07):

- **Inert:** `infra/variables.tf` sets `db_username = "appuser"`; `infra/rds.tf` makes that
  the RDS master user; `infra/secrets.tf` builds `DATABASE_URL` from it. The application
  therefore connects as the **owner of every table**, and Postgres exempts table owners from
  row security unless the table sets `FORCE ROW LEVEL SECURITY`. Migration `c4f8e9a52340`
  only ran `ENABLE`. Net effect: not one production query has ever been constrained by the
  policies ADR 0003 introduced.
- **Incomplete:** five tenant tables created after `c4f8e9a52340` shipped with no policy at
  all — `incident_summaries`, `service_accounts`, `alerts`, `idempotency_records`,
  `deploy_markers`. Nothing gated the pattern "new tenant table ⇒ new policy".
- **Docstring-only immutability:** `AuditLog` says "immutable append-only record", but any
  session with DML rights (i.e. the owner the app connects as) could `UPDATE`/`DELETE` audit
  rows.

## Decision

Migration `a7e3d9c41f28` (this change) plus a follow-up role split (phase 2, below):

1. **`FORCE ROW LEVEL SECURITY` on all 11 tenant tables** — the six from `c4f8e9a52340`
   (`jobs`, `audit_logs`, `outbox_events`, `job_events`, `sagas`, `job_triages`) and the five
   newly covered ones. FORCE removes the owner exemption; policies now apply to the RDS
   master connection.
2. **`tenant_isolation` policies on the five missing tables**, with the exact policy text
   from `c4f8e9a52340` — the unset-tenant escape hatch (`current_setting('app.tenant_id',
   true) IS NULL OR … = ''`) is deliberately preserved. `deploy_markers` gets a variant that
   also admits `tenant_id IS NULL` rows in USING and WITH CHECK: its `tenant_id` is nullable
   by design (deploys are platform-wide today — see the model docstring), and the standard
   shape would hide every NULL row from tenant-scoped sessions, silently degrading
   `get_deploy_history` (MCP, runs under a service-account tenant context) to its env-var
   fallback.
3. **RESTRICTIVE deny policies on `audit_logs`** for UPDATE and DELETE (`USING (false)`).
   Restrictive policies AND with the permissive `tenant_isolation` policy, so with FORCE on
   they bind owner and app role alike.
4. **Phase 2 — non-owner runtime role (`incident_app`), tracked separately:** create the role
   in Terraform, grant it CRUD on the tenant tables (minus UPDATE/DELETE on `audit_logs`),
   and point the API's `DATABASE_URL` at it; the owner remains for migrations and workers.
   This is the "future hardening step is mechanical" paragraph of ADR 0003, made real. It is
   not required for enforcement anymore (FORCE already binds the owner) but restores least
   privilege and turns audit tampering from a silent no-op into a loud
   `insufficient_privilege` error.

## Correcting ADR 0003's "FORCE breaks Alembic" claim

ADR 0003 gave this reason for omitting FORCE:

> "Migrations run as the table owner; with `FORCE ROW LEVEL SECURITY` they'd need a setting
> before every statement, breaking Alembic."

That claim is incorrect, for two independent reasons:

1. **The policy's own escape hatch admits tenant-less sessions.** Every `tenant_isolation`
   USING/WITH CHECK clause begins with `current_setting('app.tenant_id', true) IS NULL OR
   … = ''`. An Alembic session never sets `app.tenant_id`, so under FORCE every row still
   passes both clauses — no per-statement setup, nothing breaks. The escape hatch ADR 0003
   built for workers is precisely what makes FORCE safe for migrations too.
2. **Row security never constrains DDL.** `ALTER TABLE`, `CREATE INDEX`, `CREATE POLICY` and
   friends are not row-returning or row-writing commands; RLS applies to
   SELECT/INSERT/UPDATE/DELETE. A migration that backfills data falls under reason 1.

Verified live, not just argued: the integration harness runs the entire Alembic chain —
including this migration and every earlier one — against Postgres 16 with FORCE active, plus
a `downgrade -1` / re-`upgrade head` round-trip.

## Why FORCE alone already enforces in production

The RDS master is the owner of every table but it is **not** a superuser and has no
`BYPASSRLS` (RDS never grants either to the master user). Owner exemption was the only thing
keeping the policies off that connection; FORCE removes it. So this migration flips RLS live
for the production connection at the next deploy that runs migrations — no role or
connection-string change needed.

The unset-tenant escape hatch remains **load-bearing** under FORCE — do not tighten it:

- migrations (the entrypoint runs `alembic upgrade head` as the owner, no tenant context);
- worker loops (dispatcher, outbox relay, reaper, digest) — mixed-tenant by design (ADR 0003);
- boot-time checks and health probes;
- the pre-auth `service_accounts` lookup in `get_current_principal`
  (`backend/app/dependencies.py`): `verify_token` reads `service_accounts` *before*
  `_apply_tenant_context` runs in the same transaction — with a strict policy and FORCE, no
  service account could ever authenticate.

Request-context write paths were audited for WITH CHECK safety at this revision: `replay_job`
and `resolve_incident` write under `current_user.tenant_id`; the platform-admin digest
override re-issues `set_config` to the override tenant before writing `incident_summaries`
(`resolve_admin_tenant`); chaos `bad_deploy` writes `alerts` under the service account's own
tenant; register/login touch only `users`/`tenants` (uncovered). No cross-tenant-write path
exists, so nothing starts failing WITH CHECK when FORCE lands.

## Audit immutability: restrictive policies, not a trigger, not grants alone

- **Trigger raising on UPDATE/DELETE — rejected.** `audit_logs.user_id` and `audit_logs.job_id`
  are FKs with `ON DELETE SET NULL`. Deleting a job or user performs a genuine UPDATE on the
  referencing audit rows via the referential action — which would fire the trigger.
  `scripts/reset_eval_state.py` deletes jobs and chaos-owner users routinely; a raising
  trigger breaks every eval reset. Referential-integrity actions **bypass row security**, so
  restrictive policies do not have this failure mode.
- **Grant revocation alone — insufficient.** Revoking UPDATE/DELETE binds nothing while the
  app connects as the owner (owners hold implicit rights). It becomes a valuable second layer
  in phase 2.
- **RESTRICTIVE deny policies — chosen.** ANDed with the permissive policy; with FORCE they
  bind owner and app role alike. The failure mode is a *silent no-op* — command tag
  `UPDATE 0` / `DELETE 0`, not an error. The loud `insufficient_privilege` variant arrives
  with the phase-2 grant revoke.
- **Deliberately not extended** to `job_events` or `outbox_events`: the outbox relay
  legitimately UPDATEs `outbox_events.published_at`, and `job_events` immutability is a
  separate decision, out of scope here.

## Coverage rule and the single exclusion

Every table with a `tenant_id` column must appear in an RLS migration's policy-table list.
The unit gate `backend/tests/unit/test_rls_coverage.py` enforces this in plain CI (no
Docker) by diffing `Base.metadata` against the migration modules' declared lists. The single
allowed exclusion is `users`: authentication must read the users row *before* the request's
`app.tenant_id` exists (ADR 0003's bootstrap consequence). `tenants`, `job_dependencies` and
`service_account_tokens` carry no `tenant_id` column (`job_dependencies` is reached only
through `jobs`, which is covered; `service_account_tokens` is reached through
`service_accounts`).

## The two-phase production rollout

1. **Phase 1 (this change):** FORCE + full policy coverage + audit deny policies, all in one
   dialect-guarded migration. Enforcement begins the moment the next production deploy runs
   `alembic upgrade head`. Nothing about the connection changes; the WITH CHECK audit above
   is what makes this safe to flip.
2. **Phase 2 (role split, tracked):** the Terraform-managed `incident_app` non-owner role and
   the API connection-string switch, plus revoking UPDATE/DELETE on `audit_logs` from it.
   Independent of phase 1 and deliberately a separate, individually revertible deploy.

## Consequences

- **Positive:** RLS is real in production for the first time; the "forgot the WHERE clause"
  bug class is now actually caught where it matters. New tenant tables can't silently ship
  without a policy (unit gate). Audit history can't be rewritten through the app connection.
- **Negative / to watch:**
  - Any *future* request-context write whose row `tenant_id` differs from the session's
    `app.tenant_id` fails WITH CHECK at runtime instead of silently succeeding. That is the
    point, but it moves a class of bug from "silent data issue" to "500 + rollback".
  - Superusers still bypass RLS entirely: local docker-compose connects as `postgres`
    (superuser), and the SQLite suite has no RLS at all — only the Docker-gated integration
    tier (`RUN_RLS_TEST=1`) proves enforcement.
  - Audit UPDATE/DELETE from the app is a silent `UPDATE 0` until phase 2 makes it loud.

## Verification

- `backend/tests/unit/test_rls_coverage.py` — model-vs-policy completeness gate (fails in
  plain CI if a tenant_id table ships without a policy; encodes the `users` exclusion).
- `backend/tests/integration/test_rls_enforcement.py` (Testcontainers Postgres 16,
  `RUN_RLS_TEST=1`) — posture: `relrowsecurity` AND `relforcerowsecurity` true for all 11
  tables; `alerts` tenant isolation; `deploy_markers` NULL-tenant visibility under a scoped
  session; `audit_logs` UPDATE/DELETE command tags `UPDATE 0`/`DELETE 0` with the row
  unchanged; job delete still nulls `audit_logs.job_id` through the FK (RI bypasses RLS).
- Alembic `upgrade head` → `downgrade -1` → `upgrade head` round-trip on Postgres 16.

## Pointers

- `backend/alembic/versions/a7e3d9c41f28_rls_force_and_full_coverage.py` — the migration
- `backend/alembic/versions/c4f8e9a52340_row_level_security.py` — the original six policies
- `infra/variables.tf`, `infra/rds.tf`, `infra/secrets.tf` — why the app connects as the owner
- `backend/app/dependencies.py` — `set_config` sites (`get_current_user`,
  `_apply_tenant_context`, `resolve_admin_tenant`)
- `backend/app/models/deploy_marker.py` — nullable `tenant_id` rationale
- `scripts/reset_eval_state.py` — the job/user deletions the no-trigger decision protects
