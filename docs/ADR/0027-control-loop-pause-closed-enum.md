# ADR 0027 — One hook pauses a background loop, and the enum of loops is closed
*Status: Accepted · 2026-09-17 · WO-R3-200 (plan v2.1 WP-4.1) · amended 2026-09-17 by WO-R3-213 (WP-7.1): pausing the resume sweep suspends a correctness backstop*

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

## Amendment, 2026-09-17 — suspending a correctness backstop (WO-R3-213, plan v2.1 WP-7.1)

The decision above added `resume_unblocked_waiting` to the enum and said Family C
needs it. Building that family showed the member is not like the other ten, and
the difference is worth a record rather than a comment.

**The fault is composed of two mechanisms, and neither alone produces it.**
Plan 01 §7.2 wants a child stuck `WAITING` with nothing dead-lettered and nothing
paused, so the correct answer is to escalate rather than to replay
(`create_stuck_dag`) or to un-pause (`pause_dag`). Two things promote such a
child, and the world exists only while both are stopped:

| Stopped | How | Mechanism |
|---|---|---|
| `dependency-resolver` consumer group | `kill_consumer('dependency-resolver')` | `chaos:kill:<group>`, checked in `BaseKafkaConsumer` per poll |
| resume sweep | `pause_control_loop('resume_unblocked_waiting')` | `chaos:pause:<loop>`, checked in the loop per iteration |

Two keys, two checks, two tools — deliberately, because they stop two different
runtime kinds, which is the same reasoning that kept the consumer groups out of
the enum in the first place (divergence H2). The lab sets both and the reset's one
`chaos:*` scan clears both; `test_pause_control_loop.py` asserts that of the exact
pair rather than of the pattern in general.

**The recovery is asymmetric, and only one of the two TTLs matters.** The resolver
reacts to `job.completed` and nothing else. In this world the parent's completion
is already in the past, so the resolver returning promotes nothing — there is no
event left to deliver. **The sweep's pause expiring is the only thing that heals
the world**, which means the fault's duration is bounded by the pause TTL (capped
at 3600 s), never by the kill TTL, and that a teardown which cleared only the kill
key would leave a permanently stranded child behind. Stated here because the
intuition runs the other way: the kill looks like the bigger act.

**What makes the sweep different from the other ten members.** The other loops are
relays, promoters and sweeps whose absence delays work. This one is a *correctness
backstop*: [ADR 0022](0022-promotable-only-resume-sweep-and-dependency-cascade.md)
and [ADR 0011](0011-dag-pause-enforcement.md) together make it the reason a DAG
pause is temporary rather than terminal — the resolver has already consumed the
parent's completion event by the time a pause lifts, so without the sweep a held
child would stay `WAITING` forever. Pausing it deliberately suspends that
guarantee for a bounded window.

**Why that is safe in a lab.** The window is bounded by a TTL the caller cannot
raise past an hour; `scripts/reset_eval_state.py` sweeps the key between
scenarios; the hook registers only under `CHAOS_ENABLED=true` and the check
short-circuits on that flag before any Redis call; and nothing is harmed that the
expiry does not undo — the first unpaused iteration promotes the child and mints
its `job.submitted`, with no operator action and no compensator. Unlike
`lease_renewal` (above), this pause cannot leave a row in a state the TTL does not
reverse.

**Why it would not be safe in production.** Two reasons, and the second is the
one that would hurt. A suspended backstop means every DAG pause that lifts inside
the window, and every promotion event lost to a redelivery gap, strands its
children until the flag expires. And it does so **silently**: a `WAITING` child
raises no alert, dead-letters nothing, and appears in no queue depth — the
cascade in `cascade_cancel_blocked_children` covers a parent that can never
complete, not a promotion that never arrived. The observable symptom is work that
does not happen, which is the hardest class of fault to notice and exactly why
this world is worth evaluating an agent on.

**Blast radius, stated more precisely than `single_loop` implies.** The member
name is accurate about the process — the eight consumer groups and the other ten
loops keep running. But the sweep is cross-tenant by design (it is a
platform-level scheduler, not a request path), so pausing it holds promotions for
**every** DAG in the environment for the duration, not only the scenario's chain.
In a single-scenario lab that is the intent; it is a second reason the TTL is
short and the reset sweeps the key.

**Nothing agent-visible changed.** No tool was added, no description, schema or
enum member moved: the world is read entirely through tools that already exist —
`get_dag_state` (parent `completed`, child `waiting`, `paused` false with
`paused_by` null, the child's `created_at` long past) and `list_dlq_messages`
(empty). `tests/api/test_read_tools_never_name_the_lab.py` now arms both of this
world's keys while it screens every read tool's response, because a scenario whose
correct answer is to escalate is the one where a leak does most damage: there is
nothing to fix, so a hint that something was done *to* the platform would be the
only lead in the world.

**What the agent still cannot read.** How long the sweep has been idle. WP-4.2 gave
the outbox relay a per-pass heartbeat (`outbox:relay:last_tick`,
[ADR 0028](0028-outbox-relay-heartbeat-and-delivery-reading.md)) and the same
reading generalised to "last tick age per loop" would let a caller distinguish a
child nothing has got to yet from one nothing is coming for, instead of inferring
it from `created_at` against its own clock. This packet does not add it — the work
order asks for no status read, and an agent-facing tool is a contract delta that
belongs to a packet that asks for one. Recorded as a follow-up.

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

## Amendment, 2026-09-17 — owner decision O-23: retain all eleven members

The enum remains at eleven members. A member's presence means the platform has
one verified, bounded pause mechanism for that loop; it does not commit the
evaluation corpus to a scenario for every member. Scenarios are added only for
loops with a contrast family. The first families are outbox relay, dependency
resolver, saga coordinator, and read model; unused members remain available
until a later family needs one.

Dependency resolver, saga coordinator, and read model are consumer groups, so
their scenarios stop them with `kill_consumer`. Where a family also needs a
background-loop absence, it combines that stop with the appropriate
`pause_control_loop` pause, including the resume sweep for a stranded `WAITING`
child. This keeps the runtime kinds and their teardown keys explicit.
