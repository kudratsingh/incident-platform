# ADR 0029 — A stranded chain and a lab pause are manufactured, not found
*Status: Accepted · 2026-09-18 · WO-R3-274 + WO-R3-275 (plan v2.1 WP-7.2, Family C platform half)*

## Context

Plan 01 §7.2's `workflow_stuck` family needs five worlds that differ by one fact
each, so that a grader can tell "the agent read the world" from "the agent
guessed the family". Three of them could not be built on v0.6.9, and the WP-7.2
builder found it with zero-LLM live evidence rather than by reading code:

| World | Wanted | Why it was unreachable |
|---|---|---|
| `resolver_stall` | root `completed`, child `waiting`, **no** DLQ row | `create_stuck_dag` always dead-letters the root |
| `downstream_child_failed` | root `completed`, a descendant dead-lettered | same gap |
| `paused_dag` | `get_dag_state` reads `paused: true` | `pause_dag` needs `actions:execute`, which the evaluator principal deliberately lacks |

The obvious fixture for the first two looked free: the boot seed writes a
three-node DAG — `dag-parent-job` `completed` → `dag-seed-job` `waiting` →
`dag-child-job` `waiting`. It is not a fixture. Its parent is `completed`, so
the dependency resolver (or the resume sweep, within ~10 s) promotes both
children on the first boot and they run to `completed`. Nothing restores them:
`make eval-reset` re-anchors the trio's **timestamps** from `_dag_specs()` and
never touches statuses, and because those specs carry `run_seconds: None` the
re-anchor writes NULL `started_at`/`completed_at` onto rows that are by then
`completed`. The seeded DAG is therefore a *drained* DAG with a slightly
incoherent lifecycle, and it has been for the life of the project.

Two live readings made the shape of the problem concrete. With both Family C
stalls armed, `search_traces(status="waiting")` was empty **environment-wide** —
there is no waiting row anywhere on a warm stack to borrow. And `ChaosHook`
refuses any tool name outside the chaos surface, so the evaluator cannot reach
`pause_dag` even if the token were widened.

## Decision

### 1. The stranded chain is manufactured by the hook that already manufactures chains

`create_stuck_dag` gains three inputs. The default is byte-for-byte the chain it
has always written, because four scenarios and the commander's canned fixtures
are graded against it.

* `root_status: "dead_letter" | "completed"` — `completed` writes upstream
  `completed` → root `completed` → step-1..N `waiting`, with **no dead-letter
  row anywhere in the chain**. That absence is the `resolver_stall`
  discriminator: there is nothing to replay, so escalating is the correct move.
* `failed_step: int | None` (only with `root_status="completed"`) — descendant N
  is `dead_letter`, the descendants **before** it are `completed`, and the ones
  after it are `waiting`. That is `downstream_child_failed`.
* `child_age_seconds: int` (0..86 400) — backdates `created_at`/`updated_at` so
  "child `created_at` age large" is true on the first read rather than after a
  wait. The platform's own stranded-child proof uses 47 minutes.

**Rejected: restoring the seeded trio in the reset.** It is the tempting fix and
it is racy. The reset clears `chaos:*` *first*, and the resume sweep ticks every
10 s, so a row restored to `WAITING` behind a `COMPLETED` parent is a row the
sweep is about to promote again — the reset would be fighting a correctness
backstop, with the outcome depending on where in the 10 s window the reset
landed. The seeded trio is documented as drained instead (decision 4).

**Rejected: `failed_step` leaving the descendants ahead of the failed one
`waiting`.** The order text reads "descendant N is dead-lettered, the rest
wait", which is exactly right for N=1 and incoherent for N≥2: a job cannot have
been dispatched, failed and exhausted its retries while its own parent is still
`waiting`. The platform cannot produce that row and no agent should be asked to
reason about it, so the descendants before the failed one are `completed`. For
N=1 the two readings coincide.

### 2. The completed-root chain does not hold by itself, and the tool says so

This is the part that is easy to get wrong. With the root `completed`, step-1 has
no unmet parent — so the `dependency-resolver` consumer group promotes it on the
next `job.completed`, and `_resume_unblocked_waiting_loop` promotes it within
about ten seconds regardless. The stranded world is the composition WO-R3-213
proved: **this hook plus `kill_consumer('dependency-resolver')` plus
`pause_control_loop('resume_unblocked_waiting')`**, and only the pause's TTL
heals it, because the parent's `job.completed` is already consumed.

A description that called this chain "stuck" without saying that would be the
platform's fourth description rule failing again (never advertise a safety
property the tool cannot deliver — the `poison_message` `replay_safe` defect,
WO-R2-166). So the tool description names both companion hooks, and says which
of the three shapes holds on its own: the dead-lettered default does,
`failed_step` does (no `waiting` row in it has a `completed` parent), and the
bare `completed` chain does not.

### 3. The lab pause is the operator's pause, written by a different principal

`pause_dag_chaos(root_job_id, ttl_seconds)` sets
`app/utils/dag_pause.pause_key_for(root)` → `dag:paused:<root_id>` to the string
`"paused"` with a TTL. Not a copy of that format — the shipped helper is
imported, so a rename cannot leave the lab writing a key the resolver and
`get_dag_state` no longer read. The TTL default and bounds are `pause_dag`'s own
(600 s, 1..3600), so a lab pause taken with default arguments is the same pause
an operator would have taken. Existence and tenancy are checked identically,
with the same `NotFoundError`.

`get_dag_state` answers `paused` from the key's presence and
`paused_expires_in_seconds` from its TTL, and derives `paused_by` from which
ancestor carries a key. None of those reads the value, and no field is added for
an owner — so there is nowhere for a lab marker to sit, which is ADR 0012 rule 1
as amended to cover response bodies. The value must stay `"paused"` for a
second reason: an operator with `redis-cli` is a reader too, and `"chaos"` in
that slot would put the lab in front of a human mid-run.

**The key is deliberately outside `chaos:*`.** Every other chaos hook keeps its
keys there and `test_eval_reset.py::test_every_chaos_key_helper_lives_under_the_chaos_namespace`
holds them there, because `_clear_chaos_keys` is one `chaos:*` SCAN. This hook
cannot, because a `chaos:…` key would not be the key the platform reads.
Teardown is the reset step that already existed for operator residue —
`_clear_dag_pauses`, one `dag:paused:*` SCAN, reported as `dag_pauses_cleared` —
plus the TTL.

**Blast radius `environment_wide`, and the enum is not widened.** One DAG is
narrower than any member of that closed five-member set; `single_consumer`,
`single_loop`, `single_service` and `shared_dependency` all name objects this
hook does not touch. `environment_wide` is what every hook that writes state
into the shared world carries, including this one's sibling `create_stuck_dag`
on the same chain. The enum moved once, for `pause_control_loop` (ADR 0027), and
a second widening for one more hook would make the label a per-hook taxonomy
rather than a coarse warning.

### 4. The seeded trio's spec is made honest, not restored

`scripts/reset_eval_state.py`'s docstring now says, in the step that lists what
the reset does and does not restore, that `dag-seed-job`/`dag-child-job` drain on
first boot, that the reset re-stamps their timestamps without restoring their
statuses (so they end as `completed` rows whose spec says NULL dispatch times),
why restoring the statuses would race the resume sweep, and that a stranded
chain comes from `create_stuck_dag(root_status="completed")` instead. A unit test
pins both halves — the paragraph, and the seed still declaring those two rows
`waiting` with no dispatch times, so the paragraph cannot quietly become false.

The incoherent lifecycle itself (a `completed` row with NULL `started_at`) is
left alone: repairing it means teaching `_rebaseline_timestamps` to skip rows it
does not reset, which is in `scripts/seed_eval_fixtures.py` — outside this
order's file ownership — and no tool output reads those two columns for these
rows today.

## What the agent can and cannot see

The whole point of both mechanisms is that the fault is real, so this list is the
contract:

**Can see** — and these are the reads a Family C scenario is graded on:
* `get_dag_state(job_id)` — node statuses, edges, `paused`,
  `paused_expires_in_seconds`, `paused_by`. Identical whether the pause came
  from `pause_dag` or from `pause_dag_chaos`.
* `search_traces(status="waiting")` — the stranded descendants, with the
  backdated `created_at`.
* `list_dlq_messages` — nothing of a `root_status="completed"` chain; exactly one
  row of a `failed_step` chain.
* `get_trace` / `list_incidents` / the rest of the read surface, unchanged.

**Cannot see:**
* Any `chaos.` audit row. `list_audit_events` and `get_trace` exclude the prefix
  in SQL and out of `total` for a principal without `chaos:invoke` (WO-R3-187).
* Any `chaos:*` Redis key — no read tool exposes Redis keys by name.
* The hook names and their arguments, except through `tools/list`, which is not
  principal-scoped (ADR 0016, divergence G4) and has advertised the chaos
  surface since Step 0.

**One asymmetry, recorded rather than hidden.** An operator pause writes an
`agent.tool_invoked` audit row; a lab pause writes `chaos.tool_invoked`, which is
withheld. So the agent sees *no* audit row where it would have seen one. That is
an absence, not lab vocabulary — it leaks no name and no argument — and it is the
same property every chaos hook has had since the token split. Closing it would
mean either writing a forged `agent.tool_invoked` row (a lie in the ground-truth
stream, which ADR 0012's own amendment forbids) or withholding operator rows too
(which would break the audit surface for every other scenario). Neither is worth
it: no scenario in this family grades on the presence of an audit row for the
pause, and a scenario that wanted to would be grading the lab rather than the
agent.

## Consequences

* **Tool surface delta, so a release and a re-pin.** `+pause_dag_chaos`: 32 → 33
  tools with `CHAOS_ENABLED=true`, 12 of them chaos. `create_stuck_dag`'s input
  gains `root_status`, `child_age_seconds`, `failed_step` and its output gains
  `step_job_ids`, `dead_letter_job_id`; its description is rewritten. Pinned by
  `backend/tests/unit/test_stranded_chain_and_lab_pause.py::test_the_shape_deltas_are_exactly_these`.
* **A behaviour change with no new field:** a chain manufactured by this hook can
  now exist with no dead-letter row at all. Anything that assumed
  `create_stuck_dag` implies a DLQ row — a grader, a world audit, a precondition
  — has to read `dead_letter_job_id` or the shape it asked for.
* **`waiting_job_ids` changed meaning, narrowly.** It now lists only the
  descendants actually in `waiting`; `step_job_ids` is what it used to be. Under
  the default they are equal, so no existing caller moves.
* **No new refusal code.** The stranded chain reuses `stuck_chain_name_in_use`,
  the two input validators refuse as JSON-RPC invalid params, and the lab pause
  reuses `not_found`. The commander's ChaosClient, which buckets unknown codes as
  transport faults (R2-16), needs no change.
* **A repeat call with a different shape under an existing `chain_name` is
  refused,** because the ids do not depend on the shape. That is deliberate:
  rewriting a chain's shape under its own name would silently change a world a
  scenario already pinned.
* The lab now has two ways to make a `WAITING` child look stuck and they mean
  opposite things — the composed stall (nothing is coming) and the pause
  (deliberately held). That is the paired comparison the plan asked for, and it
  is also a new way for a scenario to be written wrong: a scenario that arms both
  is not a harder version of either, it is a world with no correct answer.

## Pointers

* `backend/app/mcp/tools/chaos/create_stuck_dag.py`,
  `backend/app/mcp/tools/chaos/pause_dag_chaos.py`
* `backend/app/utils/dag_pause.py` (the key helper both pauses share),
  `backend/app/mcp/tools/actions/pause_dag.py`,
  `backend/app/mcp/tools/dag_state.py`
* `scripts/reset_eval_state.py` (`_clear_dag_pauses`,
  `_delete_seeded_dlq_fixtures`), `scripts/seed_eval_fixtures.py` (`_dag_specs`)
* Tests: `backend/tests/unit/test_stranded_chain_and_lab_pause.py`,
  `backend/tests/api/test_mcp_stranded_chain_and_lab_pause.py`,
  `backend/tests/integration/test_stranded_chain.py`
* [ADR 0011](0011-dag-pause-enforcement.md) (the pause is enforced, not just
  recorded), [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md),
  [ADR 0016](0016-defer-principal-scoped-tools-list.md),
  [ADR 0022](0022-promotable-only-resume-sweep-and-dependency-cascade.md),
  [ADR 0027](0027-control-loop-pause-closed-enum.md)
