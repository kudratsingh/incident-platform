# Postmortem 0002 — The phantom supervisor

**Status:** Landed with v0.4.5 · **Written:** 2026 Q3 · **Owner:** Platform

## Impact

Seven consecutive live remediation-eval runs of the incident-commander agent failed with different-looking symptoms — a new bug every run, no coherent story. Root cause turned out to be that the *first* eval killed the dispatcher consumer and it never came back; every subsequent scenario ran against a platform with a permanently dead consumer group. Each new failure looked novel while the underlying state drifted.

Debug time burned: ~7 eval runs × ~15 minutes each of investigation + a full afternoon of triaging what turned out to be one bug wearing seven costumes.

## Timeline

- **Wave 1 (PR #56)** ships `kill_consumer` (chaos hook) and `restart_consumer_group` (Tier-1 action). Both docstrings describe a kill-then-restart round-trip. `restart_consumer_group` deletes the Redis kill key that `kill_consumer` set. The docstring on `worker_loop` in `dispatcher.py` references "the supervisor" that will restart the consumer after the kill key clears. No such supervisor exists.
- **Wave 2 (PR #58)** ships `inject_latency`. Docstring: "restart clears the latency by dropping the consumer's Redis state". `restart_consumer_group` was never updated to delete the latency key. Same gap-pattern.
- **v0.4.4** — first live eval run against the commander's remediation loop. `remediate_consumer_lag` scenario invokes `kill_consumer` → observes stalled queue → invokes `restart_consumer_group` → polls `get_consumer_lag` → never sees recovery. Judged as `not_verified`. Debugged as a polling-window sizing issue.
- **Runs 2–5** — different scenarios (bad-deploy remediation, DLQ triage, stale-cache reset) each fail in distinct-looking ways. The dispatcher consumer is dead but nothing in the scenario knows it depends on a live consumer; failure modes present as timeouts, stale reads, and grader `EVIDENCE` failures.
- **Run 6** — operator restarts the worker process manually between runs. Suddenly two scenarios pass that previously failed. First real signal that runs were poisoning each other's environment.
- **Run 7** — reproduces the pass/fail split by isolating: fresh process → kill_consumer scenario passes; second scenario without process restart → fails. Diagnosis: `restart_consumer_group` never actually restarts anything.
- **Fix design and PRs land** — [ADR 0009](../ADR/0009-consumer-lifecycle-and-supervision.md), PR #72 (supervisor), PR #71 (idempotency TTL + audit savepoint). Live eval retries with the supervisor in place: seven scenarios, seven fresh states, seven separately-scored outcomes.

## Root cause

**A supervisor was documented in three places and implemented in none.**

- `worker_loop` docstring in `backend/app/workers/dispatcher.py` referenced "the supervisor" as if it existed.
- `kill_consumer`'s help text described the round-trip: kill → restart_consumer_group → resumed consumption.
- `restart_consumer_group`'s help text advertised itself as "the compensating counterpart to `kill_consumer`". Restore-your-consumer verbs.

What actually existed: `restart_consumer_group` deleted the Redis kill key. That's all. No code ever called `stop()`/`start()` on the returned-from consumer. When `BaseKafkaConsumer.run()` returned because the kill key was set, the `asyncio.create_task(c.run())` future in `worker_loop` completed and was garbage-collected. No further consumer activity happened for that group until the worker process itself restarted.

The `inject_latency` sibling gap was the same shape: docstring described a Redis-key-clearing recovery, `restart_consumer_group` never touched the latency key.

## Detection gap

Three separate misses lined up:

1. **No test asserted the restart contract end-to-end.** The two `restart_consumer_group` unit tests both asserted "the kill key was deleted". Neither asserted "a consumer that had been killed resumed consumption". The gap between the tool's job and the tool's *effect* was invisible to the test suite.
2. **The docstring language wasn't checked against reality.** "The supervisor" appears in three files; the word "supervisor" appears zero times anywhere in the implementation. A basic grep-against-docstrings audit would have caught this in Wave 1.
3. **Live eval runs were shared-state.** No reset between scenarios meant a dead consumer from run N poisoned runs N+1, N+2, .... The fact that "each run looked different" was itself the signal that shared state was drifting, but the loop wasn't classified into buckets before it was debugged (see the commander-side lessons doc).

## Contributing factors

- The failure mode was invisible from any single scenario's perspective. The scenario that killed the consumer *did* observe the kill; the scenario that came next didn't know it depended on a live consumer. No single trace had enough information to diagnose the class of bug.
- Postmortem 0001 (v0.4.1 schema drift) is the same *gap-pattern* — a citation to "the v0.4.1 postmortem" existed for months in code comments before the referenced doc did. Docstrings and citations were being written as if they were self-fulfilling, and neither review nor test caught it.
- The eval harness had no `--reset` mode. Every fault injected in a run persisted into the next, but nothing in the workflow named this as the invariant being violated. Filed as follow-up #24 (systemic).

## Fix

Landed in PR #72 (v0.4.5):

- **`_supervise_consumer`** (`backend/app/workers/dispatcher.py`) wraps each consumer's `run()` in `worker_loop`. Three exit reasons, three defined responses (chaos-kill → poll → restart; crash → backoff → restart; orderly stop → end supervision). Full mechanism in [ADR 0009](../ADR/0009-consumer-lifecycle-and-supervision.md).
- **`BaseKafkaConsumer.chaos_killed` / `is_running` properties** give the supervisor the signal it needs to distinguish the three cases.
- **`restart_consumer_group` clears both keys** (kill_key and latency_key). Additive output fields (`latency_key_cleared`, `latency_key`) are backward-compatible for older commanders that ignore unknown fields.
- **Two contract tests**: the kill+latency both-set case, and the latency-only path (`test_restart_consumer_group_clears_injected_latency_without_kill`). The tests are named for the *contract*, not the implementation — the tool's promise is "the compensating counterpart to both chaos hooks", so the tests prove both round-trips.

## Prevention rule adopted

**Docstrings are contracts — test the promise.**

Any docstring, help text, or code comment describing runtime behavior gets a test named for the claim. A referenced doc must exist. In particular:

- Every chaos hook must **name its compensating action and link the contract test proving the pair round-trips**. Codified as an amendment to [ADR 0008](../ADR/0008-chaos-gating.md).
- Every citation to a doc that doesn't exist yet is treated as a PR blocker. If postmortem X is referenced in a code comment, postmortem X exists in the same PR.
- The `restart` contract test isn't "the kill key was deleted"; it's "a consumer that had been killed resumed consumption". Tests are named for what the tool *does for the caller*, not what it does internally.

Enforcement is the PR-review checklist: "every behavioral claim added/edited has a matching test or issue".

## Related

- [ADR 0009](../ADR/0009-consumer-lifecycle-and-supervision.md) — the supervisor design.
- [ADR 0008](../ADR/0008-chaos-gating.md) — chaos gating, amended with the compensating-action rule.
- [Postmortem 0001](0001-v0.4.1-schema-drift.md) — same gap-pattern (docstring citations without backing implementation/doc) in the schema-drift subsystem.
- PR #72 — the code fix.
- Commander-side companion: `docs/lessons/live-eval-noise-sources.md` (incident-commander repo) — the taxonomy of run-failure buckets that turns "a new bug every run" into a diagnosable distribution.
