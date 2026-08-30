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

## Amendment — 2026-08-09 (WO-P4-05)

*The accepted decision above stands unchanged; this note extends it to the two states it did not name — the consumer's FIRST start, and a kill-key lookup that fails rather than answers.*

### Supervision owns `start()`

The decision says "the supervisor is the only owner of the consumer's start/stop transitions during normal operation", but `worker_loop` still performed the initial `start()` itself and supervised only the consumers that survived it (a `started: list[BaseKafkaConsumer]` that failed starters were filtered out of). Boot was therefore the one transition the supervisor did not own, and it failed in exactly the shape [postmortem 0002](../postmortems/0002-phantom-supervisor.md) describes: one transient Kafka/DNS error at startup and that consumer group was silently gone for the life of the process, with no supervisor to bring it back.

`worker_loop` now hands every consumer to `_supervise_consumer` unstarted. The supervisor starts it through `_restart_consumer` — the same capped-backoff `stop()`+`start()` helper the crash path uses — so a boot failure heals like any other transient. The guard sits *before* the `while True`, not inside it: inside, an orderly `stop()` (which leaves `is_running` False, the ADR's third exit reason) would restart every consumer during shutdown.

Consequence worth stating plainly: the "dispatcher consumer not running — worker disabled" special case is gone. A permanently unreachable broker now leaves all 8 supervisors retrying with capped backoff instead of the worker shutting itself down. That is this ADR's stated bias — transients heal — applied to boot; a hard, permanent broker outage is an infrastructure alarm, not a reason to discard the consumer groups.

### The kill-window wait fails closed

The chaos-kill branch polled `_check_chaos_kill`, which returns False on *any* Redis error. In the consumer's own poll loop that fail-open bias is right (a Redis blip must not stall real message processing), but in the supervisor it inverts the meaning of the wait: a blip — including one caused by the `saturate_redis` chaos hook running concurrently — read as "kill cleared" and resurrected the consumer in the middle of the window the scenario was measuring.

The supervisor now polls `_check_chaos_kill_strict` (`kafka_consumer.py`), which lets lookup errors propagate, and holds the consumer down on error, logging one warning per 2s poll. Only an *observed-absent* key releases it. `_check_chaos_kill` is unchanged and remains the poll-loop variant. Trade-off: a permanent Redis outage keeps the consumer down until Redis returns — acceptable because the kill key carries a TTL, so by then it has usually expired and the restart proceeds.

### Verification (supersedes the "no direct unit test" note above)

The gap the Verification section admitted — "the supervisor itself does not have a direct unit test" — is now closed for the lifecycle branches, in `backend/tests/unit/test_dispatcher.py`:

- `test_supervise_consumer_retries_failed_boot_start` — start() fails once, then succeeds; supervision retries rather than dropping the group.
- `test_supervise_consumer_holds_consumer_down_on_kill_key_lookup_error` — two failing kill-key lookups do not restart the consumer; only the third, non-error, absent-key answer does.
- `test_supervise_consumer_does_not_resurrect_on_orderly_stop` — the third exit reason still exits, with no extra `start()`.

The crash branch (`run()` raises) is still covered only by the live eval scenario.

## Amendment — 2026-08-30 (WO-R2-10)

*The accepted decision above stands unchanged; this note extends supervision one level up, to the task that hosts every supervisor.*

### The worker task is supervised too

This ADR made supervisors "the unit of consumer lifecycle" and then left the thing holding all of them unsupervised. `worker_loop` was started from the API lifespan with a bare `asyncio.create_task`, and nothing ever looked at the task again: no done-callback, no restart, no liveness signal. The phantom-supervisor shape one level up — this time with a larger blast radius, because a dead `worker_loop` is not one consumer group, it is all eight plus all nine background loops.

Every loop catches `Exception` around its body, so only two paths escape:

- **the deferred imports before a loop's `while True`** (`_promote_dlq_replay_loop`, `_digest_loop`, `_idempotency_reaper_loop`) — an import regression raises outside every guard, and `asyncio.gather` propagates it out of `worker_loop`;
- **a `CancelledError` reaching `_supervise_consumer`**, which re-raises it by design (the table above); `worker_loop` unwinds and the task ends *cancelled*, with no exception stored anywhere.

`backend/app/workers/supervisor.py` now owns that task. Policy deliberately mirrors the consumer supervisor: log, restart, capped exponential backoff, unbounded attempts. Two deviations, both stated here because they differ from the table above:

- **The first restart is immediate** (the consumer path sleeps 2s first). At this level a one-off crash costs *all* job processing, so the ladder starts at the second consecutive failure: 0s, then 1s → 30s.
- **A run of 60s or more resets the ladder**, so next month's transient does not inherit today's backoff.

### The deep health check now means something different

Restarting a dead worker is only half of it — the half that fails when the worker cannot come back. The other half is admitting it, and the metric that should have carried that signal cannot: `ConsumerLag`, which both backlog alarms read, is emitted by `_metrics_loop` — a loop *inside* the dead worker — and is deliberately not emitted when the lag is unknown, so a dead worker produces absent datapoints rather than high ones. Both alarms treat missing data as `notBreaching`; `infra/cloudwatch.tf` states this and assigns the dead-worker case to "the ECS task-count alarm and to worker supervision, not here". This is that supervision. Worker death silenced exactly the metrics that would have detected it.

So the liveness lands on `GET /api/v1/health`, which both the ECS container check (`infra/ecs.tf`) and the ALB target group (`infra/alb.tf`) already probe every 30s with a 3-failure threshold. **A green answer there no longer means "this process can reach Postgres and Redis" — it means "…and it is processing jobs."** That is a deliberate widening, and it is only correct because `worker_loop` runs *inside* the API process (see "More than one process runs this" in ARCHITECTURE.md); the day a separate worker deployable exists, this probe has to move with it.

> **Superseded in part — see the [2026-08-30 amendment](#amendment-2026-08-30--the-probes-were-split-wo-r2-65) below.** Reporting worker liveness to the probe with restart authority was right and still holds. Reporting it on the *same endpoint* as Postgres and Redis, and letting the ALB probe that endpoint too, was not: it gave a shared dependency the power to deregister every target and recycle every task. The signal moved to `/healthz/worker`; the reasoning above is unchanged.

Liveness is answered from three sources, cheapest first, with no I/O:

1. the supervisor's state (`not_started` / `running` / `restarting` / `stopped`);
2. `worker_task.done()` — the truth, and true the instant the worker dies, so there is no window where a stale recorded state reads healthy;
3. two heartbeats sharing one staleness bound (`worker_heartbeat_stale_seconds`, default 60s), the backstop for what the first two cannot see.

The heartbeats sit on **separate timestamps**, which is the point of having two. `heartbeat()` is refreshed by the supervisor's own watchdog: its silence means the supervisor stopped being scheduled, the one failure `task.done()` cannot report because the reporter is what died. `worker_tick()` is called from `_promote_delayed_loop` (dispatcher.py), which turns every `POLL_INTERVAL` (0.5s) and touches both Redis and Postgres: its silence means the gather is alive but the loops are wedged. On a shared timestamp the watchdog would keep refreshing on behalf of loops that had stopped turning — the exact case worth catching — so merging them would have quietly cost the second signal.

The tick bound is enforced only once a tick has been observed in the process, so a build without the dispatcher-side call degrades to the supervisor-only signal rather than reporting every task in the fleet unhealthy over a deleted line. A health check must not be able to fail the thing it measures.

The backoff ceiling (30s) sits well inside the probes' 3 × 30s unhealthy window on purpose: a worker that can recover does so in-process, and only one that cannot gets its task recycled.

The one line this adds to `dispatcher.py` — `worker_tick()` at the top of `_promote_delayed_loop`'s body — is the whole of the worker-side contract. It is deliberately a call *into* the supervisor rather than a callback registered by it: no signature changes, no lifecycle to keep in sync, and a loop that knows nothing about who is reading. Moving the tick to a different loop later is a one-line move.

### Shutdown cannot be aborted by the worker's stored exception

`await worker_task` on a task that already stored an exception re-raises it. In the old lifespan that await ran *before* `stop_producer()` and both Redis pool closes, so every shutdown following a worker crash skipped all of them. `supervisor.stop()` cannot raise, and the metrics-emitter stop below it is now guarded for the same reason.

### Verification

`backend/tests/unit/test_worker_supervision.py` — eight tests driving the *real* lifespan (nothing else in the suite does; `httpx.ASGITransport` skips startup/shutdown, which is why a dead worker had no test that could see it): the done-callback logs the death, a crashed worker restarts, a *cancelled* worker restarts, an orderly shutdown does not resurrect it, `/api/v1/health` reports 503 with a dead worker and 200 with a live one, a stale heartbeat is unhealthy on its own, and shutdown still closes the producer and both pools after a worker crash.


---

## Amendment (2026-08-30) — the probes were split (WO-R2-65)

**Status:** Accepted · **Amends:** "The deep health check now means something different", above.

### What was wrong

This ADR put worker liveness on `GET /api/v1/health` because that endpoint was already probed by something that could act on it. That was true, but it was probed by *two* somethings, with different powers, and the endpoint also reported two shared dependencies:

| Probe | Power | Read the deep check |
|---|---|---|
| ECS container check | replace the task | yes |
| ALB target group | remove the target from rotation | yes |

`/api/v1/health` returns 503 when Redis is unreachable. Redis is shared by every task, and every request path that touches it already fails open — so a Redis blip made the ALB deregister **all** backend targets at once (an outage assembled out of a degradation, with nothing to route around because every target failed together) and made ECS **replace every task** mid-job, destroying in-flight work to arrive back at the same unreachable Redis. Neither reaction can fix its cause. The widening this ADR chose was sound; the endpoint it chose carried three unrelated signals to two automatic actuators.

### Decision

Three endpoints, one question each:

| Endpoint | Question | Consumer |
|---|---|---|
| `/healthz` | can this task serve HTTP? | ALB target group |
| `/healthz/worker` | is this task worth keeping? | ECS container check |
| `/api/v1/health` | what is the state of everything? | operators, dashboards |

`/healthz/worker` reports exactly what this ADR argued for — the supervisor state, `task.done()`, and the two heartbeats — and nothing else. It performs no I/O, so it cannot fail for a dependency; a worker that can recover still does so in-process well inside the 3 × 30s window, and one that cannot still gets its task recycled. **Nothing automatic reads the deep check any more.** That is the whole change: it keeps the full picture for the human who curls it during an incident, and it has no authority over traffic or restarts.

### Consequences

- A dead worker no longer deregisters the task from the ALB. The API half is still serving, so it should keep receiving traffic; the remedy is a recycle, which the ECS probe orders.
- A Redis or Postgres outage no longer recycles tasks or empties the target group. It shows up in the deep check, in `RedisMemory`/`DatabaseConnections` alarms, and in the failure the callers report — none of which is a reason to destroy running work.
- The invariant this ADR relies on is unchanged: `worker_loop` runs inside the API process, so "is this task worth keeping" is still a question about the worker. The day a separate worker deployable exists, `/healthz/worker` moves with it — same caveat as before, now attached to a probe that means only that.

### Verification

`backend/tests/unit/test_worker_supervision.py` — the original eight tests plus four for the split: the worker probe reports a dead worker, the worker probe is green with a live one, the ALB probe stays 200 with a *dead* worker, and — the assertion this amendment exists for — with Redis unreachable, `/healthz` and `/healthz/worker` both stay 200 while `/api/v1/health` returns 503.

`backend/tests/unit/test_probe_paths.py` keeps the Terraform and the route table from drifting apart: every probed path is a real route, the target group probes `/healthz`, the container check probes `/healthz/worker`, and neither infra file names the deep check.
