# ADR 0011 — DAG pause is enforced by the resolver, not just recorded

**Status:** Accepted (v0.4.9), amended by [ADR 0022](0022-promotable-only-resume-sweep-and-dependency-cascade.md) ·
**Date:** 2026 Q3 · **Owner:** Platform

> **Amendment (ADR 0022, WO-R2-09).** §2's sweep selected `WHERE status = 'waiting'
> LIMIT 200` and tested eligibility in Python afterwards, so `WAITING` children of a
> `DEAD_LETTER`/`CANCELLED` parent — which nothing could ever promote and nothing
> removed — filled every page and starved the sweep platform-wide. The eligibility
> test now lives in SQL (`NOT EXISTS` an unmet parent), with `ORDER BY created_at, id`
> and a rotating cursor behind it, and a terminal non-`COMPLETED` parent now cascades
> `CANCELLED` to its non-saga descendants so the blocked set stops growing. The
> "New always-on loop" consequence below names the wrong remedy: a smaller limit and a
> `(status, created_at)` index would not have helped, because the blocked set grows
> without bound.

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

> **Superseded by [ADR 0022](0022-promotable-only-resume-sweep-and-dependency-cascade.md).** This prescription was wrong. The pressure did not come from `WAITING` *volume* but from `WAITING` rows that could never be promoted, so a smaller limit would have made it strictly worse and no index would have helped. The limit now bounds *promotable* rows, which is what makes it safe at any volume.

**What this does not do.** There is still no `resume_dag` tool; the only way to lift a pause early is to wait out the TTL. That's now a real gap rather than a cosmetic one, since the pause actually holds. Sized as a follow-up.

## Alternatives considered

**Expose `paused` and stop there** — the original brief. Rejected: it manufactures a green verification for an action that does nothing. The scenario would pass, the platform would still be broken, and the eval would have been actively teaching the agent that a no-op is a success.

**Enforce in `JobService` at submission time instead of the resolver** — wrong layer. Promotion is the event being paused, and it happens in the resolver; putting the check at submission would miss every child promoted by the dependency path.

**Have `pause_dag` set job status to a new `PAUSED` state** — considered and rejected. It makes the pause durable (survives Redis loss) but requires a status migration, a new terminal-vs-transient classification across every status consumer, and an unwind on expiry. The Redis flag with a resume sweep gets the same observable behaviour without touching the status enum.

## Amendment — 2026-08-09 (WO-P4-06)

*The accepted decision above stands unchanged; this note extends its enforcement surface from the two points it named to every path that dispatches work.*

### The decision scoped enforcement to promotion, and promotion is not the only dispatch

§1 put the probe in `DependencyResolver` and §2 added `_resume_unblocked_waiting_loop`; those were the two places a `WAITING` child became `PENDING`. Four other paths turn a job into a running job without ever passing through either, and all four ignored the flag (finding E1-08):

1. **The retry cycle.** `FAILED -> PENDING -> jobs:delayed -> _promote_delayed_once` re-published with no pause probe, so a failing step inside a paused chain kept re-executing on every backoff while `get_dag_state` reported the DAG paused.
2. **`JobService.replay_job`.** The single choke point for `POST /admin/jobs/{id}/replay`, all three MCP replay tools, and the scheduled DLQ-replay loop — every one of them fired into paused DAGs.
3. **`JobService.create_job`.** A new job whose declared parents are all `COMPLETED` was created `PENDING` and published immediately, even when those parents sit in a paused chain.
4. **Execution start.** `_run_job` never re-checked the pause before claiming `RUNNING`, so any `job.submitted` already in Kafka when the pause landed still executed — which made every promotion-time probe advisory rather than binding.

A retry, a replay, and a newly created job are all **new dispatches**. They are not the "work in flight" that `pause_dag` deliberately does not recall, and treating them as such was the gap between what the tool reported and what the platform did.

### Enforcement points (amended)

`find_blocking_pause` is now probed at six points. The first two are the original decision; the rest are this amendment.

| # | Probe site | On a blocking pause |
|---|---|---|
| 1 | `DependencyResolver` (promotion on `job.completed`) | child stays `WAITING` |
| 2 | `_resume_unblocked_waiting_loop` (10s resume sweep) | child stays `WAITING`, re-evaluated on a later pass — the keyset cursor from [ADR 0022](0022-promotable-only-resume-sweep-and-dependency-cascade.md) §2 means "next" only when the eligible set fits in one page |
| 3 | `_promote_delayed_once` (delayed-retry promotion) | no outbox row; job re-pushed onto `jobs:delayed` with `_PAUSE_RECHECK_SECONDS` (10s) |
| 4 | `JobService.replay_job` (interactive replays) | `JobError` before any mutation — no status change, no audit row, no outbox row |
| 5 | `_promote_dlq_replay_loop` (scheduled DLQ replays) | re-scheduled with `_PAUSED_REPLAY_DEFER_SECONDS` (30s), *not* refused |
| 6 | `_run_job` step 1 (pre-claim re-check) | job left `PENDING`, pushed onto `jobs:delayed` for a re-check; nothing is claimed |

Plus one hold that is not a probe-and-defer: `JobService.create_job` creates the job `WAITING` instead of `PENDING` when any declared parent's chain is paused. That needs no new resume machinery — a `WAITING` job with all dependencies met is exactly what §2's sweep promotes once the pause lifts.

**Refuse vs defer is deliberate.** Interactive replays refuse (§4): a human or an agent is holding the response and can retry after the pause, and the refusal is per-item, so the MCP replay tools' savepoint counts the id as failed with the batch shape unchanged. Scheduled replays defer (§5): `_promote_dlq_replay_loop` deliberately does not re-enqueue on failure — the operator sees the miss in the audit trail — so a refusal there would silently discard a `wait_and_replay` remediation instead of holding it.

**Held, never dropped.** Every deferral re-pushes onto the set it was popped from. `pop_ready_delayed` is destructive (the Lua script `ZREM`s the batch), so a paused job that took the "not found" `continue` path would lose its retry permanently.

### What this does not do (amended)

The original list stands: there is still no `resume_dag` tool, and the only way to lift a pause early is to wait out the TTL. Three additions:

- **Work already `RUNNING` is still not recalled.** `pause_dag`'s docstring carve-out is unchanged and remains accurate: a job that won its `claim_for_running` before the flag landed runs to completion, retries and children excepted. The pre-claim re-check (§6) moves the boundary to the claim, not to the processor — it does not cancel work, it declines to start it.
- **The stale-`PENDING` backstop has no pause awareness, by design.** `_requeue_stale_pending_once` skips any job carrying a live `jobs:delayed` score, which is exactly the state every pause-deferred job is left in, and §6 is a terminal gate for anything it does re-publish anyway: a paused job that reaches `_run_job` is declined and re-deferred. Adding a seventh probe would buy a per-row ancestor walk (up to 100 rows a minute) for a case already covered twice.
- **Dep-less jobs are out of scope at create time.** A job with no declared dependencies joins no chain, so nothing in its ancestry can be paused; the create-time hold only applies when `dependencies` is non-empty.

### Unchanged

Fail-open on Redis errors (§4 of the decision) applies identically at all six probes: a lookup that raises dispatches as if unpaused and logs the fall-through. A Redis blip letting one retry through is the accepted trade; a Redis blip freezing every DAG in the system is not.

Cost: one `JobDependencyRepository.parents()` walk plus one `MGET` per dispatch. On the `_run_job` path the probe sits inside the existing load transaction so no extra session is opened, and the dispatch-latency SLO (95% < 30s) has orders of magnitude of headroom.
