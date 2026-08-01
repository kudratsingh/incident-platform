# ADR 0009 — Consumer lifecycle and supervision

**Status:** Accepted (v0.4.5) · **Date:** 2026 Q3 · **Owner:** Platform

## Context

Wave 1 added `kill_consumer` (chaos) and `restart_consumer_group` (Tier-1 action) with docstrings promising a kill-then-restart round-trip. Wave 2 added `inject_latency` with a similar promise: "restart clears the latency by dropping the consumer's Redis state." In practice, neither round-trip existed:

- `kill_consumer` sets a Redis kill key that causes `BaseKafkaConsumer.run()` to return.
- `restart_consumer_group` deleted the kill key.
- Nothing called `stop()`/`start()` on the returned-from consumer, so it stayed dead until the worker process restarted.
- `restart_consumer_group` did not delete the `latency_key`, so a slowed consumer stayed slow after "restart".

The blast radius was operational: every live remediation eval that killed a consumer left the platform with a permanently dead consumer group. Failures compounded across runs — each new eval looked novel, but the underlying platform state was drifting. Same pattern with `inject_latency`: after the first invocation, the platform ran at reduced throughput until the worker process restarted.

Three docstrings referenced "the supervisor" that was never implemented. `restart_consumer_group` satisfied its *name* while its documented effect was impossible.

## Decision

**Supervisors are the unit of consumer lifecycle.** Each consumer in `worker_loop` is wrapped in `_supervise_consumer(consumer)` (`backend/app/workers/dispatcher.py`) instead of `consumer.run()`. The supervisor is the only owner of the consumer's start/stop transitions during normal operation.

`BaseKafkaConsumer.run()` has three exit reasons, each with a defined supervisor response:

| Exit reason | Signal | Supervisor response |
|---|---|---|
| **Chaos kill** | `run()` returns with `consumer.chaos_killed == True` | Poll the Redis kill key every 2s until it clears (via `restart_consumer_group` or TTL expiry), then `stop()`+`start()` for a fresh `AIOKafkaConsumer` and re-enter `run()`. |
| **Crash** | `run()` raises | Log, sleep, `stop()`+`start()` with capped exponential backoff (1s → 30s), re-enter `run()`. |
| **Orderly stop** | `run()` returns with `consumer.is_running == False` | Supervision ends. This is the only path that exits the supervisor loop; happens when `worker_loop`'s outer cancellation calls `stop()` during shutdown. |

`restart_consumer_group` is the **compensating counterpart to both consumer-affecting chaos hooks** — it clears the kill key *and* the latency key in one call. The tool's output model gains additive `latency_key_cleared` / `latency_key` fields; older commanders that don't know about these fields ignore them (`extra="ignore"`) so the change is backward-compatible.

## Alternatives considered

### Process-level restart

Kill the entire worker process; ECS/Docker restarts it; all 8 consumer groups come back.

**Why not:** the eval scenarios kill *one* consumer to test targeted recovery. Restarting all 8 destroys the fault isolation that Phase 7 was built to prove — a stalled `event-log` consumer shouldn't cause the dispatcher to bounce, and vice versa. Also, on ECS Fargate a full task recycle takes ~30s and evicts every in-flight job; the supervisor's `stop()`+`start()` cycle takes ~2s and preserves the offsets.

### Key-watch push model (Redis pub/sub)

`restart_consumer_group` publishes to a channel; the consumer's supervisor subscribes and reacts immediately.

**Why not:** polling every 2s is one line of code and the recovery-latency target is measured in tens of seconds (the verify-with-deadline windows in the commander are `4 × 20s = 80s`). Push adds a subscription lifecycle, reconnect handling, and a second failure mode. The complexity buys us maybe 1s of recovery latency in the happy path and introduces a new "supervisor missed the wake-up" bug class. Rejected on cost/benefit.

### Consumer self-supervises (loop inside `run()`)

Move the poll-and-restart logic into `BaseKafkaConsumer.run()` itself; no external supervisor.

**Why not:** conflates two responsibilities — the consumer knows about a single Kafka session, the supervisor knows about the *sequence* of sessions across chaos/crash cycles. Merging them means `run()` can no longer be modeled as "consume until told to stop", and every future consumer subclass has to think about restart semantics. The current shape lets `BaseKafkaConsumer` stay stateless across sessions and puts all the sequencing in one supervised place.

### Restart only clears the kill key; latency requires a separate tool

Add `clear_injected_latency` as a distinct Tier-1 action.

**Why not:** the mental model in the docstrings and in operator practice is "restart is the reset button for consumer state". Splitting these means the operator has to remember to call both to fully recover, and every scenario that combines chaos hooks has to hard-code the ordering. The output model makes both fields explicit, so an operator inspecting a run can still see which key was actually cleared.

## Consequences

### Positive

- **Chaos kills are recoverable.** The kill-then-restart round-trip that three docstrings promised is now real, and each restart tears down and rebuilds `AIOKafkaConsumer` — no reused-session bugs.
- **Crashes self-heal.** A consumer that raises during `run()` gets restarted with backoff. Before this ADR, a single `event-log` crash meant the event-sourced timeline stopped updating until the process bounced.
- **Live evals stop compounding drift.** Each remediation scenario starts from the same state, because the supervisor undoes the fault before the scenario ends.
- **`inject_latency` has a matching remediation.** `restart_consumer_group` clears both keys; the chaos framework's help text no longer promises behavior it can't deliver.

### Negative

- **`worker_loop` becomes chatty on chaos runs.** Every kill/restart cycle emits three log lines (killed, waiting, restarted). Acceptable — these are the events an on-call cares about; the alternative is silence during the exact window when observability matters most.
- **Backoff on crash means slow degradation is visible.** If a consumer crashes repeatedly (bad message it can't skip, dependency down), the supervisor keeps restarting instead of surfacing a hard failure. Mitigated by structured logs at ERROR level on each restart attempt; a follow-up could add a "N crashes in T seconds → escalate" circuit breaker but the current shape's cost is bounded (max 30s backoff).
- **Reserved: supervisor teardown during shutdown.** If `worker_loop` is cancelled mid-poll, the supervisor's `await asyncio.sleep(...)` gets a `CancelledError` which propagates cleanly. Not a bug, but every future edit here must preserve the invariant that `CancelledError` is re-raised, not swallowed.

### Reversibility

Reverting is deleting `_supervise_consumer` + `_restart_consumer` in `dispatcher.py` and swapping `asyncio.create_task(_supervise_consumer(c))` back to `asyncio.create_task(c.run())`. The `chaos_killed` / `is_running` properties on `BaseKafkaConsumer` can stay — they cost nothing when unused. Fully reversible in one commit.

## Verification

- Unit contract: `test_restart_consumer_group_clears_kill_key` covers the kill+latency both-set case; `test_restart_consumer_group_clears_injected_latency_without_kill` covers the latency-only path.
- End-to-end proof (deferred to live eval): `make chaos-inject-latency` → `remediate_consumer_lag` scenario passes without any manual intervention; `kill_consumer` → `restart_consumer_group` observed recovery in `get_consumer_lag`.
- The supervisor itself does not have a direct unit test — testing it in isolation requires mocking `AIOKafkaConsumer` lifecycle, which the existing tests do only via integration-style setups. The pair-round-trip contract test above proves the compensation half; the supervisor half is proven by the live eval scenario.

## Pointers

- `backend/app/workers/dispatcher.py` — `_supervise_consumer`, `_restart_consumer`, `_SUPERVISOR_POLL_SECONDS`, `_SUPERVISOR_MAX_BACKOFF_SECONDS`.
- `backend/app/workers/kafka_consumer.py` — `chaos_killed` / `is_running` properties; `_chaos_killed` set inside `run()` when the kill key is observed.
- `backend/app/mcp/tools/actions/restart_consumer_group.py` — dual-key clear + additive output fields.
- `backend/tests/api/test_mcp_wave3_tier1_actions.py` — the two contract tests.
- Related ADRs: [0006 — MCP server standalone process](0006-mcp-server-standalone-process.md), [0008 — Chaos framework triple-gating](0008-chaos-gating.md).
- Postmortem: [0002 — The phantom supervisor](../postmortems/0002-phantom-supervisor.md).
