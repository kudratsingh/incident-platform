# ADR 0041 — The stuck chain's cause is readable: the resolver's lag and the control loops' state

**Status:** Accepted · **Date:** 2026-10-10 · **Owner:** Platform · WO-R3-371 (INC-008, owner decision O-48) · Builds on [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md), [ADR 0027](0027-control-loop-pause-closed-enum.md), [ADR 0028](0028-outbox-relay-heartbeat-and-delivery-reading.md), [ADR 0029](0029-stranded-chain-and-lab-pause-are-manufactured.md), [ADR 0039](0039-the-platform-pages-on-its-own-metric.md) and [ADR 0040](0040-the-lag-clock-survives-a-restart.md)

## Context

The `workflow_stuck` family's `resolver_stall` world is built exactly as [ADR 0029](0029-stranded-chain-and-lab-pause-are-manufactured.md) §2 says it must be: a chain whose root is `completed` and whose children are `waiting`, held there by `kill_consumer('dependency-resolver')` **and** `pause_control_loop('resume_unblocked_waiting')`. The grader's answer is `resolver_stall`; its nearest neighbour is `saga_coordinator_stall`. On 2026-10-10 a paid run answered the neighbour, and the run before it had answered correctly from the **same eight reads** — `get_dag_state` twice, `search_traces(status=waiting)`, the whole dead-letter queue, the two waiting children and `get_consumer_lag(worker-dispatcher)`. Four attempts, one right. The incident record (INC-008) calls it what it was: not an agent fault, a world whose distinguishing fact could not be read.

Nothing the agent could call named either half of the fault:

| Half | What a read returned |
|---|---|
| the killed resolver | `get_consumer_lag(consumer_group="dependency-resolver")` → `source: unrecognized`, `lag_known: false`. The tool's known groups were worker-dispatcher and seven recorded constants. |
| the held sweep | no tool reported the background loops at all. [ADR 0027](0027-control-loop-pause-closed-enum.md)'s 2026-09-17 amendment named exactly this — "what the agent still cannot read: how long the sweep has been idle" — and left it as a follow-up. |
| who did it | the `chaos.tool_invoked` rows, withheld from the agent's token by design ([ADR 0012](0012-the-lab-is-invisible-to-the-agent.md), 2026-09-15 amendment). |

So a completed root over waiting children with an empty queue was, to the agent, equally "the resolver stopped" and "the coordinator stopped stepping", and the label was a coin flip.

**Measuring the resolver's lag is not enough on its own, and that is the part that shaped this decision.** The resolver consumes `job.completed`. In the stranded world nothing completes, so a dead resolver's lag is flat, fresh and *known* — the same reading a healthy idle resolver gives. What does the platform expose for a dead group today? For `worker-dispatcher` it is lag climbing, and only because the demo keeps submitting jobs: a killed consumer keeps its Kafka assignment for up to the max poll interval (300 s by default), so the metrics pass keeps measuring it, `age_seconds` stays fresh, and no member count exists anywhere. Applied to the resolver, "whatever the dispatcher shows when dead" shows nothing. A liveness reading had to exist.

## Decision

### 1. `dependency-resolver` is measured by the same pass, with the same arithmetic

- `consumer_lag()` moves, unchanged, from `JobDispatcherConsumer` to `BaseKafkaConsumer`, so any consumer can be measured with the same sum, the same "unknown is `None`, never 0" rule and the same [ADR 0040](0040-the-lag-clock-survives-a-restart.md) bounds (the `restarting` flag and the half-a-pass query timeout).
- `_metrics_loop` takes the resolver as an optional fourth argument and `worker_loop` passes it. The two offset queries run **concurrently**: each is bounded at half a pass, and two in a row could cost a whole one.
- The resolver's reading is written exactly like the dispatcher's — `kafka:consumer_lag:dependency-resolver` under the three-pass TTL, and a dated sample in `:samples` — so `get_consumer_lag` answers `source: live` with `measured_at`, `age_seconds` and `recent_samples` (`LIVE_REFRESHED_GROUPS` in `app/core/consumer_lag.py`).
- It is **not** written into the CloudWatch `ConsumerLag` gauge (that series keeps meaning the dispatcher, and a dimension would change what the alarm reads), and `check_backpressure` still gates on the dispatcher's key alone.
- **It is measured, not paged.** `consumer_stalled` now reads `PAGED_LAG_GROUPS = (worker-dispatcher,)`. [ADR 0039](0039-the-platform-pages-on-its-own-metric.md)'s rule was written to cover "a second continuously refreshed group the day it gets a writer", and this deliberately overrides that default: the resolver's normal lag is zero, the relay delivers completions in one-second batches, and after the dispatcher recovers from a backlog — which is the demo's whole third act — a pass at the demo's five-second clock can land inside a burst and read past a threshold of 20 for a fraction of a second. A second page on the demo screen for a group nobody broke is a worse outcome than no page, and paging on the resolver needs a threshold of its own. Left as a decision for the owner.

### 2. A measured group says when its consumer last polled

This is the field that moves when the resolver is killed.

- `BaseKafkaConsumer.last_poll_at` is set in memory at the top of every poll, after the kill and latency checks — so a consumer the lab stopped or slowed stops stamping exactly when it stops asking. **No I/O in the poll loop:** today a production consumer makes no Redis call per poll (the chaos checks short-circuit on `CHAOS_ENABLED`), and a diagnostic must not make every consumer's poll depend on Redis.
- The metrics pass publishes it every pass to `kafka:consumer_lag:<group>:last_poll`, **whether or not the lag was measurable**, under the value key's three-pass TTL. Fresh-or-absent on purpose: if the pass itself stops, an old poll time left readable would make a live consumer look dead; this way it reads unknown.
- `get_consumer_lag` gains `last_poll_at` and `last_poll_age_seconds` (null for the seven constants, for an unrecognised group, and whenever no readable poll time is on record). The reading rule, stated in the description: a running consumer polls every couple of seconds even with nothing to read (a 0.5 s fetch inside a 2 s bound, plus its batch), so on a consuming group `last_poll_age_seconds` is never more than a few seconds above `age_seconds`; well above it, and growing call after call, is a consumer that had stopped asking before the platform last measured — whatever the lag reads, including 0.
- It applies to `worker-dispatcher` too, by the same mechanism. Additive: the lag fields there do not move.

**Exactly which fields move for a killed resolver:**

| Field | Healthy, idle | Killed |
|---|---|---|
| `lag`, `lag_known`, `source` | `0`, `true`, `live` | **unchanged** while the consumer holds its assignment: flat when no completion arrives, +1 per completion that does. Past Kafka's max poll interval (300 s by default) the consumer may leave the group, after which the lag can read unknown — the same path the dispatcher's reading has always had |
| `age_seconds`, `recent_samples` | fresh, one sample per pass | **unchanged** — the pass keeps measuring |
| `last_poll_at` | within ~2.5 s of each measurement | **frozen** at the moment the kill stopped the poll loop |
| `last_poll_age_seconds` | ≤ `age_seconds` + a few seconds | **grows one second per second** from the kill, far above `age_seconds` |

### 3. The loops' state is one new read tool, `get_control_loops`

**Why a tool and not a field.** [ADR 0028](0028-outbox-relay-heartbeat-and-delivery-reading.md) already rejected widening `get_consumer_lag` for the relay — two different queues, two clocks, two failure modes — and the loops are a different runtime kind from the consumer groups ([ADR 0027](0027-control-loop-pause-closed-enum.md), divergence H2). No existing read covers the worker process: `get_redis_health` / `get_postgres_health` describe dependencies, `get_outbox_status` one loop's queue, so a field on any of them would put eleven loops under a name about something else. [ADR 0006](0006-mcp-server-standalone-process.md)'s house style is one question per tool, and this is one question: is each loop running?

**Shape.** `telemetry:read`, no arguments (`extra="forbid"`), every loop on every call in the enum's fixed order, `total` as a count rather than a page size:

| Field | Meaning |
|---|---|
| `measured_at` | the answering process's clock |
| `loops[].name` | the `ControlLoopName` member — the closed set [ADR 0027](0027-control-loop-pause-closed-enum.md) asserts against `worker_loop` |
| `loops[].paused` | `loop_pause_state` — the predicate `loop_is_paused` decides on (the key counts only under `CHAOS_ENABLED`) |
| `loops[].paused_expires_in_seconds` | the key's TTL, the shape `get_dag_state` already uses for a DAG pause |
| `loops[].tick_interval_seconds` | `tick_interval_seconds(loop)` — the yardstick for the age, from the same function `pause_control_loop` reports |
| `loops[].last_run_at` / `last_run_age_seconds` | the loop's own record of its last completed pass (below) |
| `unknown_reason` | set only when the store could not be read; every `paused` and `last_run_at` is then null |

**The pass record.** Each of ten loops stamps `worker:loop:<loop>:last_run` (`app/core/loop_heartbeat.py`) **after its own pause check and after its work** — a paused loop keeps turning while it skips its work, so a stamp on every turn would report it running. The eleventh, the relay, already stamps every pass inside its tick ([ADR 0028](0028-outbox-relay-heartbeat-and-delivery-reading.md)), and its loop reading **is** that record: one pass, one record, so the two tools cannot disagree. A pass counts whether or not there was work (the relay's idle-pass rule), which is why `digest` now records a pass when its feature flag is off. Keys are outside `chaos:*`, so the reset does not clear them and their name cannot hint at why a loop stopped; TTL is three days (three of the daily digest's interval). The write fails open; `tests/unit/test_resolver_and_loops_observable.py` holds the placement statically, like the pause bijection.

**Failure.** The loop's check fails open, because a lost check must not stop work. The reader does the opposite on purpose: a reader that failed open would report "not paused" about a pause it could not see. So `loop_pause_state` raises, and the tool turns that into one explicit `unknown_reason` with every state null — never eleven healthy loops.

## What stays hidden, what is now visible, and why

**Hidden, unchanged.** Who paused a loop or killed a group: the `chaos.` rows that record the fault stay withheld from any principal without `chaos:invoke`, in SQL and out of `total`. Neither reading has an actor field; neither description names a hook; no response carries a key name or any lab word. The response-side sweep (`tests/api/test_read_tools_never_name_the_lab.py`) now covers `get_control_loops`, and `tests/api/test_mcp_resolver_and_control_loops.py` arms the whole `resolver_stall` world — both keys and both `chaos.tool_invoked` rows — reads it with the agent's token, and checks the evaluator still reads the rows.

**Visible, on purpose, with a cost recorded rather than hidden.** `paused: true` on a control loop is a state with no actor, but today it is a state **only the lab produces**: there is no operator pause for a loop, and the check is off outside `CHAOS_ENABLED`. Compare [ADR 0029](0029-stranded-chain-and-lab-pause-are-manufactured.md), which made a lab DAG pause read exactly like an operator's — possible there because `pause_dag` exists, impossible here. So a principal that reads `paused: true` learns a loop is deliberately held, and its expiry tells it the stranded world heals on its own when the pause lifts. The alternative was `get_outbox_status`'s posture — report only the age, so a deliberate stop reads like a dead process — and it was **not** taken because the order asks for the pause by name, and because an age alone cannot tell "held for seven more minutes" from "wedged", which call for different responses. It is reversible: dropping `paused` and its expiry is a field removal and a re-pin. Two knock-on effects belong to the scenarios, not to this repo: the `jobs_not_progressing_outbox_stall` world (`pause_control_loop('outbox_relay')`) now has a direct reading where it had an inference, and any scenario whose correct answer is "escalate" while a pause's expiry is visible should be re-read with that in mind.

## Rejected

- **Making the killed resolver read `lag_known: false`** — by skipping the measurement while the consumer is not polling. That is what the commander's WO-R3-372 guessed the reading would be ("lag_known false / consumers 0"). It would break the dispatcher's established contract — "a stalled consumer still reports lag" is what the demo and the `consumer_stalled` page stand on — or give the two live groups opposite rules. The poll time says it without moving either.
- **A consumer count.** Kafka membership is not visible without an admin client ([ADR 0040](0040-the-lag-clock-survives-a-restart.md) rejected a second connection per process for the same reason), and a stopped consumer stays a member for minutes anyway — a count of 1 would have read healthy.
- **The consumer writing its own poll time to Redis.** It would put a Redis write in every consumer's poll loop in production, and a slow Redis would slow every consumer; publishing from the pass costs one write per group per pass.
- **A platform-computed `consuming: true/false`.** A verdict needs a threshold (an injected latency of up to 60 s per poll would flip it), and the description can state the comparison instead. Times are facts; the verdict is the reader's. — **Reversed by the 2026-10-10 amendment below (WO-R3-373):** the first paid run on this release made the read and misread the comparison.
- **Paging on the resolver** — §1.
- **`paused` without the `CHAOS_ENABLED` gate.** The loop ignores the key when the flag is off; a reading that reported it anyway would claim a pause the loop is not honouring.

## Consequences

- **Contract delta, so a release, a re-pin and a rebless.** `+get_control_loops`: 40 → 41 tools with `CHAOS_ENABLED=true` (still 15 chaos), 25 → 26 without, read tier 16 → 17. `get_consumer_lag` changes in four places and nowhere else: its `description` (3,728 → 4,852 characters — the known-groups list gains `dependency-resolver`, the live paragraph names both groups and what each consumes, a new IS ANYTHING CONSUMING paragraph, and the closing liveness sentence points at `last_poll_at` instead of "consumer-group membership", which no tool could check), the `consumer_group` input field's description, two new output properties (`last_poll_at`, `last_poll_age_seconds`), and the `recent_samples` field description ("the" → "a" continuously-refreshed group). Its `required` list and `$defs` are byte-identical, and the other 39 entries are byte-identical. `tools/list` sha256 over `json.dumps(tools, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` with `CHAOS_ENABLED=true`: `7e3c8dec…` → `5382e7c6…`.
- **The REST twin moves too:** `GET /admin/consumer-lag` lists nine groups, the resolver among them with `source: live`. Its response model does not gain the poll fields yet — the console does not need them for the demo, and adding them is a separate, additive change.
- **New Redis keys,** all outside `chaos:*` and none swept by the reset, because they describe the platform: `kafka:consumer_lag:dependency-resolver` (+ `:samples`), `kafka:consumer_lag:<group>:last_poll` for the two measured groups, `worker:loop:<loop>:last_run` for ten loops. About four extra writes a second, almost all from the two 0.5 s promoters. Catalogued in `docs/REDIS.md`.
- **No migration, no new scope, no new refusal code, no new `BlastRadius` member,** and `get_outbox_status` is unchanged.
- **Not built, recorded:** `saga-coordinator` has no reading. With this order the agent can positively identify a dead resolver, and only by elimination a stopped coordinator; measuring a third group is one more consumer handed to the pass and one more tuple member. `restart_consumer_group`'s description still tells the caller to "confirm via group membership", which now has a better answer in `last_poll_at`; changing it is a contract delta of its own.

## Amendment — 2026-10-10: the platform states the verdict, `polling` (WO-R3-373, INC-008 addendum, owner decision O-51)

§2 and the rejection of a platform-computed verdict stand above as the record of what shipped in v0.6.24 and are not rewritten. What changed on 2026-10-10 is that the comparison §2 left to the reader went wrong the first time a reader made it.

**What happened.** The next paid run of the `resolver_stall` world made the read this ADR exists for and got `lag 0, lag_known true, source live, age_seconds 3, last_poll_age_seconds 39` — a consumer that had stopped asking 36 seconds before the platform looked, i.e. dead. The planner wrote "the dependency-resolver is polling (lag=0, last_poll_age=39s) … somewhat elevated but not conclusive", then anchored on `get_control_loops`' `paused: true` with its 851-second expiry and escalated without restarting anything. The description's rule — "never more than a few seconds above `age_seconds`" — asked the reader to threshold one number against another with no stated cadence. A number the reader must threshold lost to a boolean with a clock. The rejection above traded that risk for the purity of "times are facts"; the trade was wrong for a reading whose whole purpose is one yes-or-no question.

### Decision

**1. Two output fields on `get_consumer_lag`.** `polling` (`bool | null`) and `poll_interval_seconds` (`float | null`), both optional with a null default:

| Group | `poll_interval_seconds` | `polling` |
|---|---|---|
| `worker-dispatcher`, `dependency-resolver`, poll on record | `2.0` | `true` / `false` |
| the same, nothing on record (never polled, or the pass stopped for three passes) | `2.0` | `null` |
| the seven recorded constants, an unrecognised name | `null` | `null` |

Null is unknown, never false — the rule every field in this reading already keeps.

**2. The cadence is the loop's own, and the loop reads it from the same place.** Both measured groups run `BaseKafkaConsumer.run()` unmodified (pinned: `JobDispatcherConsumer.run is BaseKafkaConsumer.run`, likewise the resolver). Its three timings were literals in the loop; they now live in `app/core/consumer_lag.py` and the loop imports them, so the verdict and the loop cannot drift: `POLL_FETCH_WAIT_SECONDS = 0.5` (one fetch waits this long when nothing is waiting), `POLL_FETCH_BOUND_SECONDS = 2.0` (a fetch is cut off here however Kafka behaves), `POLL_RETRY_PAUSE_SECONDS = 1.0` (after a failed batch or a loop error). `poll_interval_seconds` is the fetch bound — the longest a running consumer goes between asks on the fetch alone. It is a property of the platform's consumer, not of a deployment, so the description and the commander's snapshot are the same bytes on every stack (the constraint [ADR 0039](0039-the-platform-pages-on-its-own-metric.md) put on the pass interval).

**3. N = 5 intervals, so the line is 10 seconds — chosen from the loop's own jitter.**

| A running consumer's gap between asks | Seconds |
|---|---|
| idle (nothing to read) | ~0.5 |
| messages waiting | ~0, plus handling the batch of ten |
| a fetch Kafka does not answer | 2.0 (the bound) |
| a failed batch or a loop error | + 1.0 |
| worst healthy gap | ≈ 3, plus handling one batch |

Ten seconds is more than three times the worst healthy gap, so a running group does not cross the line on jitter — `tests/unit/test_consumer_polling_verdict.py` looks at a running consumer 2,000 times at arbitrary moments, at the loop's own jittered cadence and at its worst case on every gap, and the shipped loop through a failed batch, and never reads false. A stopped consumer crosses it 10 seconds after its last poll, and the reading says so at the next pass — within 15 seconds on the demo stack's 5-second pass, within 70 at the 60-second default. The INC-008 reading was 36 seconds past.

**4. Decided at the look, not at the read — in one function.** The metrics pass now records, beside the poll time, the moment it read it: `kafka:consumer_lag:<group>:last_poll` becomes one JSON record `{"last_poll_at", "observed_at"}` (same key, same three-pass TTL), both stamped by the worker's own clock. `polling_verdict` in `app/core/consumer_lag.py` decides `observed_at − last_poll_at ≤ 5 × poll_interval_seconds`; `read_lag` calls it; the MCP tool and the operator's REST reading both build from `read_lag`. Not against the reader's clock, for three reasons: at the platform's default 60-second pass a healthy consumer's `last_poll_age_seconds` reaches a minute between passes, so a read-time verdict would flap it to false; a stopped pass would make every live consumer read dead — the exact failure §2's fresh-or-absent rule exists to prevent (with the verdict taken at the look, a stopped pass reads the last look's verdict and then, three passes later, unknown); and the gap never crosses a process boundary, so no clock skew enters it. The cost, stated: a verdict is up to one pass old when read, like every other field here ("a reading can trail a change by up to one interval"). `last_poll_age_seconds` stays as the number behind it — on a reading taken right after a pass it is the gap plus `age_seconds`.

**5. The record's old shape reads as a fact without a verdict.** A bare ISO time — what a v0.6.24 worker writes, for at most three passes after an upgrade — still yields `last_poll_at`, with `polling: null`, never a guessed look time.

**6. The description says it in one sentence, and is shorter.** The IS ANYTHING CONSUMING paragraph, the comparison rule and the closing liveness sentence become one IS IT CONSUMING paragraph whose first sentence is: *"Read `polling`: a dead consumer reads `polling: false` while `lag`, `lag_known`, `source` and `age_seconds` can all still look healthy (a lag of 0 included), and `last_poll_age_seconds` is the number behind that verdict."* The rest of the description is tightened without dropping a rule: 4,852 → 3,093 characters.

**7. The REST twin catches up.** `GET /admin/consumer-lag` gains `last_poll_at`, `last_poll_age_seconds`, `polling` and `poll_interval_seconds` per group — the "not yet" in Consequences above, closed.

### Boundaries, recorded rather than discovered

- **An injected per-poll delay above 10 seconds reads `false` between polls** — the case the rejection above named. It is the honest answer (the consumer is not asking), it is a lab-only state, and no scenario uses it as its fault: the commander's `remediate_consumer_lag_success` records why `inject_latency` was abandoned (a delayed consumer still serves the per-principal arrival rate, so no visible lag forms). A future scenario that delays a consumer must expect `polling` to flicker and grade accordingly.
- **A handler that blocks longer than 10 seconds reads `false` while it blocks** — for instance the resolver waiting on a held database pool, up to the pool's 30-second checkout timeout. Also honest: it is not asking for messages. The held-pool world grades on the pool readings ([ADR 0033](0033-each-process-publishes-its-own-pool-gauge.md)), not on this.
- **While the supervisor restarts a consumer it is not polling**, so `polling` reads false until the first poll after the restart and true at the next look — which is what a scenario that verifies `restart_consumer_group` by `polling: true` should wait for.
- **Not paged, not a new rule.** `consumer_stalled` is unchanged. `restart_consumer_group`'s description still says to "confirm via group membership"; `polling` is now the better answer, and changing that text is a contract delta of its own, not taken here.

### Consequences of the amendment

- **Contract delta, one entry:** `get_consumer_lag` — `description` 4,852 → 3,093 characters; `outputSchema` gains `polling` and `poll_interval_seconds` (optional, default null — a commander model with `extra="forbid"`/`"ignore"` needs to know); the `last_poll_at` and `last_poll_age_seconds` field descriptions are rewritten to point at the verdict. `inputSchema`, `required` and `$defs` are byte-identical; the other 40 entries are byte-identical; 41 tools. `tools/list` sha256 (same canonicalisation as above, `CHAOS_ENABLED=true`): `5382e7c6…` → `2237c269…`. Release, re-pin, rebless; the canned readings of `get_consumer_lag` gain the two fields at the re-pin.
- **One Redis value shape change, no new key:** `kafka:consumer_lag:<group>:last_poll` is a JSON record now (`docs/REDIS.md`). No migration, no new scope, no new refusal code, no new `BlastRadius` member, and the reset is untouched (the key is outside `chaos:*`, as before).
- Tests: `backend/tests/unit/test_consumer_polling_verdict.py` (the verdict, the cadence, the jitter, a kill, a kill cleared and the supervisor's restart, the nulls, the description), wire cases in `backend/tests/api/test_mcp_resolver_and_control_loops.py` and `backend/tests/api/test_demo_operator_reads.py`.

## Pointers

- `backend/app/core/consumer_lag.py` (`LIVE_REFRESHED_GROUPS`, `PAGED_LAG_GROUPS`, `record_last_poll`, `read_lag`; since the amendment `POLL_*`, `POLLING_INTERVALS`, `polling_verdict`, `parse_last_poll`), `backend/app/core/loop_heartbeat.py`
- `backend/app/workers/kafka_consumer.py` (`last_poll_at`, `consumer_lag`), `backend/app/workers/dispatcher.py` (`_metrics_loop`, each loop's `record_loop_run`), `backend/app/workers/control_loop_pause.py` (`loop_pause_state`)
- `backend/app/mcp/tools/consumer_lag.py`, `backend/app/mcp/tools/control_loops.py`, `backend/app/services/alert_rules.py` (`_measured_groups`)
- Tests: `backend/tests/unit/test_consumer_polling_verdict.py` (amendment), `backend/tests/unit/test_resolver_and_loops_observable.py`, `backend/tests/api/test_mcp_resolver_and_control_loops.py`, and the read-tier pins updated in `test_outbox_status.py`, `test_phase8_read_tool_shape_deltas.py`, `test_pool_gauge_shape_delta.py`, `test_run_record_shape_delta.py`, `test_agent_run_contract.py`
