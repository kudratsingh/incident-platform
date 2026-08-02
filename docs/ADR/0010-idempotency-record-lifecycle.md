# ADR 0010 — Idempotency record lifecycle

**Status:** Accepted (v0.4.5) · **Date:** 2026 Q3 · **Owner:** Platform

## Context

Tier-1 MCP actions (`restart_consumer_group`, `replay_dlq_messages`, `pause_dag`, `invalidate_cache_key`) are effect-bearing and must be safe to retry. The commander threads an `Idempotency-Key` header (per the [MCP spec convention](https://spec.modelcontextprotocol.io)) on every action call so a network hiccup between "server executed the effect" and "client received the response" can be resolved by resubmission without doubling the effect.

The platform's `IdempotencyService` (`backend/app/services/idempotency.py`) implements this with a Postgres record keyed by `(tenant_id, principal_id, idempotency_key)`. Store on first execution; on a matching-key lookup, return the cached response; on a same-key-different-args lookup, raise `IdempotencyKeyReusedError` (409).

Two problems the prior implementation left unspecified:

1. **How long is a record valid?** Pre-v0.4.5, records had no expiry. A response was pinned forever. A repeat operator call weeks later would replay a stale result — worst case, an operator who forgot they'd already invoked a restore reads a cached success from a prior incident and treats it as fresh, ignoring the current one.
2. **What exactly goes into the arguments hash?** The commander binds requests to their `Idempotency-Key` via a hash-on-hash contract: it computes an expected hash locally and refuses to reuse a key that doesn't match. If the platform's `_hash_arguments` shape drifts (Pydantic defaults filled vs. not, ordering, etc.), the commander's retry dedup silently breaks and Tier-1 actions can double-execute.

## Decision

### 1. 24-hour TTL

Every idempotency record carries `expires_at = now() + 24h`. Rationale for the number:

- **Upper bound: longest plausible incident duration.** A retry that lands >24h after the original call is not disambiguating a transient network fault — it's a new operational intent that happens to reuse a key. Treating it as a cache hit is a bug, not a feature.
- **Lower bound: multi-hop retry windows.** The commander's transport layer retries with exponential backoff up to ~5 minutes; the outer agent loop can retry a plan across ~30 minutes; a human operator resurrecting yesterday's session is at most a working day away. 24h absorbs all of these.
- **Chosen number** falls comfortably above the operational retry window and below "same key means something different now" territory.

Lookups treat expired records as absent — the caller re-executes and stores fresh. A background reaper is unnecessary at current volumes (10²–10³ Tier-1 calls/day per tenant); if the table grows, a partial index on `expires_at < now()` and a nightly `DELETE` job handle it. Deferred until the write rate justifies the operational surface.

### 2. Arguments-hash contract (cross-repo)

The `_hash_arguments` function is a **published contract**, consumed by the commander. The shape is:

- **Input**: the exact `dict[str, Any]` passed as the tool's `arguments` — the raw wire payload the JSON-RPC handler received, with any Pydantic-filled defaults already materialized. The platform hashes what it *actually executes*, not what the wire carried before defaulting.
- **Hash**: SHA-256 of the canonical-JSON serialization: `json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)`. Sorted keys, tight separators, `default=str` for datetime/UUID/other non-JSON-native types.
- **Contract stability**: any change to this shape (different serializer, different `default=`, different key handling) is a **breaking change to the commander**. Retry dedup silently breaks — a network-retry that should hit the cache instead executes the effect a second time.

Enforcement: a cross-repo contract test (backlog item #26) pins the hash of a fixed argument dict on both sides. Any drift fails CI in the repo that changed. Until that test lands, changes to `_hash_arguments` require a manual note on the PR and a coordinated commander-side change.

### 3. Same-key-different-args = 409

`lookup` raises `IdempotencyKeyReusedError` (HTTP 409, `error_code: idempotency_key_reused`) when the key matches but the arguments hash doesn't. This mirrors Stripe's shape — the client either recomputes with a fresh key or resubmits with identical arguments. No fall-through to "execute anyway", no silent overwrite of the cached response.

Cross-tool key collision (`(tenant_id, principal_id, idempotency_key)` matches but `tool_name` differs) is the same 409, same message class. Reusing a key across tools is always a caller bug.

## Alternatives considered

### No TTL — records live forever

The pre-v0.4.5 shape. Rejected: silent replay of week-old responses is a correctness problem worse than the alternative of re-executing an idempotent action.

### Configurable TTL per tool

Let each tool declare its own idempotency window (`restart_consumer_group` might want 1h, `replay_dlq_messages` might want a week).

Rejected for v0.4.5: adds one lever per tool and forces the commander to know the per-tool retention window before it can reason about a stale cache hit. 24h is a defensible ceiling for every current Tier-1 action; if a future tool genuinely needs a different window, add the lever then.

### Client-provided TTL header

`Idempotency-Retention: 72h` on the request.

Rejected: the platform is the system of record for its own idempotency behavior. A client that requested 72h retention would still expect Stripe-shape 409s on same-key-different-args at t=72h, which is the exact incident this ADR exists to prevent. The platform sets the policy.

### Hash the wire bytes instead of the parsed dict

Skip Pydantic-materialization and hash the raw JSON body.

Rejected: two commanders that omit an optional field vs. explicitly send its default would hash differently and be treated as different arguments, even though the platform executes the same effect. Hashing the parsed dict means "what the platform will actually do" is what determines idempotency.

## Consequences

### Positive

- **Stale responses can't outlive an incident.** 24h is a hard ceiling; a "cached success" older than that is treated as absent and the caller re-executes.
- **Cross-repo contract has a named shape.** `_hash_arguments` docstring points here; the commander implementation points here; a change to either side is visible as an ADR-touching diff.
- **Stripe-shape 409 semantics.** Callers who already implement idempotency against Stripe's model have the mental model needed to reason about this one.

### Negative

- **No reaper means expired records accumulate.** Rows accumulate at ~10²–10³/tenant/day and are read-cheap (indexed lookup ignores expired rows). A partial-index + nightly cleanup follow-up is filed when write rate justifies it. Not urgent.
- **24h is a global constant, not a per-tool policy.** If a legitimate need arises for a shorter or longer window for a specific tool, this ADR's decision has to be revisited. Cheap to revisit — the constant is one place — but the mental model shifts from "always 24h" to "per-tool", which is a review-time cost.
- **The contract-test gap is real until #26 lands.** Right now, `_hash_arguments` drift caught by manual review only. Item #26 in FIX_PLAN_v2 tracks the cross-repo test that makes drift a CI failure.

### Commit-before-response (resolved v0.4.6)

Two-part fix. The pre-v0.4.6 shape had `IdempotencyService.store()` executing in the same request transaction as the tool's writes, with the transaction committing at request exit via the `get_db()` dependency. Two failure modes:

**(a) Mid-loop non-AppError in a replay tool.** `replay_dlq_messages` and its siblings iterate over DLQ jobs, calling `service.replay_job` per item. The `try/except` only caught `AppError`; a `RuntimeError` (SQLAlchemy constraint violation, unexpected bug, dependency error) raised on job N propagated up to `handle_tools_call`, which caught it as `except Exception`, recorded an error audit, and returned "internal tool error" — while the outer `get_db()` cleanup then **committed** the writes staged for jobs 1..N-1. Caller saw failure; DB kept the partial effect.

**(b) Deferred SQL errors surfaced only at outer commit.** A constraint violation ORM-detected only at flush/commit time would have already left `handle_tools_call` past the response-build point when it raised — mid-response, hard to correlate.

Fix:

- **SAVEPOINT per item.** Each per-item call in `replay_dlq_messages`, `replay_dlq_by_ids`, and `replay_dlq_by_category` is wrapped in `async with ctx.db.begin_nested():`. Both `AppError` and non-`AppError` exceptions per item are caught, counted as `failed`, and logged. The savepoint rolls back only that item; the batch continues. Success shape (`replayed=N failed=M`) accurately reflects reality.
- **Explicit rollback in the `except Exception` handler.** When something raises outside the per-item savepoints (or a tool that doesn't use them at all), `handle_tools_call` now `await ctx.db.rollback()`s before recording the error audit — so the outer cleanup doesn't commit half-executed writes behind the error response. The audit itself is savepoint-wrapped ([#6](../postmortems/0002-phantom-supervisor.md)) so a rollback-broken session can still log without propagating.
- **`await ctx.db.flush()` before response build.** Success-path only. Sends pending SQL to the DB so deferred errors (FK drift, constraint violations — the class that sank [PR #70](https://github.com/kudratsingh/incident-platform/pull/70)) surface here as exceptions rather than silently at the outer commit. A flush failure lands in the `except Exception` above and the whole tx rolls back.

Together these give: **a success response means the DB has the pending writes, and the writes will commit as a unit; an error response means nothing committed for that call.** The per-item semantics for replay tools are additive: partial success is a first-class outcome, distinguishable from partial failure.

Contract test: `test_replay_dlq_messages_mid_loop_crash_isolates_via_savepoint` in `tests/api/test_mcp_wave3_tier1_actions.py` injects a `RuntimeError` on the 2nd of 3 jobs and asserts `replayed=2 failed=1` + the second job's status unchanged.

### Reservations

Nothing outstanding at v0.4.6.

## Verification

- `test_idempotency_service.py` — canonical-JSON invariance (same dict, different insertion orders → same hash), expiry (records past `expires_at` return `None`), same-key-different-args (raises 409), cross-tool collision (raises 409).
- `test_mcp_wave3_tier1_actions.py::test_restart_consumer_group_replay_returns_cached_response` — end-to-end replay through a Tier-1 action; second call returns the first response, doesn't re-execute.
- **Deferred (item #26)**: cross-repo hash contract test pinning a fixed argument dict on both platform and commander sides.

## Pointers

- `backend/app/services/idempotency.py` — the service; `_hash_arguments` is the contract function.
- `backend/app/mcp/handlers.py` — `_IDEMPOTENCY_TTL = timedelta(hours=24)`; passed into `store(...)` at line ~385.
- `backend/app/repositories/idempotency.py` — `IdempotencyRecord` table (`expires_at` column, indexed).
- `backend/tests/unit/test_idempotency_service.py` — hash + expiry + collision unit tests.
- Related: [ADR 0007 — Machine-principal scope model](0007-machine-principal-scope-model.md) (Tier-1 vs Tier-2 action tiering).
- Postmortem context: [Postmortem 0002 — Phantom supervisor](../postmortems/0002-phantom-supervisor.md) (idempotency stress-tested during the seven-run debug loop).
