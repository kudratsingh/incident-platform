# The `/demo` page

What it shows, where every number on it comes from, and what the two principals
can and cannot see. Built by WO-R3-313 for the live demo: an operator opens one
screen, records it, and narrates an incident from injection to resolution
without switching tabs. WO-R3-327 added the reset boundary, so a second take on
the same stack opens clean instead of opening on the first take's incident.

The other half of the demo — the step machine that fires the fault, runs the
agent and resets the world — lives in the commander repo
(`scripts/demo_live.py`, `docs/demo-runbook.md`, WO-R3-314). This file is only
about the screen.

- **Route**: `/demo`, guarded by `ProtectedRoute requiredRole="support"` — the
  same bar as `/admin`, because everything on it is operator-only.
- **Mode**: `?mode=consumer_outage` (the default) or `?mode=dlq_backlog`. The
  header's two buttons write the URL, so the mode survives a reload and can be
  pasted into a runbook.
- **Cadence**: every panel polls every **2 s** through `usePolling`, so no two
  panels disagree about *now*. Alerts and breakers poll at 10 s; they change far
  more slowly than lag does.
- **Code**: `frontend/src/pages/DemoPage.tsx` for the screen,
  `frontend/src/utils/demoPhase.ts` for every derivation (all pure functions,
  all driven from fixtures in `frontend/src/test/demoPhase.test.ts`).

---

## Three rules the page is built around

Everything below is a consequence of these, and they exist because the page is
going on camera.

1. **An absent reading renders as absent, with its reason — never as zero.**
   `lag_known: false` is not lag 0; a breaker with no published record is
   *missing from the list*, not closed. This is [ADR 0030](ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md)'s
   rule carried into the UI. Every panel therefore has three states, not two:
   a value, an empty answer, and a degraded one.
2. **The agent's word and the platform's reading are never merged.** They are
   different witnesses with different competence (below). When they name
   different phases the strip shows both, labelled. A merged reading would put a
   state on screen that neither witness ever asserted — and the disagreement is
   the most interesting thing the demo can show, because it is the shape of
   every incident where an agent believes it fixed something it did not
   (`../context/INCIDENTS.md`, INC-001…003).
3. **One panel's failure is one panel's failure.** Each panel owns its own
   request. A 403 on the agent-run endpoint — what a stack without WO-R3-312
   gives you — degrades that panel and leaves the world and the audit log up. A
   single failed poll shows the error *beside* the last good reading rather than
   blanking the panel.
4. **A reset is a boundary, and nothing older than the newest one is read.**
   See below — it is the reason a second take opens clean instead of opening on
   the first take's incident.

---

## A reset ends a take, and the page knows where

`audit_logs` is append-only by design, so a take's rows are still there after the
world it describes is gone. That made the page confidently wrong in exactly the
moment it exists for: after `make eval-reset`, the newest `chaos.*` row was still
the *previous* take's kill, so a freshly wiped world opened at **agent
remediating**, with `T+ 4m 12s since the fault` counting from an incident that no
longer existed. Every derived reading was affected — the strip, the clock, the
agent card, the metric latch, and the DLQ "agent decided" badges (the seeded
dead-letter rows come back under *stable* ids, so last take's replay named this
take's row).

The page cannot infer the boundary. It is not in the audit log, the Redis keys are
gone, and `agent_runs` says only that a run ended. So the reset **states** it: one
`lab.world_reset` audit row per reset, appended last, carrying that reset's own
counters as its payload (platform WO-R3-327).

| | |
|---|---|
| **Action** | `lab.world_reset` |
| **Written by** | `scripts/reset_eval_state.py`, once per reset, after every restoring step |
| **Payload** | that reset's summary of counters — `chaos_keys_cleared`, `dlq_reset`, `hot_set_reseeded`, … |
| **Principal** | the evaluator's service account (`incident-commander-chaos`), or a null machine principal on a stack where that account has not been seeded |
| **Visible to** | human operators, over REST. **Withheld from the agent** beside `chaos.`, under the same `chaos:invoke` condition ([ADR 0012](ADR/0012-the-lab-is-invisible-to-the-agent.md), 2026-09-20 amendment) — its payload names every mechanism the reset swept, which leaks more than a hook name would |

**Its own prefix, not `chaos.`** — because to this page the newest `chaos.*` row
*is* the fault. A boundary filed under that prefix would be read as the very thing
it exists to say did not happen.

What reads the boundary, all in `frontend/src/utils/demoPhase.ts`:

- the phase strip's platform reading and the fault clock — rows strictly newer than
  it, so a previous take's fault **and** its remediation are discarded together
  (crediting one take's `restart_consumer_group` to the next take's fault is the
  same lie in a different station);
- the agent card — the newest run the boundary has not closed out. `make eval-reset`
  closes open runs as `failed` with a `closed_by: reset` marker (WO-R3-315), so those
  are dropped. A run still **open** and older than the boundary is deliberately kept:
  that is either a race with the reset's own sweep or a responder it could not reach,
  and on camera the disagreement is worth seeing;
- the metric latch — keyed on the boundary as well as the fault, so a reset with no
  new fault yet cannot carry the previous take's observed breach;
- the DLQ badges — a decision older than the boundary is not this take's.

Two details worth knowing. A row sharing the boundary's exact timestamp belongs to
the take being **closed** — the reset writes its row last, so anything simultaneous
with it is the world it just wound up. And on a stack older than WO-R3-327 there is
no boundary row, so the page behaves exactly as it did before and reads the whole
history: an inferred boundary would be a guess about which rows to throw away.

In the timeline the boundary is drawn as a **grey divider** (`world reset · HH:MM:SS`)
rather than as a row, under every filter chip — rows below the line are a previous
take. It is the only thing on the page with no actor colour, because nothing happened
to the world there.

---

## The phase strip

```
healthy → fault injected → agent investigating → agent planning
        → awaiting approval → agent remediating → verifying
        → recovered | escalated
```

Eight stations. The last one is the terminal **pair**, not two steps: it names
whichever terminal was actually reached, and shows `recovered | escalated` until
one is.

`awaiting approval` has a station of its own even though the approvals subsystem
is unbuilt and no run reaches it today. The nine reportable states are the
commander's own `IncidentState` values with no mapping layer on either side of
the wire, so this one arrives the moment Tier-2 approvals ship — and a state with
nowhere to land is a state that silently reads as something else. An empty
station on screen is the cheaper mistake.

Each lit station carries up to two markers:

| Marker | Means |
|---|---|
| `agent` (purple) | what the agent reported about itself |
| `platform` (blue) | what the platform can see for itself |

### The agent's word

Source: the newest active row from `GET /api/v1/admin/agent-runs?active=true`
— its `state` and `phase_history`, written by the commander over the two
`[commander: telemetry]` MCP tools (platform ADR 0035, written by WO-R3-312).

This is the **only** source that can tell investigating from planning from
remediating, because a plan leaves no mark on the platform at all. The agent
thinking is invisible from the outside.

All nine states, and the eight stations they land on:

| Run state | Station | Why |
|---|---|---|
| `triage` | agent investigating | triage IS the first read; a station for it would be lit for a second or two at most |
| `investigating` | agent investigating | |
| `planning` | agent planning | |
| `awaiting_approval` | awaiting approval | unreachable today; see above |
| `remediating` | agent remediating | |
| `verifying` | verifying | |
| `resolved` | recovered | |
| `escalated` | escalated | |
| `failed` | escalated | the terminal pair is recovered \| escalated, and a failed run is the not-recovered one. The agent card still shows `failed` — "the run broke" and "the agent handed over" are different things to the person watching |

These nine strings are the commander's own `IncidentState` values, character for
character: the responder maps nothing on its way out and the platform maps
nothing on its way in, so a state cannot be lost in translation and a member
added in one repository and not the other is a refusal at the wire rather than a
silently dropped state. Only `resolved` / `escalated` / `failed` close a run
(`finished_at`, surfaced as the computed `active`). An unrecognised value — this
list can only ever be one release behind — parks on the first agent station and
the card shows the real word.

One field name differs between the two halves on purpose: the MCP write side
calls the run's short name `run_label`, because ADR 0012's registry screen bans
the lab's word for it from a non-chaos tool's `tools/list` surface. It lands in
`agent_runs.scenario` and reaches this console under that name.

### The platform's reading

Sources: `GET /api/v1/audit/logs` (unfiltered) plus the mode's own metric. Every
step below reads only rows **newer than the newest `lab.world_reset`** — see "A
reset ends a take" above. The rule, in order:

1. **No `chaos.*` row in this take → `healthy`.** Nothing was injected, whatever
   else is happening. A healthy world with an agent poking at it is still a
   healthy world, and a world that was just reset is a healthy world however
   complete the take before it was.
2. **The metric has been breached and is now back inside its threshold →
   `recovered`.** All three conditions are required: a reading exists, a breach
   really happened, and the reading is back inside the bar.
3. **An `agent.tool_invoked` row naming a Tier-1 action, at or after the fault →
   `agent remediating`.**
4. **Any `agent.tool_invoked` row at or after the fault → `agent investigating`.**
5. **Otherwise → `fault injected`.**

Step 2's middle condition is the one that matters and the one that is easy to
miss. The metric is inside its threshold **both** before the fault lands and
after it is fixed — lag is 0 in a healthy world and 0 again after a successful
restart. A rule that only asked "is it inside the bar?" would flash *recovered*
during the seconds between injecting the fault and it becoming visible, which on
camera is a lie about the most important moment in the demo. So the page latches
"a breach has been observed" and resets that latch whenever a **new** fault row
appears, so a second take in one session starts clean.

Consequence worth knowing: if you open the page *after* the fault has already
been fixed, it says `fault injected`, not `recovered` — it never saw the breach.
`make demo-live` prints the console URL at baseline, before the fault, for
exactly this reason.

### The clock

`T+ <elapsed> since the fault`, counted from the newest `chaos.*` audit row's
`created_at` **in the current take** — the platform's own clock, not the browser's
idea of when the operator pressed a key. Before any lab row, and after a reset with
no new fault yet, it reads *no fault injected yet*.

---

## Left column — the world

### The two sparklines

| Panel | Value from | Threshold | Why that number |
|---|---|---|---|
| `worker-dispatcher` lag | `GET /api/v1/admin/consumer-lag`, the `worker-dispatcher` group's `lag` (only when `lag_known`) | **20** | `remediate_consumer_lag_success` polls until lag ≥ 20 before it lets the agent start, so below 20 is the world back inside its bar |
| DLQ depth | `GET /api/v1/admin/dlq/stats`, `total` | **4** | `remediate_dlq_backlog_success` seeds 5 dead letters of which exactly one is replay-safe, and grades on `replayed == 1`. Four rows left is the fixed world; the other four are *supposed* to stay |

Both panels are always rendered, in both modes; the mode decides which one the
phase strip's recovery rule reads.

**The five-minute window is client-side, and that is deliberate.** The REST lag
reading carries only the handful of samples the metrics loop cached, and DLQ
depth carries none at all, so a server-side five-minute series does not exist to
be fetched. The honest consequence: *a page opened thirty seconds ago shows
thirty seconds.* Where the lag reading does bring `recent_samples`, they backfill
the line so a page opened mid-run is not starting from nothing.

An unknown lag renders as `unknown` plus the reason the platform gave
(`lag_unknown_reason`, which is null exactly when `lag_known` is true so a blank
cell always has an explanation beside it), and the phase strip adds a line saying
it cannot confirm recovery. It never renders as 0.

Two details of the reading that a chart has to respect. `recent_samples` arrives
**newest first**, so the seed is reversed before it is drawn — fed in as given,
the series' own span goes negative and every point lands off the left edge. And
`live_group` names the one group whose number actually moves; the others are
recorded constants. The page reads `worker-dispatcher` by name and falls back to
`live_group`, in that order: the fallback is the right answer if the group were
ever renamed and the wrong one to prefer while the named group is present.

### The jobs strip

The last 20 jobs from `GET /api/v1/admin/jobs`, one chip each, coloured by
status (hover for type, status and time), with a per-status count line
underneath. In `consumer_outage` this is where a viewer sees `make traffic`'s
jobs stop completing and then start again.

### The DLQ mini-table (`dlq_backlog` mode only)

Rows from `GET /api/v1/admin/jobs?status=dead_letter`. Six columns:

| Column | Source |
|---|---|
| Row | the job id, first 8 characters |
| Error | `error_message` |
| Hint | `remediation_hint`, or **`not categorised`** when null |
| Triage | `triage.root_cause_category`, or `none` |
| Fenced | `fenced_by` when `fenced_at` is set, else `no`. The value is `{principal_type}:{id}`, shown truncated with the whole string in the title. It reads the *timestamp*, not the hint, because a null `fenced_at` with a `human_required` hint means triage wrote that hint and no operator fenced anything |
| Agent decided | derived — see below |

`dead_lettered_at` is computed server-side from `completed_at` and is null for
any job that is not dead-lettered — one fact rather than two that can disagree,
and `status` is the reason, so it carries no reason string of its own.

`remediation_hint: null` prints *not categorised* rather than a dash because
null means "the platform has not classified this row". It is emphatically **not**
"replay-safe" — the platform's triage is off by default, so organically
dead-lettered rows stay null.

**"Agent decided" is derived from the agent's own audit rows**, newest decision
first, so a run that changed its mind shows the last thing it decided:

| Badge | Derived from |
|---|---|
| `replay` | `replay_dlq_by_ids` / `replay_dlq_messages` whose `job_ids` contain this row, **or** `replay_dlq_by_category` whose `category` equals this row's `remediation_hint` (and whose `job_type`, if narrowed, matches) |
| `fence` | `mark_dlq_permanent` whose `job_id` is this row |
| `leave` | nothing above |

Only `agent.tool_invoked` rows count, and only those newer than the newest
`lab.world_reset`. `chaos.tool_invoked` rows are excluded on purpose: the evaluator
seeds this world, and attributing its seeding to the agent would badge every planted
row as something the agent decided. Rows from a previous take are excluded for a
sharper version of the same reason — the seeded dead-letter rows come back under
*stable* ids, so last take's `replay_dlq_by_ids` names this take's row exactly.

`leave` is a **result, not a blank**. For `dlq_backlog` the correct answer is to
replay one row and leave four alone, so four `leave` badges is the agent passing,
not the agent doing nothing.

### Platform readings

A compact line for the active alert (`GET /api/v1/admin/alerts?active=true`,
newest first) and any breaker not `closed` (`GET /api/v1/admin/circuit-breakers`).

A breaker with no published record is **absent from that list**, never reported
closed — so "No breaker open among those publishing state" is the honest wording
and is what the panel says. The endpoint's `unknown_reason` is the other half of
that: an empty list *with* it set means the platform could tell you nothing, and
the panel says so instead. Both answers are an empty array, and only one of them
means nothing is open, which is why the console carries the whole response rather
than just the array.

---

## Middle column — the agent

The active run's card:

- the run's **state** as a pill, and the scenario name it reported;
- the **current hypothesis** — name, category, and confidence as a bar plus a
  percentage. "None yet" when the agent has not ranked a cause;
- the **last step** — `read` or `action`, the tool, and the time;
- the **phase history** as a vertical timeline with a duration per phase, each
  measured against the next phase's start (and the last one against
  `finished_at`, or `ongoing` while the run is still there).

Before the agent reports anything: *Waiting for the agent to report a run.*

---

## Right column — the audit timeline

Newest first, from `GET /api/v1/audit/logs`. Five lanes plus the boundary,
colour-coded:

| Lane | Colour | Rows | Rendering |
|---|---|---|---|
| world reset | grey | `lab.world_reset` | a **divider** across the timeline, not a row — `world reset · HH:MM:SS` |
| lab | amber | `chaos.*` | tool name and arguments — what exactly was fired |
| agent action | blue | `agent.tool_invoked` whose tool is a Tier-1 action | expanded: tool, arguments, platform outcome, latency |
| agent read | grey | `agent.tool_invoked`, anything else | consecutive reads collapse into one "N reads" group you can expand |
| agent run report | purple | `agent.run_reported` | collapsed the same way |
| human | green | everything with `principal_type: user` | action and time |

`chaos.*` is matched on the **action**, not the principal: the evaluator is a
service account too, so a principal-only test would file the lab's rows under
"agent" and make the fault look like the agent's doing.

The four chips — all / agent / lab / human — narrow the query server-side
(`action_prefix=chaos.` for the lab, `principal_type=` for the other two) *and*
filter the render, so the two can never show different rows. The unfiltered
stream is always fetched as well, because the phase strip and the DLQ badges
derive from it: a chip the operator clicked must not be able to change what the
strip says.

The divider comes from that unfiltered stream too, which is why it survives every
chip: `lab.` and `chaos.` are different prefixes and one server-side filter cannot
carry both, so the `lab` chip fetches the faults and the line is drawn from what the
page already has. The rows are split at the boundary *before* they are grouped, so a
run of collapsible reads can never straddle the line and hide it inside a "N reads"
group. Where a reset has happened and nothing has followed it, the panel is the
divider alone — which is the truthful first frame of a recording.

---

## Bottom — the escalation briefing

Rendered only once `agent_run.briefing` lands; absent until then.

- **final state** as a pill (green for `resolved`, red otherwise);
- **alert summary** and **escalation reason**;
- **attempted action** — the tool and its arguments, or "None — the agent
  escalated without acting";
- **the verdict**. Worth being explicit: the briefing carries **no separate
  verification field**. The verdict *is* the final state, and the escalation
  reason is the judgement behind it. The card says so rather than inventing a
  field the commander never sends;
- **causes**, as the three slots of the commander's ADR 0065 —
  primary, secondary, unresolved extra — each with category, name, confidence
  and whether any attempt in the run aimed at it. `unresolved extra` is printed
  even when empty: the remainder is the thing a reader must not have to infer
  from an absence;
- **what the writer said** — the enrichment prose. On a canned (unenriched) run
  it says so plainly instead of leaving a gap.

**Copy as Markdown** puts the whole card on the clipboard for an incident
channel, including the empty slots. On a plain-HTTP stack `navigator.clipboard`
is undefined, so it falls back to `execCommand` and reports failure rather than
claiming a copy that did not happen (the `copyToClipboard` helper in
`components/TraceId.tsx`).

---

## What the two principals can and cannot see

This difference *is* the demo's point, and the page is on the human side of it.

| | The agent (`incident-commander`, MCP) | A human operator (this page, REST) |
|---|---|---|
| `chaos.*` audit rows | **withheld** — `list_audit_events` and `get_trace` exclude the prefix in SQL and out of `total` for any principal without `chaos:invoke` ([ADR 0012](ADR/0012-the-lab-is-invisible-to-the-agent.md), 2026-09-15 amendment) | visible |
| `lab.world_reset` — the boundary | **withheld**, same mechanism and same condition (2026-09-20 amendment). Asking for it by prefix or by exact action is an empty page with `total: 0`, never a refusal | visible, payload included — this page reads the boundary from it |
| `agent_runs` — its own reported run | **not readable at all**. There is no read tool, by design (platform ADR 0035, written by WO-R3-312); the two write tools are called by the loop's checkpoint hook, not chosen by the model, and are excluded from the planner surface | visible |
| Consumer lag | `get_consumer_lag` (one group per call) | `GET /admin/consumer-lag` (every group) |
| DLQ rows | `list_dlq_messages` | `GET /admin/jobs?status=dead_letter`, with `remediation_hint` / `dead_lettered_at` / `fenced_at` / `fenced_by` / `triage` |
| Breakers, alerts, SLOs | `get_circuit_breakers`, `list_active_alerts`, `get_slo_status` | the `/admin` twins of the same readings |

So: the agent never learns *that it was a lab*, and never sees what it told the
platform about itself. The operator sees both. Nothing on this page is reachable
by the agent's principal, which is why it can say things the agent must not know.

---

## Running the console

In this repo's compose it is the `frontend` service on `http://localhost:3000`.

The console also ships as its own image,
`ghcr.io/<owner>/incident-platform-console:vX.Y.Z`, published by
`.github/workflows/release.yml` on the same version as the backend and with its
digest in the run summary. Its nginx config is a template
(`frontend/nginx.conf.template`) whose `/api/` upstream is `${API_UPSTREAM}`:

| Stack | Backend service | `API_UPSTREAM` |
|---|---|---|
| this repo's `docker-compose.yml` | `app` | `http://app:8000` (set in compose) |
| the commander's `demo/compose.yml` | `api` | `http://api:8000` (the image default) |
| ECS | — | unused; the ALB routes `/api/*` before nginx sees it |

One image, two stacks, one variable. The upstream is held in an nginx *variable*
so the name is resolved per request rather than at config load — which is what
makes a value naming a host that does not exist cost nothing until a request
arrives (WO-R2-65).
