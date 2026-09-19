# The `/demo` page

What it shows, where every number on it comes from, and what the two principals
can and cannot see. Built by WO-R3-313 for the live demo: an operator opens one
screen, records it, and narrates an incident from injection to resolution
without switching tabs.

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

---

## The phase strip

```
healthy → fault injected → agent investigating → agent planning
        → agent remediating → verifying → recovered | escalated
```

Seven stations. The last one is the terminal **pair**, not two steps: it names
whichever terminal was actually reached, and shows `recovered | escalated` until
one is.

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

| Run state | Station | Why |
|---|---|---|
| `triaging` | agent investigating | triage is the first read; it has no station of its own |
| `investigating` | agent investigating | |
| `planning` | agent planning | |
| `remediating` | agent remediating | |
| `verifying` | verifying | |
| `resolved` | recovered | |
| `escalated` | escalated | |
| `failed` | escalated | the terminal pair is recovered \| escalated, and a failed run is the not-recovered one. The agent card still shows `failed` — "the run broke" and "the agent handed over" are different things to the person watching |

The platform's `state` enum is not the commander's: it says `triaging` where the
agent says `triage`, and it has no `awaiting_approval` (Tier-2 approvals are
unbuilt, so no run can reach it). An unrecognised value parks on the first agent
station and the card shows the real word.

### The platform's reading

Sources: `GET /api/v1/audit/logs` (unfiltered) plus the mode's own metric. The
rule, in order:

1. **No `chaos.*` row anywhere → `healthy`.** Nothing was injected, whatever
   else is happening. A healthy world with an agent poking at it is still a
   healthy world.
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
`created_at` — the platform's own clock, not the browser's idea of when the
operator pressed a key. Before any lab row it reads *no fault injected yet*.

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
(`unknown_reason`), and the phase strip adds a line saying it cannot confirm
recovery. It never renders as 0.

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
| Fenced | `fenced_by` when `fenced_at` is set, else `no` |
| Agent decided | derived — see below |

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

Only `agent.tool_invoked` rows count. `chaos.tool_invoked` rows are excluded on
purpose: the evaluator seeds this world, and attributing its seeding to the agent
would badge every planted row as something the agent decided.

`leave` is a **result, not a blank**. For `dlq_backlog` the correct answer is to
replay one row and leave four alone, so four `leave` badges is the agent passing,
not the agent doing nothing.

### Platform readings

A compact line for the active alert (`GET /api/v1/admin/alerts?active=true`,
newest first) and any breaker not `closed` (`GET /api/v1/admin/circuit-breakers`).
A breaker with no published record is **absent from that list**, never reported
closed — so "No breaker open among those publishing state" is the honest wording
and is what the panel says.

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

Newest first, from `GET /api/v1/audit/logs`. Five lanes, colour-coded:

| Lane | Colour | Rows | Rendering |
|---|---|---|---|
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
