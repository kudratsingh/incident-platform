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
4. **Phase 2 — non-owner runtime role (`incident_app`), landed with WO-P2-03:** migration
   `b8e4a1c92f35` creates the role (guarded `CREATE ROLE ... LOGIN`, **no password** — see
   the password-sync note below) and grants it DML on all tables minus UPDATE/DELETE on
   `audit_logs`; Terraform owns the password (`random_password.app_db` → the
   `app-db-password` secret) and the runtime `DATABASE_URL` flip, while migrations move to
   `ALEMBIC_DATABASE_URL` (the owner URL — `alembic/env.py` prefers it). The API, worker
   loops and MCP share one engine (`backend/app/dependencies.py`), so the flip moves all
   three; the owner remains for **migrations only**. (A second owner engine for the workers
   was considered and rejected: tenant-less worker sessions are exactly what the escape
   hatch admits, and a second pool against a db.t3.micro buys zero security.) This restores
   least privilege — no DDL, no TRUNCATE, no DROP POLICY on the network-facing process —
   and turns audit tampering from a silent no-op into a loud `insufficient_privilege` error.
5. **Boot-time guardrails (WO-P2-03):** `app/core/db_bootstrap.py`, an idempotent
   injection-safe password sync run before uvicorn on every boot (the migration cannot own
   the password: it runs exactly once, on a phase-1 deploy that predates the Terraform
   secret — the sync also gives free rotation: change the secret, bounce the service); and
   `app/core/rls_check.assert_rls_posture`, a probe in both lifespans that detects an
   RLS-inert connection (superuser, or owner without FORCE), logs ERROR always, and refuses
   to serve only in production — local superuser stacks and the commander's pinned eval
   stack must keep booting.

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
  app connects as the owner (owners hold implicit rights). Phase 2 made it the first line:
  `incident_app` holds no UPDATE/DELETE on `audit_logs`, so tampering from the runtime is a
  loud `insufficient_privilege` error.
- **RESTRICTIVE deny policies — chosen (and kept).** ANDed with the permissive policy; with
  FORCE they bind owner and app role alike. For an owner-connected session (migrations,
  operator scripts on the owner URL) the failure mode is a *silent no-op* — command tag
  `UPDATE 0` / `DELETE 0`, not an error; that back-stop is why the policies stay even now
  that the grant revoke fails the runtime loudly first.
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

The ordering below is load-bearing — it is the whole reason the password lives in a
boot-time sync and migrations get their own URL. **Sequence the phases; never parallelize.**

1. **Phase 1 — merge and deploy the image, Secrets/task-def UNCHANGED.** The entrypoint's
   `alembic upgrade head` still runs on the master `DATABASE_URL` and executes
   `b8e4a1c92f35`: `incident_app` now exists with its grants, but — passwordless under
   scram — cannot yet log in. The app keeps running as the master (RLS already enforced via
   FORCE), and `db_bootstrap` no-ops because `INCIDENT_APP_DB_PASSWORD` does not exist yet.
2. **Phase 2 — `terraform apply`, then force a new ECS deployment.** The apply creates the
   `app-db-password` and `database-url-owner` secrets, repoints the `database-url` secret at
   `incident_app`, registers the task-definition revision carrying `ALEMBIC_DATABASE_URL` +
   `INCIDENT_APP_DB_PASSWORD`, and extends the execution role's `GetSecretValue` resource
   list (forgetting the IAM entries kills every new task at provisioning, before the
   container starts). On the next task boot: alembic runs on `ALEMBIC_DATABASE_URL` (owner),
   `db_bootstrap` sets `incident_app`'s password, and uvicorn connects as `incident_app`.
   Note the backend service's `lifecycle { ignore_changes = [task_definition] }`: CI's
   deploy job re-renders only the image on the current family revision, so the
   Terraform-registered revision is picked up by the **next CI deploy — apply Terraform
   before triggering it.**

Two ordering hazards, both by construction rather than by care:

- Flipping the `database-url` secret early cannot break a RUNNING task (ECS injects secrets
  at task start), but any restart before phase 1's migration has run crash-loops on auth
  failure — hence phases, not one big-bang deploy.
- The password deliberately does NOT live in the role-creating migration: on phase 1 the env
  var doesn't exist, migrations never re-run, and the role would be passwordless forever.
  The boot-time sync is the only ordering-safe place, and rotation comes free (change the
  secret, bounce the service).

**Grants maintenance caveat:** `ALTER DEFAULT PRIVILEGES` only covers objects created by the
role that issued it — the master, which runs all migrations. If migrations are ever run as a
different role, future tables silently lose `incident_app`'s grants. And the default
privileges hand out full DML: any future *immutable* table needs its own explicit
`REVOKE`, like `audit_logs` got in `b8e4a1c92f35`.

Rollback: phase 2 reverts by re-applying the previous secret/task-def state (the owner URL
still exists in `database-url-owner`); phase 1 reverts with `alembic downgrade -1`, which
drops the role after revoking its grants. Each phase is individually revertible.

## Consequences

- **Positive:** RLS is real in production for the first time; the "forgot the WHERE clause"
  bug class is now actually caught where it matters. New tenant tables can't silently ship
  without a policy (unit gate). Audit history can't be rewritten through the app connection.
- **Negative / to watch:**
  - Any *future* request-context write whose row `tenant_id` differs from the session's
    `app.tenant_id` fails WITH CHECK at runtime instead of silently succeeding. That is the
    point, but it moves a class of bug from "silent data issue" to "500 + rollback".
  - Superusers still bypass RLS entirely. Since WO-P2-03 the compose `app`/`mcp` services
    connect as `incident_app` (the migrate one-shot keeps the superuser URL for DDL), so the
    local topology no longer lies — but the SQLite suite has no RLS at all, and only the
    Docker-gated integration tier (`RUN_RLS_TEST=1`) proves real enforcement. The boot
    posture probe logs ERROR on any superuser stack as a standing reminder.
  - Audit UPDATE/DELETE from the runtime is now a loud `insufficient_privilege` error
    (phase 2); owner-connected sessions still get the deny policies' silent `UPDATE 0`.
  - Operator scripts that genuinely need owner powers (e.g. an ad-hoc backfill) must be run
    with the owner URL (`database-url-owner`); `reset_eval_state.py` and the seeders work
    unchanged as `incident_app` — their deletes ride the FK referential actions, which
    execute with the table owner's privileges.

## Verification

- `backend/tests/unit/test_rls_coverage.py` — model-vs-policy completeness gate (fails in
  plain CI if a tenant_id table ships without a policy; encodes the `users` exclusion).
- `backend/tests/integration/test_rls_enforcement.py` (Testcontainers Postgres 16,
  `RUN_RLS_TEST=1`) — the non-superuser sessions connect as the real `incident_app` role,
  created by the migration chain and password-synced by `python -m app.core.db_bootstrap`
  (the exact entrypoint steps). Asserts: tenant isolation on `jobs`/`alerts`;
  `deploy_markers` NULL-tenant visibility; `audit_logs` UPDATE/DELETE raise
  `insufficient_privilege` with the row unchanged while INSERT still succeeds; job delete
  still nulls `audit_logs.job_id` through the FK (referential actions run with the table
  owner's privileges); CREATE/ALTER TABLE as `incident_app` refused; `alembic_version`
  readable (boot checks); `assert_rls_posture` passes on a live `incident_app` engine and
  raises on a superuser engine under production settings.
- `backend/tests/unit/test_rls_check.py` — the probe's raise/log/no-op matrix on SQLite;
  `backend/tests/unit/test_db_bootstrap.py` — no-op discipline and the injection-safe
  `set_config` + `format('%L', ...)` statement shape.
- Alembic `upgrade head` → `downgrade -1` → `upgrade head` round-trip on Postgres 16 (runs
  inside the integration fixture, covering the role migration's downgrade).

## Pointers

- `backend/alembic/versions/a7e3d9c41f28_rls_force_and_full_coverage.py` — the FORCE migration
- `backend/alembic/versions/b8e4a1c92f35_incident_app_role.py` — the role + grants migration
- `backend/app/core/db_bootstrap.py` — boot-time password sync; `backend/app/core/rls_check.py`
  — the posture probe (both lifespans call it after `assert_migrations_current`)
- `backend/alembic/versions/c4f8e9a52340_row_level_security.py` — the original six policies
- `infra/secrets.tf`, `infra/ecs.tf`, `infra/iam.tf` — the two-URL scheme: runtime
  `database-url` (incident_app), `database-url-owner` + `app-db-password`, task-def secrets,
  execution-role ARNs
- `infra/variables.tf`, `infra/rds.tf` — why the owner is the RDS master
- `backend/app/dependencies.py` — `set_config` sites (`get_current_user`,
  `_apply_tenant_context`, `resolve_admin_tenant`)
- `backend/app/models/deploy_marker.py` — nullable `tenant_id` rationale
- `scripts/reset_eval_state.py` — the job/user deletions the no-trigger decision protects
