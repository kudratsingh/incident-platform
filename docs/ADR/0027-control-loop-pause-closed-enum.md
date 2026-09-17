# ADR 0027 — One hook pauses a background loop, and the enum of loops is closed
*Status: Accepted · 2026-09-17 · WO-R3-200 (plan v2.1 WP-4.1)*

## Context

The eval needs a fault where **jobs are accepted but nothing executes**, with
evidence that is the *opposite* of the fault we already have. `kill_consumer`
stops the `worker-dispatcher` consumer group: submissions land, Kafka fills up,
consumer lag climbs. The contrast is a stalled **outbox relay**: the same
top-level symptom, but the backlog is in Postgres (`outbox_events` unpublished
and ageing) while consumer lag stays flat, because nothing is reaching Kafka to
fall behind on. One symptom, two worlds, opposite readings — that contrast is
the whole reason the `jobs_not_progressing` family is built first (plan 01 §7.1).

The relay is not a consumer group, so `kill_consumer` cannot touch it. Eleven
background loops run beside the eight consumer groups inside
`dispatcher.worker_loop`, and none of them was reachable by any hook.

The plan proposed the mechanism and an eight-member enum:

```text
pause_control_loop(loop_name: Literal[outbox_relay, dependency_resolver,
  saga_coordinator, read_model, delayed_retry_promote, dlq_replay_promote,
  stale_pending_backstop, stale_running_sweep], ttl_seconds)
```

Checked against the code, that enum mixed two runtime kinds (divergence report
row H2) and missed one loop a later family depends on (row H3).

## Decision

**1. One hook, one key pattern, one check per loop.**
`pause_control_loop(loop_name, ttl_seconds)` sets `chaos:pause:<loop>` with a
TTL. Each loop reads its own key once per iteration and skips that iteration's
work while it is set. Eleven tools would be eleven descriptions to keep true
for one idea; one tool with a closed enum is the same mechanism with a single
place to be wrong.

**2. The enum is the eleven background loops, and nothing else.**

| Member | Loop |
|---|---|
| `outbox_relay` | `_outbox_relay_loop` |
| `delayed_retry_promote` | `_promote_delayed_loop` |
| `dlq_replay_promote` | `_promote_dlq_replay_loop` |
| `resume_unblocked_waiting` | `_resume_unblocked_waiting_loop` |
| `stale_pending_backstop` | `_requeue_stale_pending_loop` |
| `stale_running_sweep` | `_stale_running_sweep_loop` |
| `lease_renewal` | `_renew_running_leases_loop` |
| `slo_evaluation` | `_slo_evaluation_loop` |
| `metrics` | `_metrics_loop` |
| `digest` | `_digest_loop` |
| `idempotency_reaper` | `_idempotency_reaper_loop` |

**The three Kafka consumer groups in the proposed enum are dropped.**
`dependency_resolver`, `saga_coordinator` and `read_model` are consumer groups,
not loops. `kill_consumer` has stopped *any* consumer group since Wave 1 — the
kill key is checked in `BaseKafkaConsumer` at the top of every poll, for any
group id — so a second mechanism aimed at the same three would mean two key
patterns, two checks and two ways for a teardown to miss one. A caller written
against the draft enum now gets an invalid-params refusal naming the closed set,
which is louder than a key no loop reads.

**`resume_unblocked_waiting` is added, and Family C cannot work without it**
(row H3). `_resume_unblocked_waiting_loop` exists to "double as a backstop for
any child whose promotion event was missed" and runs every 10 seconds. Stalling
the dependency resolver alone does *not* strand a child in `WAITING`: the sweep
promotes it within about ten seconds. Any scenario that needs a child to stay
`WAITING` must stop the resolver (via `kill_consumer`) **and** pause this sweep.

**The enum covers all eleven rather than only the loops a family needs today.**
Each member costs one `if` and buys the mechanism for a future family without a
platform release, a re-pin and a snapshot rebless — the campaign's most
expensive step. Two members are honest but weak in practice, and the tool says
so rather than hiding it: see *Consequences*.

**3. The enum is a safety boundary, so it is asserted, not documented.**
`tests/unit/test_pause_control_loop.py` parses `workers/dispatcher.py`, collects
the loop coroutines `worker_loop` actually starts, and asserts a bijection with
the enum — plus, per member, that the matching loop really calls
`loop_is_paused` with that member and no other. A twelfth loop cannot ship
unpausable, and a member cannot outlive the loop it names. The table above is
therefore a convenience; the test is the claim.

**4. Where the check sits is part of the decision.**

- **After `worker_tick()`, never before it.** `_promote_delayed_loop` calls the
  heartbeat the deep health check reads for *every* loop. A pause that skipped
  it would report the whole worker wedged — a process-wide signal for a
  single-loop fault.
- **Inside the outbox relay's leader gate, not in front of it.** The relay is
  single-writer via a Postgres advisory lock ([ADR 0020](0020-outbox-relay-single-writer.md)).
  Checking first would make a paused replica stop contending, handing leadership
  to another replica — the pause would still hold there, since the key is
  global, but leadership would have moved for a reason unrelated to leadership.
  Inside the gate, the gate behaves identically paused or not.
- **Fail open.** An unreachable Redis reads as not-paused, matching
  `_check_chaos_kill`. The strict, fail-closed variant exists only to decide
  whether to *restart* something chaos stopped, where "unknown" must not read as
  "cleared"; nothing here restarts anything, because the key's own TTL ends the
  pause. The alternative trades a real production stall for a lab convenience.
- **Switched off before any Redis call.** `CHAOS_ENABLED` is read in-process and
  short-circuits, so a production deployment pays one boolean per loop per tick.

**5. `BlastRadius` gains a fifth member, `single_loop`.** A consumer group is a
whole subscription; a background loop is one coroutine, with the process, the
eight consumer groups and the other ten loops untouched. Calling that
`single_consumer` would overstate it on every audit row. The member appears in
this tool's `[chaos: single_loop]` description prefix, so it is a contract
delta.

## Consequences

- **A tool-surface change.** `master` with `CHAOS_ENABLED=true` now serves
  **31** tools (11 chaos). The coordinator cuts a release, the commander
  re-pins by index digest and runs `make snapshot`, and the ledgered delta is
  `+pause_control_loop`. Nothing else in `tools/list` moves: no existing tool's
  description, schema or name changes.
- **A pause lands on the loop's next iteration, and the intervals differ by
  three orders of magnitude** — 0.5 s for the delayed-retry promoter, an hour
  for the idempotency reaper. `ttl_seconds` is capped at 3600, so a pause on
  `digest` (default 24 h) or `idempotency_reaper` (1 h) can expire before the
  loop ever reads the flag. Rather than quietly accept a call that will do
  nothing, the result returns `tick_interval_seconds` for the loop asked about,
  and null when the loop is not iterating at all (`slo_evaluation` with its
  interval set to 0, which is how the demo stack runs it). The platform's fourth
  tool-description rule — never advertise what the tool cannot deliver — applies
  to a hook as much as to a read tool.
- **`lease_renewal` has a consequence the TTL does not undo.** Leases stop being
  renewed, and if the pause outlives `STALE_RUNNING_THRESHOLD_SECONDS`
  (default 900 s) the stale-RUNNING sweep may dead-letter live jobs
  ([ADR 0023](0023-dispatcher-sweep-ownership.md)). The pause is reversible; a
  dead-lettered job is a row, undone by a replay. At the capped 3600 s TTL this
  is reachable, so it is stated here rather than discovered in a run.
- **Teardown needs nothing new.** `scripts/reset_eval_state.py` sweeps `chaos:*`
  and the pause key is inside that namespace, which
  `test_eval_reset.py::test_every_chaos_key_helper_lives_under_the_chaos_namespace`
  now asserts for the new helper as well.
- **Nothing the agent reads names the key** ([ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)
  rule 1, extended to response bodies in 2026-09-15). The response-side sweep in
  `tests/api/test_read_tools_never_name_the_lab.py` seeds
  `chaos:pause:outbox_relay` into the world it screens, so every read tool is
  called against a paused world and the whole envelope is checked for the word.
- **The hook alone does not make the fault legible.** The evidence the contrast
  needs — unpublished count, oldest unpublished age, last publish time — has no
  read tool yet; that is `get_outbox_status` (WP-4.2). Until it lands, the
  outbox stall is observable from the database and from `search_traces`, not
  from a single probe.
- **The invisible half is not covered.** ADR 0016 defers principal-scoped
  `tools/list`, so a read-scoped token can still enumerate this tool's name and
  its `[chaos: single_loop]` description, as it can for the other ten hooks.
  Unchanged by this ADR, and recorded again because the enum's member names are
  now on that surface too.

## Alternatives considered

- **A separate hook per loop.** Rejected: identical mechanism, eleven
  descriptions to keep true, eleven names in `tools/list`.
- **Extend `kill_consumer` with a loop mode.** Rejected: a consumer group is an
  open string and a loop is a closed set, and the tool's description would have
  to be true of both. The closed set *is* the safety property here.
- **Ship only the loops Family B and Family C need** (`outbox_relay`,
  `resume_unblocked_waiting`, and the three other real tick loops from the
  proposed enum). Rejected: each extra member is one `if`, while each later
  member added on demand costs a release, a re-pin and a rebless. Recorded as a
  reversible choice — narrowing the enum later is a schema change, so if the
  owner prefers the smaller set it is cheaper to decide before the release than
  after.
- **Keep the three consumer groups in the enum as aliases for `kill_consumer`.**
  Rejected: two key patterns for one effect, and the reset would have to sweep
  both. The refusal is the better teacher.
