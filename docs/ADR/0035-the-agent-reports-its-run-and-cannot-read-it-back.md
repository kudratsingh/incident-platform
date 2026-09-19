# ADR 0035 — The agent reports its run to the platform, and the platform never shows the agent what it reported
*Status: Accepted · 2026-09-19 · WO-R3-312 (the live demo, platform half)*

## Context

The owner wants to record a demo: a fault appears, an autonomous responder investigates
it, acts, verifies, and either resolves the incident or escalates it — with a human
watching the whole thing on a screen rather than reading a transcript afterwards. Two of
the four things that screen has to show did not exist anywhere the screen could reach.

**What the responder is doing right now.** It knows: it holds a state machine and
checkpoints it after every transition. But it holds it *in its own process*. The only
trace of it that reaches this platform is one `agent.tool_invoked` audit row per call,
which says which tool ran and with what arguments — not what the responder thought it
was doing, not what it believed the cause was, and not how sure it was.

**What it concluded.** Its end-of-run write-up renders inside its own repository when the
run finishes. Nothing on the platform side has ever seen one.

Three ways to close that gap were on the table.

1. **The console reads the responder.** It exposes `GET /health` and `POST /alerts` and
   nothing else; during a run it writes an append-only JSONL trace on the machine that
   launched it. Making the console read that means the console reaching into the
   responder's filesystem or the responder growing an HTTP read surface, and either way
   the console now depends on a process whose whole point is that it can crash without
   taking the platform with it.
2. **The console infers it from the audit log.** Possible, and worth having as a
   fallback — a tool call is evidence of a phase. But it is inference: `get_consumer_lag`
   is read three times both while investigating and while verifying, and no audit row
   distinguishes those. An inferred phase strip would be wrong in exactly the moments a
   demo is about.
3. **The responder reports, the platform stores.** The responder already speaks MCP to
   this platform on a token it already holds. One more write and the fact stops being an
   inference.

Option 3 is the decision. What it costs is the subject of the rest of this record,
because a write surface for the agent under test is not a free thing to add.

## Decision

**The responder reports its own run over MCP; the platform stores the report, shows it to
human operators, and offers the responder nothing to read it back with.**

### The table

`agent_runs`, tenant-scoped under the strict `tenant_isolation` policy and FORCE RLS
([ADR 0015](0015-force-rls-and-nonowner-app-role.md),
[ADR 0026](0026-strict-tenant-isolation-and-declared-platform-scope.md)), keyed by the
**caller's own run id** so a repeat report lands on the row it already knows the key of.
Columns and the reasoning for each are in
[`docs/DATA_MODEL.md`](../DATA_MODEL.md#agent_runs). Four rules govern writes, and each
exists because a console reads the table live:

1. **Upsert by run id.** The same state reported twice leaves the row as it was, so a
   caller retrying a timed-out report cannot double-count.
2. **`phase_history` is append-only, and appends on a state *change*.** One entry per
   transition, oldest first. A revisited state appends again — going back to
   investigating after a failed plan is a real transition — but a repeat of the current
   state adds nothing, or a caller reporting every loop iteration would turn a timeline
   into a call log.
3. **A terminal state closes the run**, and a later report on a closed run is refused
   (409 `agent_run_already_finished`) rather than allowed to rewind a strip an operator
   has already read to its ending.
4. **The briefing lands once** (409 `agent_run_briefing_already_recorded`). Not merged,
   not overwritten: an operator may be reading it.

**The state vocabulary is the responder's own, character for character** — `triage`,
`investigating`, `planning`, `awaiting_approval`, `remediating`, `verifying`, `resolved`,
`escalated`, `failed`. Nine values, closed on the wire, and deliberately not a vocabulary
of the platform's own invention: a mapping layer between two enums is a place for a state
to be lost, and the state the console draws has to be the state the responder is in. The
platform's own three terminal members are a *property* of that list, not a re-grouping of
it. `awaiting_approval` is emphatically not terminal — a run parked on an approval is
still open, and closing it would make the console stop watching the thing an operator is
most likely to be looking at.

Nothing in the platform interprets any of it. `state`, `current_hypothesis`, `last_step`
and `briefing` are stored as the caller sent them, and `briefing` is an unvalidated JSON
object on purpose — pinning its shape would couple two repositories' release cycles to
buy nothing, since the platform only stores and displays it.

### The write surface: two MCP tools, one new scope, and a prefix

`report_agent_run` and `report_agent_briefing`, both under a **new sixth scope**
`agent_runs:write` ([ADR 0007](0007-machine-principal-scope-model.md)'s enum grows for
the first time since Step 0), both carrying a `[commander: telemetry]` description
prefix. Never REST from the responder: it speaks MCP to this platform and a second
transport would mean a second auth path.

The prefix is the load-bearing part, and it sits beside `[chaos: <blast_radius>]` for
the same reason that one exists. **No model chooses these calls.** The responder's loop
makes them at a fixed point in its cycle, and its planner drops `[commander:` tools from
the tool list it offers its model exactly as it drops `[chaos:` ones. So the model never
sees them, never spends a budgeted call on one, and never reasons about one. The
platform's half of that contract is that the prefix is stable, that the flag and the
prefix always agree, and that no read-scoped tool ever carries it — all pinned by test
(`backend/tests/unit/test_agent_run_contract.py`).

### The read surface: operators only, over REST

`GET /api/v1/admin/agent-runs`, `…/agent-runs/{id}`, and — because the demo needs them
and they were MCP-only — `…/consumer-lag`, `…/circuit-breakers`, `…/alerts`, plus five
additive nullable fields on the job shape so a console does not have to read the agent's
surface to see what the agent saw. All `support|admin`, all tenant-scoped, none reachable
by a machine principal's token.

Two of those readings are the same numbers the agent's tools return, and the arithmetic
is now **shared rather than duplicated**: `app/core/consumer_lag.py` was extracted from
the MCP tool so both surfaces build their answer from one reading. `app.api` may not
import `app.mcp` ([ADR 0006](0006-mcp-server-standalone-process.md)'s import contract),
and a second copy of "is this lag known?" is a console and an agent that can disagree
about it. The tool's wire shape is unchanged; only where the function lives moved.

### Why there is no read tool, and why that took more than not writing one

"No read tool for `agent_runs`" is easy. The hard part is that **the audit log is a read
surface for `agent_runs` under another name.** Every MCP call writes an audit row
carrying its arguments, and `list_audit_events` is on the responder's own read surface —
so without a further decision, a responder could have read back every state, hypothesis
and confidence it had reported, by asking for its own `agent.tool_invoked` rows.

Two changes close it.

- These calls audit as **`agent.run_reported`**, a stream of their own. It sits under
  `agent.` because a service account really did make the call, but it is deliberately not
  `agent.tool_invoked`: that stream is what the responder did *to* the platform, and an
  operator timeline that filed a status report under it would colour a report as an
  action. The row shape is identical to every other tool row, which is what keeps the
  phase strip rebuildable from the audit log alone if the table is ever lost — the run id
  and the state ride in `extra_data.arguments`.
- `hidden_audit_action_prefixes` gains a second rule, and it points the **opposite way**
  from the first. The `chaos.` stream is shown *only* to a principal holding
  `chaos:invoke` ([ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)). The
  `agent.run_reported` stream is hidden *from* a principal holding `agent_runs:write`:
  **the writer of a stream is not its reader.** The exclusion lands in SQL, so `total`
  counts only readable rows, and asking for the withheld stream is an empty page rather
  than an error — a refusal would confirm what is being withheld, which is the same
  reasoning WO-R3-187 applied to the chaos stream.

Routing the row in the envelope rather than writing a second row from the tool is also
what gives the report the R2-51 guarantee: **a report that cannot be audited does not
commit.** A row written by the tool itself would be savepoint-swallowed on failure,
leaving a stored report with no record of it.

### One wire name is not the column name, and that is deliberate

The column that holds the run's human-readable name is `agent_runs.scenario`, because
that is the word an operator reading the console uses. The **input field is
`run_label`**, because ADR 0012's registry screen bans the lab's own vocabulary from any
non-chaos tool's `tools/list` surface — and the word for a named rehearsal is in that
vocabulary. The screen's own rule for a legitimate wording that trips it is to reword the
description, never to weaken the pattern, and a new exemption for this family was
declined outright: the responder's principal *does* hold `agent_runs:write`, so it can
call `tools/list` and see these descriptions. Whether its model sees them depends on a
filter in the other repository, and the platform does not rely on that filter for a
property it can hold on its own. The mapping is pinned by test so nobody "fixes" it back.

## Consequences

**The agent under test now has a write surface it did not have.** That is the real cost,
and it is bounded three ways: the scope grants no read anywhere, the tools change nothing
about the incident, and their descriptions say so in the plainest words available — "this
is not a tool to choose", "calling it is not progress", "a refusal here changes nothing
about what you have already done". The last one matters because reporting is fail-open on
the caller's side: a report that fails must not change a run, and a description that left
that ambiguous would invite the model to treat a 409 as a problem to solve.

**ADR 0012 is untouched, in both directions.** Nothing here tells the responder anything
about the apparatus: the descriptions carry no lab vocabulary (the registry screen passes
with no new exemption), and there is nothing to read. And the withholding above means the
platform does not hand the responder its own reported state back as though it were an
observation of the world.

**The phase strip has two sources and they may disagree.** The run record is the
responder's word; the audit log and the metrics are the platform's. The console shows
both rather than merging them, which is the owner's decision C, and this table is what
makes the first of the two exist at all. A console that had only the inferred strip would
be confidently wrong at the moments a demo is about; one that had only the reported strip
would go blank whenever the responder crashed.

**`agent_runs` is not swept by the eval reset.** Deliberately out of scope here and filed
as a follow-up rather than added quietly: rows accumulate across runs, and the console's
"active run" query is `finished_at IS NULL` newest-first, so a stale finished run is not
mistaken for the current one. What it means today is that the table grows, and that a run
the reset ends is left open forever unless the responder reported a terminal state.

**Six scopes, not five.** [ADR 0007](0007-machine-principal-scope-model.md) says five and
is history, not current state; this record is the amendment. `agent_runs:write` is
grantable through the admin API, unlike `chaos:invoke` — it is not the lab, and an
operator provisioning a responder should be able to grant it. `scripts/seed_incident_commander.py`
adds it to the agent account's defaults; the responder's own bootstrap script mirrors that
change in the other repository.

**What this does not do.** It does not let the platform know whether the responder is
alive: a run with no report for ten minutes is indistinguishable from one whose reporter
failed silently, because the reporter is fail-open by design and there is no heartbeat.
The console shows the wall clock since the last report and lets the operator draw the
conclusion. Adding a liveness claim would mean the platform asserting something it cannot
observe, which is the mistake [ADR 0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md)
spent half its length refusing to make.
