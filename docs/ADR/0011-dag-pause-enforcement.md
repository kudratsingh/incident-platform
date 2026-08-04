# ADR 0011 — DAG pause is enforced by the resolver, not just recorded

**Status:** Accepted (v0.4.9) · **Date:** 2026 Q3 · **Owner:** Platform

## Context

`pause_dag` (Wave 3, Tier 1) has shipped since v0.4.x with this contract in its own docstring:

> Mechanism: Redis key `dag:paused:<root_id>` with a TTL. The DependencyResolver consumer checks the key before promoting a `WAITING` child; if any ancestor (or the child itself) is paused, the child stays waiting.

The second sentence was not true. `DependencyResolver` never imported Redis and never probed the key. A grep across `backend/app/` found exactly three references to the pause key: the function that builds it, the tool that writes it, and a test asserting it had been written. Nothing read it.

So `pause_dag` returned `accepted: true`, wrote a key with a TTL, emitted an audit row — and children kept promoting on the next `job.completed`. Every observable signal said the pause worked. The only thing that didn't happen was the pause.

This surfaced through the `runaway_saga` eval scenario, which failed twice. The initial read was that the agent couldn't *see* the pause take effect, and the proposed fix was to expose `paused` on `get_dag_state`. That would have been worse than the bug: the scenario would have gone green off a flag that describes nothing, and a Tier-1 action that does nothing would have looked verified.

## Decision

### 1. The resolver enforces the flag

`DependencyResolver` now takes a Redis client and, before promoting a `WAITING` child, walks the child plus its transitive parents and refuses to promote if any of them carries `dag:paused:*`. Ancestor-inclusive because `pause_dag(root)` is documented to pause a chain, not one hop.

The walk is bounded (`_MAX_ANCESTOR_NODES = 64`) and resolves to a single `MGET` regardless of depth — DB reads scale with depth, the Redis lookup doesn't.

### 2. Pause is temporary, which requires a resume path

Enforcement alone would have replaced a no-op with a worse failure. The resolver only reacts to `job.completed`; a child held during a pause has already had its promotion event consumed, so when the TTL expired the child would sit in `WAITING` forever. A "10 minute pause" would have meant "permanently stalled DAG".

`_resume_unblocked_waiting_loop` (10s) promotes `WAITING` jobs whose dependencies are all met and whose ancestry is no longer paused. This is what makes the TTL mean what the tool says it means, and it doubles as a backstop for any child whose promotion event was lost for unrelated reasons.

It is deliberately cross-tenant: it's a platform scheduler, not a request path, so it does not go through the tenant-scoped `JobRepository.list_jobs`.

### 3. Pause state is observable

`get_dag_state` returns:

- `paused` — the job's *own* flag, the direct result of `pause_dag` on it
- `paused_expires_in_seconds` — countdown to automatic resume
- `paused_by` — the job whose flag is actually holding this one back: itself, or an ancestor

`paused` and `paused_by` are separate because they answer different questions. A child paused via its root reads `paused=false, paused_by=<root>` — false because it has no flag of its own, and `paused_by` because that's where a resume has to be aimed. Collapsing them into one boolean would tell the agent it's paused without telling it what to un-pause.

### 4. Redis failures fail open

A pause lookup that raises promotes as if unpaused, logging the fall-through. Consistent with [ADR 0005](0005-llm-features-fail-open.md) and the platform's standing treatment of Redis as a performance/UX dependency and never a correctness one.

The alternative — fail closed — means a Redis blip silently freezes every DAG in the system, with the symptom (jobs stuck in `WAITING`) appearing far from the cause. Given pause is an operator convenience and the resume sweep re-evaluates continuously, a missed pause is recoverable in seconds; a system-wide freeze is not.

## Consequences

**Contract change.** `get_dag_state` gains three output fields. Additive, so existing consumers are unaffected, but the commander's tool snapshot churns and must be re-synced.

**Behavioural change.** `pause_dag` now does something. Any caller that relied on it being inert — including eval scenarios written against the old behaviour — will see children stop promoting. That is the point, but it is a real change to a shipped tool, hence this ADR rather than a bugfix note.

**New always-on loop.** One additional DB query every 10s per worker (bounded at 200 rows). Negligible at current scale; if `WAITING` volume ever grows, the natural fix is an index-backed query on `(status, created_at)` and a smaller limit, not a longer interval — the interval is what bounds post-pause resume latency.

**What this does not do.** There is still no `resume_dag` tool; the only way to lift a pause early is to wait out the TTL. That's now a real gap rather than a cosmetic one, since the pause actually holds. Sized as a follow-up.

## Alternatives considered

**Expose `paused` and stop there** — the original brief. Rejected: it manufactures a green verification for an action that does nothing. The scenario would pass, the platform would still be broken, and the eval would have been actively teaching the agent that a no-op is a success.

**Enforce in `JobService` at submission time instead of the resolver** — wrong layer. Promotion is the event being paused, and it happens in the resolver; putting the check at submission would miss every child promoted by the dependency path.

**Have `pause_dag` set job status to a new `PAUSED` state** — considered and rejected. It makes the pause durable (survives Redis loss) but requires a status migration, a new terminal-vs-transient classification across every status consumer, and an unwind on expiry. The Redis flag with a resume sweep gets the same observable behaviour without touching the status enum.
