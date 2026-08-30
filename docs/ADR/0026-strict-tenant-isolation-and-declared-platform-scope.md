# ADR 0026 — Strict `tenant_isolation`: an unscoped statement is refused, and cross-tenant work declares itself

**Status:** Accepted · **Date:** 2026-08-30 · **Owner:** Platform

> **Amends [ADR 0003](0003-rls-as-defense-in-depth.md) and [ADR 0015](0015-force-rls-and-nonowner-app-role.md).**
> Both describe the unset-tenant escape hatch as deliberate, and ADR 0015 says in as many
> words: *"The unset-tenant escape hatch remains **load-bearing** under FORCE — do not
> tighten it."* This ADR tightens it, and the sections below are the argument for why that
> instruction was right about the consumers and wrong about the mechanism.

## Context

Every `tenant_isolation` policy created by `c4f8e9a52340` and `a7e3d9c41f28` opened with the
same two disjuncts:

```sql
current_setting('app.tenant_id', true) IS NULL
OR current_setting('app.tenant_id', true) = ''
OR tenant_id = current_setting('app.tenant_id', true)::uuid
```

A statement that had not set `app.tenant_id` satisfied the policy **unconditionally**, in
both `USING` and `WITH CHECK`, on all eleven tenant tables. The default was fail-open:
forgetting bought full cross-tenant read *and* write, with nothing raised and nothing logged.

plat #192 proved it live rather than by argument. `test_a_cleared_tenant_setting_turns_digest_isolation_off`
opened a connection that had never been scoped and showed an `INSERT` into another tenant's
`incident_summaries` returning `INSERT 0 1`, followed by a `SELECT` that read that tenant's
rows back. Its assertion message left an instruction for whoever changed it:

> "an unscoped session must be shown to fail OPEN — if this ever starts raising, the
> bootstrap branch changed and this finding needs rewriting, not deleting"

That is this ADR, and the test is rewritten rather than deleted:
`test_a_cleared_tenant_setting_is_refused_not_admitted`.

### The branch was not doing the job it was introduced for

ADR 0003 introduced the branch for one narrow reason, stated in its "Consequences →
bootstrap": authentication has to read a principal row *before* the request's tenant is
known. But the table that need names — `users` — **carries no policy at all**. It is the
single entry in `rls_check.RLS_EXEMPT_TABLES` and the single allowed exclusion in the
`test_rls_coverage` gate. The bootstrap need was already met by exemption.

So on the other ten tables the branch was serving no bootstrap purpose whatsoever. It was
load-bearing only by accident, for paths that had never been asked to declare themselves —
and it was carrying, silently, a set of consumers nobody had enumerated. Enumerating them
was most of this work order.

### Secondary defect, same cause

A pooled connection resets the GUC to the empty string rather than to unset. `''` fails the
first disjunct, passes the second — but a session that reached the third would evaluate
`''::uuid`, which Postgres refuses with `invalid_text_representation`. So the same mistake
produced either a silent leak or a hard error depending on connection history. Both are now
one clean refusal.

## Decision

### 1. The tenant match is the only way in, and it is NULL-safe

```sql
current_setting('app.tenant_scope', true) = 'platform'
OR tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
```

`nullif` folds unset and empty-string into the same deny: both yield NULL, `tenant_id = NULL`
is NULL, never true. Rows are filtered on read and rejected by `WITH CHECK` on write, without
reaching a cast that could raise.

### 2. Cross-tenant work declares itself: `app.tenant_scope = 'platform'`

The consumers ADR 0015 listed are real and still legitimate. The outbox relay publishes for
every tenant in one loop; the dispatcher polls pending jobs across all of them. What changes
is that they now **say so** rather than being admitted for having forgotten.

The mechanism resembles what it replaces — a session variable the policy consults — and the
resemblance is the objection worth answering. The difference is direction, and direction is
the whole point:

| | Before | After |
|---|---|---|
| A path that sets nothing | Full cross-tenant read + write | **Refused** |
| A path that crosses tenants | Indistinguishable from a bug | A declaration you can grep for |
| A new endpoint that forgets | Silently unprotected | Fails loudly on first use |
| A pooled connection reset to `''` | Admitted, or `''::uuid` raises | Refused |

Forgetting is no longer a way *in*. That is the property the finding asked for, and no
arrangement of a permissive default provides it.

`app/core/tenant_scope.py` is the only thing that sets the GUC, always **transaction-local**
(`set_config(..., true)`). The runtime shares one connection pool between requests and worker
loops (ADR 0015 §4), so a session-level `SET` would leak platform scope onto whichever
request checked that connection out next. Transaction scoping makes that impossible by
construction, and `test_platform_scope_is_what_lets_the_worker_loops_work` asserts the scope
does not outlive its transaction.

Five declaration sites, all audited:

| Consumer | How it declares |
|---|---|
| Worker loops + Kafka consumers | `platform_session_factory` handed to `worker_loop` (`main.py`) — an `after_begin` hook, so all ~30 `async with session_factory()` sites inherit it |
| Migration runs | `alembic/env.py` issues a session-level `SET` inside alembic's own transaction |
| `seed_eval_fixtures.py` | `platform_session_factory` |
| `reset_eval_state.py` | `platform_session_factory` |
| `seed_incident_commander.py` | `platform_session_factory` |

### 3. `service_accounts` keeps a narrow, SELECT-only bootstrap read

This is the one genuine non-`users` bootstrap consumer, and unlike the others it cannot be
fixed by declaring anything. `get_current_principal` calls `verify_token`, which reads
`service_accounts` two statements before `_apply_tenant_context` issues `set_config` on the
same transaction. It cannot name the tenant first — **the row it is fetching is what tells it
which tenant this is.** With a strictly scoped policy the read returns `None` and every
service-account request fails `AuthenticationError("Service account not found")`, taking the
entire MCP surface with it.

A separate permissive policy restores exactly that read and nothing else:

```sql
CREATE POLICY service_accounts_bootstrap_read ON service_accounts
  FOR SELECT USING (nullif(current_setting('app.tenant_id', true), '') IS NULL)
```

`FOR SELECT` only, so every INSERT/UPDATE/DELETE on the table stays strictly scoped; and it
admits only sessions that have named *no* tenant, so it cannot be used as a cross-tenant
window from inside an authenticated request. `test_service_accounts_preauth_read_survives_but_writes_do_not`
pins all three properties.

This is the shape ADR 0003's bootstrap argument always described. It just belonged on one
table, for one command — not on eleven tables for all four.

### 4. Three request paths that were never scoped at all are now scoped

The sweep found three paths that authenticate *themselves* and so never pass through
`get_current_user`. Each was relying on the bootstrap branch, which means each had **no RLS
backstop whatsoever** — including the audit write for every login in the system:

| Path | Touched | Now |
|---|---|---|
| `POST /auth/register` | `audit_logs` INSERT | `declare_tenant_scope(..., tenant.id)` |
| `POST /auth/login` | `audit_logs` INSERT | `declare_tenant_scope(..., user.tenant_id)` |
| `GET /jobs/{id}/stream` | `jobs` SELECT | `declare_tenant_scope(..., tenant_id)` from the signed stream token |

All three already knew their tenant; none of them said it. These are strict improvements —
the query narrows, and the stream token's tenant was already validated against the job id
(ADR 0014).

### 5. `deploy_markers` keeps `OR tenant_id IS NULL` — unchanged and deliberate

Per ADR 0015: `tenant_id` is nullable by design because deploys are platform-wide, and hiding
NULL rows from tenant-scoped sessions would silently degrade `get_deploy_history` (MCP, under
a service-account context) to its env-var fallback. That variant is preserved verbatim.

Worth naming what the change does to it anyway: an unscoped session may still write a
**NULL-tenant** marker, but can no longer forge one belonging to a named tenant. The
remaining admission is bounded to rows that belong to nobody.
`test_unscoped_deploy_marker_write_is_limited_to_platform_rows` fixes that boundary.

### 6. Migrations declare scope in `env.py`, not one revision at a time

This is not hypothetical for *past* revisions. Four merged migrations run DML against
policy-covered tables, and three land **after** `a7e3d9c41f28` turned FORCE on — which binds
the owner they connect as:

| Revision | DML | Position |
|---|---|---|
| `a5c19d3f7e42` | `UPDATE audit_logs` backfill | after the policies, before FORCE |
| `b1f39d7c2a84` | `UPDATE jobs SET heartbeat_at` | after FORCE |
| `d1f6a2b940c7` | `saga_step_index` backfill | after FORCE (previous head) |
| `c9e41a7b62d5` | `DELETE FROM idempotency_records` (downgrade) | after FORCE |

Under a strict policy an owner-connected `UPDATE` matches no rows and reports `UPDATE 0` — a
**silent** no-op, which for a backfill is worse than an error. Merged migrations are frozen
history and are not edited; `env.py` declares the scope once for the whole run, covering
every past and future revision. The integration fixture replays the entire chain from empty
on every CI run, so this is exercised rather than asserted.

## Alternatives considered

### A dedicated Postgres role with `BYPASSRLS` — **not available here**

The work order named this as the likely shape, and it is the textbook answer. It does not
survive contact with this deployment:

- **`BYPASSRLS` can only be granted by a superuser.** Production's table owner is the RDS
  master, which is *not* a superuser and has no `BYPASSRLS` — ADR 0015 establishes this, and
  it is the same fact that makes `FORCE` sufficient there. So the migration chain **cannot
  create such a role on RDS at all**. This is a hard blocker, not a preference.
- It also reintroduces exactly what ADR 0015 removed. The owner exemption was the bug
  (F1-01); a bypass attribute is the owner exemption with a different name.

### A role-membership predicate instead of `BYPASSRLS`

`pg_has_role(current_user, 'incident_platform', 'MEMBER')` in the policy avoids the
superuser problem — the check is data, not an attribute. But it only means anything if the
workers *connect* as a different role, and they do not: API, workers and MCP share one engine
(ADR 0015 §4). Splitting them requires a second engine, a second pool against a db.t3.micro,
a new secret, new Terraform, and a third rollout phase. ADR 0015 considered a second engine
for the workers and rejected it. The GUC gets the same fail-closed default with none of that,
and the residual trust — worker code is trusted to be cross-tenant — is unchanged either way,
because a role the workers connect as is equally available to any query they run.

### Make the workers set `app.tenant_id` per unit of work

Right in spirit, wrong about the workload. The dispatcher's `_run_job` handles one tenant's
job and could scope itself, but the *poll that finds the job* is cross-tenant by definition,
as is the outbox relay's fetch, the stale-running sweep and the idempotency reaper. Scoping
those means "one transaction per tenant per tick", which is a different scheduler. It would
also have meant editing ~30 session sites in `dispatcher.py` alone, each a chance to miss one
— and a missed one fails closed but silently (zero rows), which for the outbox relay means
all Kafka delivery stops.

### Drop the branch and let `''::uuid` raise

Simplest diff, worst behaviour: cross-tenant mistakes would surface as
`invalid_text_representation` on some connections and as clean denials on others, depending
on pool history. `nullif` makes the outcome the same either way.

## Consequences

### Positive

- The wave-wide fail-open default is gone: an unscoped statement on a non-bootstrap tenant
  table is **refused**, on all eleven tables, for read and write.
- The `''`-reset hazard is closed by the same predicate.
- Three request paths that had no RLS backstop at all now have one.
- "Which code may cross tenants" became an answerable question — five sites, one module.
- The residual admissions are bounded and each has a test: one table for one command
  (`service_accounts` SELECT), and rows belonging to nobody (`deploy_markers` NULL-tenant).

### Negative / to watch

- **A forgotten declaration now breaks things.** A new background loop built on the plain
  session factory will read zero rows and fail its writes. That is the intended direction —
  loud beats silent — but it is a new way to break a worker, and the symptom for a *read* is
  an empty result rather than an error.
- **`app.tenant_scope` is trusted code's to set.** It is not an authorisation boundary
  against an attacker who can already run arbitrary SQL as `incident_app`; neither was the
  old hatch, and neither is any GUC. What it buys is that *accidents* fail closed.
- **The `service_accounts` SELECT remains readable unscoped.** Bounded to one command on one
  table, and it leaks only what a token-bearer could already resolve, but it is the one place
  the old shape survives.
- **SQLite still sees none of this.** The unit/API suite has no RLS; only the Docker-gated
  integration tier proves enforcement. That tier now runs in CI on every PR (#151), which is
  what made this change verifiable at all.

### Reversibility

`alembic downgrade -1` restores the previous permissive policies verbatim (the legacy
predicates are kept in the migration for exactly this) and drops the bootstrap-read policy.
The application-side declarations are harmless against the old policies — a session that
declares a scope the policy does not consult simply sets an unread GUC — so code and schema
can be rolled back independently, in either order.

## Verification

- `backend/tests/integration/test_rls_enforcement.py` — `test_a_cleared_tenant_setting_is_refused_not_admitted`
  (#192's proof, inverted: unscoped write refused, unscoped read empty, `''` handled);
  `test_unscoped_writes_are_refused_on_every_tenant_table` (reads the shipped predicates back
  out of `pg_policies` for all eleven tables, rather than trusting the migration looped);
  `test_platform_scope_is_what_lets_the_worker_loops_work` (declared scope spans tenants and
  does not outlive its transaction); `test_service_accounts_preauth_read_survives_but_writes_do_not`;
  `test_unscoped_deploy_marker_write_is_limited_to_platform_rows`;
  `test_rls_isolates_tenants` (the "unset sees both" assertion inverted).
- The `rls_db` fixture replays `upgrade head` → `downgrade` → `upgrade head`, so the new
  migration's downgrade and the four DML revisions above run under the strict policy on every
  CI run.
- `backend/tests/unit/test_rls_coverage.py` — unchanged and still passing: the new migration
  declares `_TABLES` / `_NULL_TENANT_TABLES` under the names the gate discovers.

## Pointers

- `backend/alembic/versions/e2a9c4f70b31_rls_strict_tenant_isolation.py` — the policy swap
- `backend/app/core/tenant_scope.py` — the only thing that sets either GUC
- `backend/alembic/env.py::_declare_platform_scope` — migration-run scope
- `backend/app/main.py` — `platform_session_factory` handed to `worker_loop`
- `backend/app/services/auth.py`, `backend/app/api/streaming.py` — the three self-authenticating paths
- `backend/app/dependencies.py` — `get_current_principal` / `verify_token` ordering that forces the `service_accounts` exception
