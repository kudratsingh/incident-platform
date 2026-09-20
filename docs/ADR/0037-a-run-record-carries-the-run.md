# ADR 0037 — A run record carries the run: excerpts of the reasoning, not a copy of the trace
*Status: Accepted · 2026-09-20 · WO-R3-328 (the live demo, second take, platform half)*

## Context

[ADR 0035](0035-the-agent-reports-its-run-and-cannot-read-it-back.md) gave the console
one fact it could not get any other way: where an autonomous responder says it is. The
first live take of the demo proved that one fact is not a story.

What the screen showed while a real run was in flight: a phase strip, and an agent panel
that was empty. `report_agent_run` was carrying `current_hypothesis: null` and
`last_step: null` on every transition, because the reporter only filled them at the end —
but the deeper problem was that even filled in, those two fields are all the platform had
room for. Nothing about the *ranked* explanations, the plan, or whether a verify poll
agreed reached the platform at all. The audit log held the calls (`agent.tool_invoked`
carries tool, arguments, latency and outcome) and deliberately holds no result, so "what
did the responder see" could not be shown either. A viewer watching the recording could
see that something was happening and could not see what was being decided.

Two more findings from the same take belong here because they have the same shape — a
number the console had to reconstruct because the platform would not carry it:

- `get_consumer_lag.recent_samples` held five measurements, ~5 minutes at one per pass,
  under a 90-second TTL. The console stitched a longer window together client-side, which
  meant the chart restarted on every page reload and ended at whatever the browser had
  happened to see.
- `GET /api/v1/audit/logs` took one `action_prefix` and had no exclusion, so the timeline
  the console could ask for was 43 of 50 rows of job lifecycle (`event.job.completed`,
  written by the traffic loop) with the three rows the demo was about buried in it.

Three ways to give the console the reasoning were on the table.

1. **The console reads the responder's own trace.** Rejected for the reason
   [ADR 0035](0035-the-agent-reports-its-run-and-cannot-read-it-back.md) rejected it: the
   trace is a file on the machine that launched the run, and a console that reads it
   depends on a process whose whole point is that it may die without consequence.
2. **Store the trace.** Send each tool result whole and let the console render it. This
   is the tempting one, and it is wrong in two ways. It makes `agent_runs` a second copy
   of an append-only artefact that already exists and is already archived, growing one
   row without bound during a run; and it puts full tool output — job payloads, error
   bodies, DLQ contents — into a table whose purpose is a screen, where nobody chose what
   a person would read.
3. **Store excerpts of the responder's own words, bounded, beside the state.** The
   responder already truncates for its own prompts and already holds every number as a
   typed object.

Option 3 is the decision.

## Decision

**The run record carries the run: the ranked hypotheses, the plan, every verify verdict,
an append-only step ledger and the budget — as short excerpts the caller truncates, under
bounds the platform enforces, all of it optional and additive.**

### Seven columns, in two shapes, and the shape is the decision

Latest-reading columns hold the newest thing the caller said; append-only columns hold
everything it said, in order, bounded.

| Column | Shape | Holds |
|---|---|---|
| `hypotheses` | latest | The ranked list, best first. The **order** is the ranking; `confidence` is a number the platform never sorts by. |
| `plan` | latest | `action_tool`, `action_arguments`, `target_hypothesis`, `rationale_excerpt`. A re-plan replaces it. |
| `verification` | latest | The newest verdict, for a reader that wants one line. |
| `verifications` | append-only, cap 50 | Every verdict. Three polls to reach `verified` is a different story from one, and the latest column cannot tell it. |
| `steps` | append-only, cap 200 | One entry per call the caller made: `seq`, `kind`, `tool`, `arguments`, `result_excerpt`, `outcome`, `latency_ms`, `at`. |
| `steps_dropped` | counter | How many oldest steps a cap discarded. |
| `budget` | latest | `tool_calls_used` / `_max`, `tokens_used`, `usd_used`, `wall_seconds`, on the caller's own meters. |

`phase_history` is untouched, in shape and in meaning. `report_agent_briefing` is
untouched entirely.

### Excerpts, and why the limit refuses rather than truncates

`reasoning_excerpt` and `rationale_excerpt` are capped at 280 characters, `result_excerpt`
at 400, and a longer value is **refused** at the wire rather than silently cut. Two
reasons. A silent cut stores something the caller did not write, and a caller that meant
to send a summary and sent a page would never find out. And the refusal is what keeps
option 2 from arriving by accident: there is no value of `result_excerpt` that makes this
table a trace store.

[ADR 0012](0012-the-lab-is-invisible-to-the-agent.md) rule 1 still holds, unchanged and
for the same reason it held for ADR 0035: **nothing here is readable by the agent's
principal.** These are writes with no matching read, there is still no read tool for
`agent_runs`, and the `agent.run_reported` audit stream is still withheld from whoever
holds `agent_runs:write`. An excerpt of a tool result the responder itself produced,
stored where only an operator can read it, discloses nothing to the responder it did not
already have.

### One step per call, identified by `seq`

The `step` field takes exactly one entry. A second step in one report is not
expressible — which is what keeps the ledger a ledger, and what makes the cost of one
report constant. `seq` is the caller's own position and it is the step's identity:

- a repeat of a `seq` already stored **changes nothing**, because the reporter is
  fail-open and may retry a report whose answer it never saw;
- the ledger is served sorted by `seq`, not in stored order, so a report that arrived out
  of order still draws in the right place;
- `GET /admin/agent-runs/{id}/steps?after_seq=` is a **tail read**, not an offset page: a
  console polling twice a second asks for what is new, and a step it has already drawn
  cannot arrive twice. Offset pagination over a list that grows from the end would have
  handed it duplicates.

### A reading is filled in, never cleared — unlike the two fields before it

`hypotheses`, `plan`, `verification` and `budget` are replaced when a report carries them
and **left alone when it does not**. `current_hypothesis` and `last_step` keep ADR 0035's
replace-or-clear behaviour, unchanged.

The asymmetry is deliberate and it is the fix for the first take's actual failure. The
reporter now sends a report after every tool call, most of which say nothing about
hypotheses. Under replace-or-clear, every one of those would blank the panel a person is
watching — the empty agent panel again, arriving from the opposite direction. Changing
`current_hypothesis` to match was declined: it is a shipped field with a stated meaning,
and a release that quietly changed what omitting it does would be a worse surprise than
an asymmetry two sentences can explain.

### The lag window is fifteen minutes, on the platform's side of the wire

`recent_samples` keeps its shape — same members, same newest-first order, same
`measured_at` and `age_seconds` beside it. What changes is how much of it there is: 15
measurements at one per ~60-second pass, and a TTL of 18 minutes rather than the value
key's 90 seconds.

The TTL is the decision inside the decision. The value key must be **fresh-or-absent**,
because `check_backpressure` reads it and a stale lag would gate submissions on a fault
that is over. History is the opposite: the moment an operator most wants the last fifteen
minutes is the moment the metrics pass stopped, and a window that expired 90 seconds after
its last write would take the chart with it exactly then. Still a TTL, so a window nothing
refreshes disappears rather than being read as current.

The window's key, cap and TTL now live in `app/core/consumer_lag.py` and the worker
imports them, instead of a set of literals in the worker mirrored by a set in the reader.
A cap that drifted from the reader's would have shortened the chart without shortening the
axis — invisible until someone counted the points.

### The audit filter takes lists

`action_prefix` accepts a comma list (OR-ed) and `exclude_prefix` is new (AND-ed, and it
wins where the two overlap). Each is bounded at ten prefixes, blanks and duplicates
dropped, because each becomes a `LIKE`. Nothing about withholding changes: this is the
human REST path, which has always shown every stream, and
`hidden_audit_action_prefixes` remains a rule about principals on the MCP path.

## Consequences

**What this buys.** A console can narrate a run from the platform alone: what is being
considered and how confidently, what was decided and why, what each call returned, whether
the check agreed, and what it has all cost. No dependency on the responder's filesystem,
no inference from audit rows, and nothing reconstructed in a browser that a reload throws
away.

**What it costs.** Seven columns on a table that is not swept by the environment reset
(ADR 0035's own recorded gap, narrowed by
[ADR 0036](0036-the-reset-closes-a-breaker-and-a-registry-it-cannot-restart-honours-it.md)
closing open runs), and one more report per tool call rather than per transition — a
write and a small JSON rewrite, on a fail-open path that is not counted against the
caller's budget. The ledger's cap means a run longer than 200 calls is stored as its
newest 200, which is why `steps_dropped` exists and why the REST reply carries it beside
`total`.

**What this deliberately does not do.**

- It does not verify anything the caller says. A confidence of 0.9 on a wrong explanation
  is stored as 0.9; a `verdict` outside the three values the responder uses today is
  stored as written, because a closed enum here would refuse a verdict the caller has and
  the platform has no opinion about what counts as verified.
- It does not fill anything in. An absent `at`, an absent `latency_ms` and an absent
  `outcome` stay absent; the platform stamps its own clock only where ADR 0035 already
  said it would.
- It does not make the responder's trace redundant. The trace is the complete record and
  stays the archived one; this is the part of it a person can read on a screen.
- It adds **no read surface for the responder**, no new scope, no new refusal code, no new
  Redis key and no new audit action. The tool surface count does not move.

**What is recorded rather than solved.** There is still no heartbeat: a responder that
stops reporting and one whose reporter failed look identical, and the step ledger makes
that gap more visible rather than less (a run whose last step is 90 seconds old may be
thinking or may be gone). And `verifications` is capped without a counter of its own,
unlike `steps` — a run that produced more than 50 verify verdicts would lose the oldest
silently. The cap is stated on the field; the counter was declined as a column for a case
no run in this platform's history has come within an order of magnitude of reaching.
