# ADR 0007 — Machine principals with a scope model separate from human roles

**Status:** Accepted (Wave 1 PR #52) · **Date:** 2026 Q3 · **Owner:** Platform

## Context

Today the platform has one principal shape: a `User` row with a `role` enum of `user | support | admin` plus an additive `is_platform_admin` boolean. Every request is authenticated as a human. The role is coarse and hierarchical — a `support` user can do everything a `user` can, an `admin` can do everything a `support` can.

The agent-platform program introduces a second principal shape: a **machine principal** representing a service account that speaks to the platform via the MCP server ([ADR 0006](0006-mcp-server-standalone-process.md)). The first such principal will be `incident-commander`, seeded read-only.

We need to decide:

1. Are machine principals a new table or a flag on `users`?
2. Do they reuse the role enum, or get their own permission model?
3. What permission granularity does an agent need?

The wrong answers here are expensive to unwind because tokens are minted with whatever model we pick.

## Decision

Introduce two new concepts:

1. A `service_accounts` table — machine principals are first-class, not a boolean on `users`. Every row has `id`, `tenant_id`, `name`, `is_active`, `created_by_user_id`, `revoked_at`.
2. A **scope model** distinct from the role enum. Tokens carry a set of *scopes*; each API endpoint declares the scope(s) it requires. Scopes are non-hierarchical, additive, and orthogonal to roles.

### Scopes (fixed set, locked in during Step 0)

| Scope | What it grants |
|---|---|
| `telemetry:read` | Consumer lag, queue depth, in-flight counts, trace lookups, health snapshots — the observability read surface. |
| `incidents:read` | DLQ contents, incident summaries, saga state, per-job history, and the audit log (via `list_audit_events`) — the incident-response read surface. |
| `actions:propose` | Create a *proposal* for a Tier 1 or Tier 2 action; does not execute. |
| `actions:execute` | Execute an approved proposal (Tier 1 idempotent, Tier 2 requires approval reference). |
| `chaos:invoke` | Invoke chaos framework tools. Gated additionally by `CHAOS_ENABLED` — see [ADR 0008](0008-chaos-gating.md). |

The seed principal `incident-commander` gets `telemetry:read + incidents:read` and nothing else. Wave 3 introduces `actions:propose` and `actions:execute` behind the approvals subsystem.

### Why scopes, not new roles

- **Roles describe a *person's* job.** A support engineer's role bundles "answer tickets + look up jobs + kick a stuck one" because that's how humans work. An agent's job is different and evolves per Wave — bundling those into `agent-user | agent-support | agent-admin` copies the wrong shape.
- **Scopes are the standard OAuth/OIDC vocabulary.** Every reviewer of this system will recognize `telemetry:read` immediately; nobody will need to look up what `agent-support` means.
- **Non-hierarchical is a feature.** An agent with `actions:execute` should not automatically have `chaos:invoke`. Roles imply "more privileged"; scopes imply "different capability."
- **Revocation is per-capability.** Kill switch on the agent principal can drop `actions:execute` while keeping `telemetry:read` — the agent stays observable but can't act. A role-based model would force a full demotion.

### Why a separate table, not a flag on `users`

- **Different lifecycle.** Service accounts don't have passwords, don't reset them, don't log in through a browser. They have tokens. Cramming that into the `users` table means half the columns are always null for one shape and half for the other.
- **Different audit story.** Every action taken by a service account records `principal_type=service_account, principal_id=<sa_id>`. Distinguishing at the row level rather than at a boolean is a lot cleaner for filters and dashboards.
- **Cross-tenant intent is clearer.** A service account is bound to exactly one tenant; there's no analog of `is_platform_admin` for machines. Encoding this in the schema (`service_accounts.tenant_id NOT NULL`, no platform flag) rules out an entire class of accident.

## Tokens

Service account tokens are:

- **Long-lived** (default 90-day expiry, renewable). Machines don't do interactive refresh flows.
- **Scoped** (the token's scopes are a subset of the account's scopes, chosen at mint time — supports capability-narrowing without creating a new account).
- **Revocable** (single `revoked_at` column; middleware rejects revoked tokens on the next request).
- **Rate-limited independently** (per-service-account bucket, distinct from the per-tenant bucket humans share).
- **Not JWT.** Opaque bearer tokens issued as `sa_<random>` with a lookup table. JWTs need key rotation infrastructure we don't have; opaque tokens make revocation instant instead of dependent on key rotation.

## Audit event naming (locked in during Step 0)

Machine-principal actions emit audit events in the existing `<resource>.<verb>` snake-case shape:

- `service_account.created` / `service_account.token_minted` / `service_account.token_revoked`
- `agent.tool_invoked` — every MCP tool call (the audit log carries `tool_name`, `arguments`, `scope_used`, `latency_ms`, `outcome`)
- `agent.action_proposed` / `agent.action_approved` / `agent.action_executed` / `agent.action_rejected`
- `chaos.tool_invoked` — separate stream from `agent.tool_invoked` so chaos activity is easy to filter

Every entry is a row in `audit_logs` with `principal_type='service_account'` set, so the existing admin Audit tab and event-sourcing infrastructure work unchanged.

## Alternatives considered

### Reuse `users` with `is_service_account=true`

Cheapest to add. One migration. No new table.

**Why not:** the lifecycle divergence (auth flow, credentials, expiry, revocation, audit-log distinguishing) means half the `users` code paths grow a `if user.is_service_account:` fork. The table shape lies about what a row represents.

### Reuse the role enum, add `agent` as a new role

Consistent with how the platform already models permission. Every existing dependency (`require_admin`, etc.) continues to work.

**Why not:**
- Agent capabilities don't map to the human-role bundle. `agent` would end up as "some `support` powers + some `admin` powers + some capabilities no human role has". A role that means "collection of unrelated capabilities" is a scope by another name.
- Wave 3 needs per-capability revocation (kill switch drops `actions:execute` but not `telemetry:read`). A single role field can't express this.
- Muddles the "who is doing this" answer in the audit log.

### Copy AWS IAM's policy JSON model

Maximum flexibility. Arbitrary allow/deny per resource pattern.

**Why not:** overkill for the number of tools we're shipping (single-digit in Wave 1, ~20 by end of Wave 3). Fixed enum scopes are debuggable and gRPC-serializable; a policy language becomes its own testing surface.

### Per-tool tokens instead of scopes

Mint one token per MCP tool the agent needs. Extreme least-privilege.

**Why not:** operationally miserable. Wave 3 introduces ~20 tools; the agent would juggle 20 tokens. Bundling by capability class (`telemetry:read` covers all read observability tools) is the natural granularity.

## Consequences

### Positive

- **Least-privilege by construction.** Seed principal is read-only; execute powers require an explicit scope added later. No accidental privilege creep.
- **Kill switch is a scope revocation.** Wave 3's per-principal kill switch works by clearing scopes on the token, not by deleting the account. Reversible; audited.
- **Clean split from human auth.** No `if service_account else user` branches in the human login flow. Different auth paths for different principal shapes.
- **Auditability by principal type.** Filters on the audit tab can slice `principal_type='service_account'` cleanly; `agent.*` events show only agent activity.

### Negative

- **New table, new migration, new dependency function.** ~200 lines of code to introduce in Wave 1 PR #1 (machine principals + scoped tokens). Modest, but real.
- **Two auth paths to secure.** JWT for humans, opaque token for service accounts. Both go through the same middleware but the middleware branches. Tested via `test_scope_enforcement.py` (wrong-scope → 403; revoked → 401; per-SA rate limit trips before per-tenant).
- **Scope model is fixed at Step 0.** Adding a new scope later is easy; renaming or splitting an existing one is a token-migration operation. This is why the set was decided upfront and locked before shipping code.
- **The MCP handler must declare "which tool needs which scope" and enforce it before dispatch.** The service-layer call underneath does not know it came from MCP, so the handler owns the scope check. A mistake surfaces as a 403 rather than as silent success.

### Reversibility

Dropping the scope model means folding all scopes into a single role bundle — mechanical migration. Dropping the service account table means moving rows into `users` with `is_service_account=true` — also mechanical, though the audit-log queries need a rewrite. Neither is planned; both are possible.

## Verification

- Unit tests: token minting, scope-subset validation at mint time, expiry checks, revocation behaviour.
- API tests: `wrong-scope 403`, `revoked-token 401`, `expired-token 401`, `per-sa rate limit 429`, `no scope required → allowed` (per PR-1 test bar).
- Integration test: full MCP round-trip through the standalone MCP process using a real seeded `incident-commander` token against a read-only endpoint.

## Pointers

- `backend/app/models/service_account.py` — the SQLAlchemy models (Wave 1 PR #1, shipped)
- `backend/app/dependencies.py` — `get_current_principal` fanning in both auth paths
- `backend/app/core/scopes.py` — the fixed scope enum + `require_scope` dependency factory
- Related ADRs: [0006 — MCP server standalone process](0006-mcp-server-standalone-process.md), [0008 — Chaos gating](0008-chaos-gating.md)
