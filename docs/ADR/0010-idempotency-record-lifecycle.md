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

Lookups treat expired records as absent — the caller re-executes and stores fresh. Background cleanup runs hourly via `_idempotency_reaper_loop` in the worker process (v0.4.8) — `DELETE FROM idempotency_records WHERE expires_at IS NOT NULL AND expires_at < now()`. Interval matches the TTL cadence: a record expires at t+24h, gets reaped no later than t+25h. That bounded 1h window of "expired but still in the table" is invisible to callers because the lookup's own `expires_at < now()` check treats them as absent. *(2026-08-30: this last claim was wrong. The lookup treats an expired record as absent; `uq_idempotency_scope` does not, so `store()` collides with a row the caller was just told did not exist. See the addendum below.)*

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
- **Explicit rollback in the `except Exception` handler.** When something raises outside the per-item savepoints (or a tool that doesn't use them at all), `handle_tools_call` now `await ctx.db.rollback()`s before recording the error audit — so the outer cleanup doesn't commit half-executed writes behind the error response. The audit itself is savepoint-wrapped ([#6](../postmortems/0002-phantom-supervisor.md)) so a rollback-broken session can still log without propagating. *(2026-08-30: the last sentence was wrong and the rollback is gone — a closed context-managed transaction refuses every later statement, so the audit row was never written. Replaced by a savepoint around the handler; see the addendum below.)*
- **`await ctx.db.flush()` before response build.** Success-path only. Sends pending SQL to the DB so deferred errors (FK drift, constraint violations — the class that sank [PR #70](https://github.com/kudratsingh/incident-platform/pull/70)) surface here as exceptions rather than silently at the outer commit. A flush failure lands in the `except Exception` above and the whole tx rolls back. *(2026-08-30: it did not — the flush sat outside every `except`. The flush now runs inside the handler's savepoint, where a failure genuinely does reach the crash path.)*

Together these give: **a success response means the DB has the pending writes, and the writes will commit as a unit; an error response means nothing committed for that call.** The per-item semantics for replay tools are additive: partial success is a first-class outcome, distinguishable from partial failure. *(2026-08-30: true as of the addendum below, and not before it — the post-execution block was unreachable by any handler.)*

Contract test: `test_replay_dlq_messages_mid_loop_crash_isolates_via_savepoint` in `tests/api/test_mcp_wave3_tier1_actions.py` injects a `RuntimeError` on the 2nd of 3 jobs and asserts `replayed=2 failed=1` + the second job's status unchanged.

### Reservations

Nothing outstanding at v0.4.6. *(2026-08-30: superseded — see "What this does not do" in the addendum below.)*

---

## Addendum — 2026-08-30 (WO-R2-06) — the expiry window is visible to callers, and the envelope meant to contain it did not exist

*The decision above is unchanged: 24h TTL, expired-means-absent on lookup, hourly reaper. What follows corrects three claims about the code around it, all of which described behaviour the code did not have.*

### The expired-record window is not invisible to callers

The Decision says the 1h "expired but still in the table" window is invisible because the lookup treats such records as absent. The lookup does. The unique index does not. An expired record still occupies `(tenant_id, principal_id, idempotency_key)`, so:

1. `lookup()` reads the expired record, applies `expires_at < now()`, returns `None` — the caller is told the key is free.
2. The tool executes. The Tier-1 effect happens for real.
3. `store()` INSERTs and hits `uq_idempotency_scope`, which the expired row still holds.

The same collision arrives without any expiry at all, from two concurrent calls carrying one key: both lookups miss, both execute, the second INSERT loses the race. Either way the caller was on the far side of an action that had already run.

### The `except Exception` rollback closed the transaction it was about to write into

The Commit-before-response section above specifies `await ctx.db.rollback()` in the crash path, "so the outer cleanup doesn't commit half-executed writes behind the error response", and notes the audit that follows is savepoint-wrapped so "a rollback-broken session can still log". It cannot. `get_db()` opens the request transaction as a context manager (`async with session.begin():`), and SQLAlchemy refuses every later statement on a session whose context-managed transaction was closed underneath it:

> `InvalidRequestError: Can't operate on closed transaction inside context manager. Please complete the context manager before emitting further commands.`

`record_tool_invocation` is three lines further down and swallows its own failures by contract, so the error went to the log and the row went nowhere: **every crashed MCP tool call was deterministically missing from `agent.tool_invoked`** — the table `evals/guards.py::assert_no_tier1_successes` grades the agent's safety on. A stage that crashed mid-effect read as a stage that never acted.

### The flush was not inside the `try`

Same section: "A flush failure lands in the `except Exception` above and the whole tx rolls back." The `except Exception` block ended at the `return` above it; the success audit, `store()` and `flush()` all sat past it. Nothing there could land in a handler, so an `IntegrityError` from step 3 unwound out of `handle_tools_call`, out of `dispatch`, and out of the endpoint. `get_db` saw the exception on the way through and rolled the request transaction back — discarding the success audit row for the action that *had* executed — and Starlette answered with plain-text `Internal Server Error`. To an MCP client that is not a response at all, so it retried, and the Tier-1 effect ran a second time with no audit row for either attempt.

### Enforcement (amended)

One `try/except` now spans the whole of `handle_tools_call` and every exit from it is a JSON-RPC envelope. Inside it, three nested transactions with three different jobs:

| Region | Boundary | On failure |
| --- | --- | --- |
| Tool handler + its flush | `async with ctx.db.begin_nested()` | The tool's own writes roll back; the request transaction stays open and committable, so the error audit row can still be written. Replaces the transaction-closing `rollback()`. |
| Audit write | `begin_nested()` inside `record_tool_invocation` (unchanged) | Only the audit row is lost, and to the log. |
| `store()` | `async with ctx.db.begin_nested()` | Only the cache write is lost. The audit row was written before this savepoint opened and is untouched by its rollback. |

The ordering is the point: **the audit row outranks the cache write.** A store that fails must never take down the record of an action that already happened.

Two side effects of the first row worth naming. It applies to `AppError` too, not just the crash path the v0.4.6 fix covered: a tool that refuses partway through no longer leaves its half-written state to be committed behind a `MCP_TOOL_ERROR` response. And every tool call now costs a `SAVEPOINT`/`RELEASE` pair, read-only ones included — two round trips on a surface whose calls already do several, and the price of the transaction being recoverable at all.

A collision is resolved by reading the key back, not by failing:

- **A live record exists** — a concurrent call won the race. Its recorded response is returned. One key, one answer, whichever caller asks.
- **A live record exists under different arguments** — the same `IdempotencyKeyReusedError` (409-shaped `MCP_TOOL_ERROR`) the pre-execution lookup would have raised. A retry now meets it at the lookup and does not re-execute.
- **Only an expired record exists** — there is no live outcome to defer to, so the caller gets its own result, uncached, plus a warning log. Returning an error here would be a lie about an action that succeeded, and would invite exactly the retry this order exists to prevent.

The MCP app also registers a catch-all `Exception` handler (`app/mcp/standalone.py`), so anything escaping the dispatch layer entirely — a failing commit during dependency teardown, a middleware bug — still leaves the process as an envelope rather than a plain-text 500.

### What this does not do

- **`store()` does not reclaim an expired row.** The colliding caller is answered correctly and audited, but its response is not cached, so a further retry of that key re-executes. Closing that needs `store()` to overwrite a record its own `lookup()` already declared absent — a change to the service's semantics, tracked as the follow-up order that depends on this one. *(2026-08-30: done — WO-R2-27, addendum below.)*
- **It does not deduplicate the execution itself.** Two concurrent calls on one key still both run the tool; only the *answer* is deduplicated. Preventing the double execution needs the key reserved before the handler runs, not after. *(2026-08-30: done — WO-R2-27, addendum below.)*
- **The 24h TTL and the reaper are untouched.** Only the claim about what the leftover row does to a caller changes.

Verification: `backend/tests/api/test_mcp_transaction_envelope.py` (crash path leaves an `outcome=error` row while the tool's own writes roll back; collision returns an envelope; nothing escapes as a bare 500) and `backend/tests/integration/test_mcp_envelope_postgres.py` (the same two facts against a real `uq_idempotency_scope` violation, which is the only place the savepoint's necessity is observable — Postgres aborts the whole transaction on a constraint violation, SQLite does not).

---

## Addendum — 2026-08-30 (WO-R2-27) — the key is claimed before the action runs

*The decision above is unchanged: 24h TTL, 409 on same-key-different-args, hourly reaper. What changes is **when** the key is taken. The two follow-ups the WO-R2-06 addendum left open are closed here, and they turn out to be one change.*

### Recording the key after the fact could not be made safe

WO-R2-06 made the collision survivable: savepoint the `store()`, catch the `IntegrityError`, read back whoever won, answer with their response. That is a repair, and it runs *after* the action has already happened. Two consequences it could not reach, both listed as its own reservations:

- Two concurrent calls on one key both executed. Only the answer was deduplicated, not the effect — and for Tier-1 actions the effect is the whole point.
- A caller that collided with an expired-but-unreaped row got its answer uncached, so the next retry executed a third time.

Both come from the same ordering. The lookup and the claiming INSERT sat in one READ COMMITTED transaction with the entire action between them, and nothing in that gap reserved anything, so "the key is free" was a fact about the past by the time it was acted on.

### The claim

`IdempotencyService.acquire()` now takes the key **before** the handler runs, in a single statement:

```
INSERT INTO idempotency_records (..., response_json, ...)
VALUES (..., NULL, ...)
ON CONFLICT (tenant_id, principal_id, idempotency_key) DO NOTHING
RETURNING id
```

Winning that insert is what authorises execution. There is no window between deciding the key is free and holding it, because they are the same operation. `acquire` returns one of three things, and only the first executes anything:

| Outcome | Meaning | Caller |
| --- | --- | --- |
| `Claim` | The insert returned an id — we own the key | Runs the tool, then `complete(claim, response)` |
| `Replay` | Lost, and the holder has a response | Returns the holder's response verbatim |
| raise | Lost, and the holder disagrees or is unfinished | `IdempotencyKeyReusedError` (409) or `IdempotencyKeyInFlightError` |

`response_json` became nullable to make the reserved-but-unanswered state representable (migration `c9e41a7b62d5`). NULL means "claimed, not yet answered" — a state, not a missing value.

**On Postgres the loser blocks rather than failing.** `ON CONFLICT DO NOTHING` against an *uncommitted* conflicting row waits on the holder's transaction instead of returning immediately. The second caller therefore parks until the first commits its response, then reads it and replays. The blocking is the serialisation; it is not a cost to design around. This is also why the concurrency test is Postgres-only twice over: SQLite's in-memory engine puts every session on one connection, so two concurrent requests cannot exist there to begin with.

### Completion cannot lose a race, and failure must release

`complete()` is an UPDATE by primary key on a row this call inserted, so the duplicate-key error that used to land at store time has nowhere to come from. It stays savepoint-wrapped for the reason WO-R2-06 established — the audit row for an action that really executed outranks the cache write — but the failure it guards against is now hypothetical rather than routine.

The claim introduces one obligation the old shape did not have. The envelope deliberately commits the request transaction even when the tool failed, so that the `outcome=error` audit row survives; a reservation row would commit right alongside it and hold the key for its full 24 hours. So **every path that does not complete releases**: the handler sets a flag at the end of the successful execution block and a `finally` deletes the claim otherwise. A failed call must leave the key re-usable, or one crash would turn into a permanently dead key.

A release that itself cannot be written is the one remaining way to strand a key. It is logged at ERROR, and a retry meets `idempotency_key_in_flight` — retryable, and distinct from `idempotency_key_reused`, which is the caller's mistake and never clears.

### `lookup` evicts instead of reading past

The expired-record half needed no separate mechanism. `lookup()` now deletes the expired row it finds rather than returning `None` and leaving it on the unique index, and `acquire()` evicts an expired holder and retries the claim exactly once. A second lost attempt means a live caller took the key in between, so we defer to them rather than spin.

This retires the WO-R2-06 addendum's third collision bullet ("only an expired record exists — the caller gets its own result, uncached"). There is no such case now: the expired row is gone before the action runs, and the claim that replaces it is completed normally.

### What this does not do

- **The 24h TTL and the reaper are untouched.** The reaper now also collects any stranded claim, since an unfinished claim carries the same `expires_at` as a completed record.
- **It does not make tool handlers themselves idempotent.** The key is deduplicated; a handler with an external side effect that is retried under a *different* key is still that handler's problem.

## Verification

- `test_idempotency_service.py` — canonical-JSON invariance (same dict, different insertion orders → same hash), expiry (records past `expires_at` return `None`), same-key-different-args (raises 409), cross-tool collision (raises 409).
- `test_mcp_wave3_tier1_actions.py::test_restart_consumer_group_replay_returns_cached_response` — end-to-end replay through a Tier-1 action; second call returns the first response, doesn't re-execute.
- `test_mcp_transaction_envelope.py` — the envelope contract (WO-R2-06): crash path returns an envelope *and* persists an `outcome=error` row, the crashed tool's own writes roll back, an expired key re-executes and still returns an envelope, a duplicate call returns the recorded outcome.
- `tests/integration/test_mcp_envelope_postgres.py` — the same against a real `uq_idempotency_scope` violation. Postgres aborts the transaction on a constraint violation and its COMMIT then degrades to a ROLLBACK, so this is the only tier where losing the savepoint is observable; SQLite passes either way. Also the WO-R2-27 concurrency proof: two genuinely concurrent `tools/call` requests on one key produce one execution and one replay, and a failed call leaves no claim behind.
- `test_mcp_wave3_tier1_actions.py` — that a replay is a replay: the side-effect count on the Redis stub distinguishes a cached answer from a re-execution, which the payload alone cannot (both produce `deleted=False`).
- **Deferred (item #26)**: cross-repo hash contract test pinning a fixed argument dict on both platform and commander sides.

## Pointers

- `backend/app/services/idempotency.py` — the service; `_hash_arguments` is the contract function.
- `backend/app/mcp/handlers.py` — `_IDEMPOTENCY_TTL = timedelta(hours=24)`, passed into `acquire(...)` before the handler runs and into `complete(...)` after it; `_complete_claim` and `_release_claim` own the savepoints.
- `backend/app/mcp/standalone.py` — the catch-all `Exception` handler that keeps every failure a JSON-RPC envelope.
- `backend/app/repositories/idempotency.py` — `insert_claim` (the `ON CONFLICT DO NOTHING` reservation), `complete_claim`, `delete_by_id`, `delete_expired` (the reaper).
- `backend/alembic/versions/c9e41a7b62d5_idempotency_claim.py` — makes `response_json` nullable so the reserved state is representable.
- `backend/tests/unit/test_idempotency_service.py` — hash + expiry + collision unit tests.
- Related: [ADR 0007 — Machine-principal scope model](0007-machine-principal-scope-model.md) (Tier-1 vs Tier-2 action tiering).
- Postmortem context: [Postmortem 0002 — Phantom supervisor](../postmortems/0002-phantom-supervisor.md) (idempotency stress-tested during the seven-run debug loop).
