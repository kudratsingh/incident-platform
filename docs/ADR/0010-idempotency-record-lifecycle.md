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

Lookups treat expired records as absent — the caller re-executes and stores fresh. Background cleanup runs hourly via `_idempotency_reaper_loop` in the worker process (v0.4.8) — `DELETE FROM idempotency_records WHERE expires_at IS NOT NULL AND expires_at < now()`. Interval matches the TTL cadence: a record expires at t+24h, gets reaped no later than t+25h. That bounded 1h window of "expired but still in the table" is invisible to callers because the lookup's own `expires_at < now()` check treats them as absent.

### 2. Arguments-hash contract (cross-repo)

The `_hash_arguments` function (`backend/app/services/idempotency.py`) is a **published contract** — its output feeds the platform's own `IdempotencyRecord.arguments_hash` column and (since v0.4.6 finalization) is also pinned from the commander side. The commander uses matching normalization to build a locally-computed reference hash inside its cross-repo contract-snapshot test, so any drift on either side fails CI at the version-sync PR that caused it — the same job that already catches [ADR 0009](0009-consumer-lifecycle-and-supervision.md)-shaped tool schema drift.

The rest of this section is the **exact normalization spec**, derived from code, so both repos pin the same reference.

#### What is hashed

`call_params.arguments` — the raw JSON-object dict as it arrives on the JSON-RPC wire, before any Pydantic parsing. The platform does *not* run the tool's input model over the dict before hashing it (see `backend/app/mcp/handlers.py::handle_tools_call` — the call to `idempotency_service.lookup(...)` / `.store(...)` passes `call_params.arguments`, not `parsed_input`).

Consequence: the bytes on the wire *are* the hash input. If the commander's serialization changes what those bytes look like — even for a semantically-equivalent request — the hash changes and an in-flight retry with the same `Idempotency-Key` starts 409ing.

In particular, the commander produces the wire dict via `model_validate(...).model_dump(mode="json")`, which by default materializes Pydantic defaults into explicit fields. Changing that to `exclude_unset=True` (or any equivalent) is a **breaking change to in-flight idempotency records** on the same day it's deployed. Same-day changes must go through a coordinated version-sync (see "Coordination rule" below).

#### Normalization

```python
body = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode()
sha256(body).hexdigest()
```

Point-by-point, so the commander's local reference computation can match byte-for-byte:

| Aspect | Behaviour | Notes |
|---|---|---|
| **Algorithm** | SHA-256, lowercase hex digest | `hashlib.sha256(...).hexdigest()` |
| **Serializer** | Python stdlib `json.dumps` | Not `orjson`, not `simplejson` — the commander must not swap in a different encoder without a version-sync. |
| **Object key order** | Sorted (recursive) | `sort_keys=True`. Insertion order at the caller is not significant, at any nesting depth. |
| **Whitespace** | None between tokens | `separators=(",", ":")`. `{"a":1,"b":2}`, not `{"a": 1, "b": 2}`. |
| **`default=str`** | Non-JSON-native values stringify via `str()` | Not exercised by the wire path — the wire dict is already JSON — but a future path that hashes Python-native dicts (e.g. UUIDs, datetimes) would rely on it. Change to `default=` is by definition breaking. |
| **`idempotency_key` field** | **Included in the hash** | The platform hashes the whole `arguments` dict; it does not strip `idempotency_key`. Doesn't affect same-key-different-args semantics (the key always matches when we reach the hash comparison), but the commander's local hash must include it. |
| **JSON array element order** | Significant | Arrays are ordered by JSON spec. `{"tags":["a","b"]}` and `{"tags":["b","a"]}` are different hashes. Intentional — lists aren't sets. |
| **Numeric type** | Significant | `1` and `1.0` are distinct at the Python and JSON levels and produce different bytes. A commander that switches a field's declared type between calls will 409. |
| **String encoding** | UTF-8 | `.encode()` default. Non-ASCII characters must round-trip cleanly on both sides. |
| **Null vs. absent** | Significant | `{"x": null}` and `{}` are different keys → different bytes → different hashes. Commanders using `exclude_none=True` produce a different hash than those emitting explicit `null`. |

#### Enforcement (post-v0.4.6)

- **Provider self-verification (platform)** — `backend/tests/unit/test_idempotency_service.py` locks the canonical-JSON invariance, expiry semantics, and same-key-different-args 409 shape. `backend/tests/api/test_mcp_wave3_tier1_actions.py` covers the end-to-end replay path. No new platform-side test is added for #26 — the provider already tests its own contract.
- **Consumer verification (commander)** — `contracts/platform-tools.snapshot.json` gets a perturbation matrix section: a fixed set of representative argument dicts (with variations for nested-key order, list-vs-null-vs-absent, default fields present/omitted) is pinned to their expected hashes, computed once against the digest-pinned platform image. That block sits in the same CI job that already verifies input/output schemas per PR #55, so a change to `_hash_arguments` on either side fails at the version-sync PR that introduced it.
- **What the commander does NOT do**: import the platform's `_hash_arguments` function to re-compute locally. That would (a) invert the ADR 0001 dependency arrow — the commander is an external client, and a provider importing its client's normalizer breaks the moment a second client exists; (b) verify whatever platform checkout is on disk, not the digest-pinned artifact live evals actually hit — the same failure class as [FIX_PLAN #25](../../CLAUDE.md); (c) freeze internal platform refactors by making the private helper de-facto public API.

#### Coordination rule

Any change to what feeds `_hash_arguments` (the caller's dict shape) OR how it hashes (sort behaviour, separators, `default=`, algorithm) is a **conscious spec change**, not a refactor. Coordinated cross-repo sequence:

1. Platform revises the normalization table above in this ADR and lands the code change in the same PR.
2. Platform release tags a new version, referencing the ADR change in the tag message.
3. Commander pin-bump PR regenerates the perturbation matrix against the new pinned image and updates its own snapshot in the same PR.

If the commander's matrix disagrees with this ADR at any pin bump, one of them is a bug — not a compatibility issue to work around. In particular, if the matrix ever reveals that platform-observed key order *is* significant (i.e. `sort_keys=True` isn't holding), treat that as a **conscious platform-side spec change** (formalize sorted-key canonical JSON here, add the fix, commander updates matrix next sync). Neither agent silently patches around a mismatch.

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

### Hash the parsed Pydantic model instead of the wire dict

Run `tool.input_model.model_validate(arguments)` first, then hash the parsed model's serialization. Two callers that omit an optional field vs. explicitly send its default would then hash the same way.

Rejected: it moves the source-of-truth from "what the caller sent" to "what the platform inferred". A commander PR that changes a field's default (or adds a new one) silently changes every in-flight hash on deployment, without any wire-level indication. Hashing the wire dict makes the contract precisely observable — the bytes on the wire *are* the hash input, and neither side can accidentally change it without a coordinated version-sync (see the coordination rule in section 2). Fixed the corresponding text in the earlier version of this ADR that mis-stated this as the current implementation.

### Cross-repo hash contract via shared normalizer package or provider-import

Two variants of the same idea, both rejected. Provider (platform) exports its `_hash_arguments` function; consumer (commander) imports and re-computes locally to compare.

Rejected because:
- **Inverts the dependency arrow.** [ADR 0001](0001-outbox-vs-cdc.md) and the agent-facing surface docs frame the commander as an external client. A provider that imports its own client's helpers breaks the moment a second client shows up — the provider now has to satisfy two clients' schemas.
- **Verifies the wrong artifact.** A test importing `app.services.idempotency` runs against whatever platform checkout is on disk, not the digest-pinned container the live evals actually hit. FIX_PLAN #25 already taught this lesson — the exact reason we're pinning by image digest is that in-repo checkout state and shipped-artifact state can diverge.
- **Freezes internal refactors.** `_hash_arguments` becomes de-facto public API and can't be renamed / restructured / inlined without a commander-visible change.

### Cross-repo shared contract package (third repo)

Publish an `incident-platform-contract` package with the normalization spec + reference implementation; both platform and commander pip-install it.

Rejected at current scale. Right answer at N-consumers × M-providers where a shared package amortises the versioning + release burden; overkill at 1×1 for the sake of one hash function. The lightweight part of this idea worth keeping — a **written spec that lives with the provider** — is what section 2 of this ADR now is.

## Consequences

### Positive

- **Stale responses can't outlive an incident.** 24h is a hard ceiling; a "cached success" older than that is treated as absent and the caller re-executes.
- **Cross-repo contract has a named shape.** `_hash_arguments` docstring points here; the commander implementation points here; a change to either side is visible as an ADR-touching diff.
- **Stripe-shape 409 semantics.** Callers who already implement idempotency against Stripe's model have the mental model needed to reason about this one.

### Negative

- **Hourly reaper interval is a compromise.** Every hour is short enough that the table stays bounded at ~24× the daily write rate but long enough that a burst of expired records isn't held for weeks. If write volume rises significantly, drop to 15min or add a partial index on `(expires_at) WHERE expires_at IS NOT NULL` for cheaper deletes.
- **24h is a global constant, not a per-tool policy.** If a legitimate need arises for a shorter or longer window for a specific tool, this ADR's decision has to be revisited. Cheap to revisit — the constant is one place — but the mental model shifts from "always 24h" to "per-tool", which is a review-time cost.
- **Cross-repo hash coordination lives in two places** (the spec here + the commander's perturbation matrix) and both must move together on any change to `_hash_arguments`. That coupling is intentional — the alternative is silent drift — but any future change to the normalization is a two-repo PR sequence, not a one-repo refactor.

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
