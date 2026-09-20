# The `/demo` page — an agent-run dashboard

What is on the screen, where every number comes from, and what the two principals
can and cannot see.

An operator opens one page, records it, and narrates an incident from injection to
resolution without switching tabs. WO-R3-313 built the first version, WO-R3-327
added the reset boundary, and **WO-R3-330 rebuilt it around one run** after the
owner's first live take — see "What the first take showed" at the end, which is the
whole reason the page has the shape it does.

The other half of the demo — the step machine that fires the fault, runs the agent
and resets the world — lives in the commander repo (`scripts/demo_live.py`,
`docs/demo-runbook.md`, WO-R3-329). This file is only about the screen.

| | |
|---|---|
| **Route** | `/demo`, `ProtectedRoute requiredRole="support"` — the same bar as `/admin`, because everything on it is operator-only |
| **Mode** | `?mode=consumer_outage` (default) or `?mode=dlq_backlog`; the header buttons write the URL |
| **Run** | `?run=<id>`; the default is the newest run since the reset boundary, and the selector lists every run of the take |
| **Cadence** | every panel polls every **2 s** through `usePolling`, so no two panels disagree about *now*; alerts and breakers poll at 10 s |
| **Code** | `frontend/src/pages/DemoPage.tsx`, with every derivation in `frontend/src/utils/demoPhase.ts` (the incident) and `frontend/src/utils/demoRun.ts` (the run record) — all pure functions, all driven from fixtures in `src/test/demoPhase.test.ts`, `src/test/demoRun.test.ts` and `src/test/DemoPage.test.tsx` |
| **Backend** | the shapes WO-R3-328 added to `agent_runs` and to the read endpoints (plat #230, → v0.6.16): the ranked `hypotheses`, `plan`, `verification`/`verifications`, `steps` with `steps_dropped`, `budget`; `GET /admin/agent-runs/{id}/steps?after_seq=`; the 15-minute lag window with `sample_window_seconds` / `sample_interval_seconds`; comma-list `action_prefix` and `exclude_prefix` on the human audit filter |

Layout, at 1440×900 with no scroll for the top half:

```
  header      run selector · mode · T+ since the fault
  PLATFORM    healthy → fault injected → agent acting → recovered
  AGENT       triage → investigating → planning → awaiting approval
              → remediating → verifying → resolved | escalated | failed
  ┌───────────────┬─────────────────────────┬──────────────────┐
  │ metric chart  │ the agent               │ action ledger    │
  │ (15 min)      │ hypotheses · plan       │ one row per step │
  │ + readings    │ verifications · budget  │ + lab + reset    │
  └───────────────┴─────────────────────────┴──────────────────┘
  briefing (when it lands) · DLQ table (dlq mode)
```

---

## Five rules the page is built around

1. **An absent reading renders as absent, with its reason — never as zero.**
   `lag_known: false` is not lag 0; a breaker with no published record is *missing
   from the list*, not closed. [ADR 0030](ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md)
   carried into the UI. Every panel has three states, not two: a value, an empty
   answer, and a degraded one.
2. **The two witnesses get a row each and are never merged.** The platform's row
   holds only what the platform can assert for itself; the agent's row holds only
   what the responder said about itself. Both are always rendered. Disagreement is
   therefore visible by construction — and it is the most interesting thing the
   demo can show, because it is the shape of every incident where an agent believes
   it fixed something it did not (`../context/INCIDENTS.md`, INC-001…003).
3. **One panel's failure is one panel's failure.** Each panel owns its own request.
   A 403 on the agent-run endpoint degrades that panel and leaves the chart and the
   ledger up. A single failed poll shows the error *beside* the last good reading
   rather than blanking the panel.
4. **A reset is a boundary and nothing older than the newest one is derived from**
   (WO-R3-327) — see below.
5. **An absence is named, not filled.** A run with no `hypotheses` is not a run
   that ranked nothing: the panel says which of the two it is looking at, because
   a stack older than v0.6.16 and a commander older than WO-R3-329 are different
   absences and both are possible.

---

## A reset ends a take, and the fault is latched inside it

`audit_logs` is append-only by design, so a take's rows are still there after the
world they describe is gone. That made the page confidently wrong in exactly the
moment it exists for: after `make eval-reset` the newest `chaos.*` row was still the
*previous* take's kill, so a freshly wiped world opened at **agent remediating**,
`T+ 4m 12s since the fault`, counting from an incident that no longer existed.

The page cannot infer the boundary — it is not in the audit log, the Redis keys are
gone, and `agent_runs` says only that a run ended. So the reset **states** it: one
`lab.world_reset` audit row per reset, appended last, carrying that reset's own
counters as its payload (platform WO-R3-327).

| | |
|---|---|
| **Action** | `lab.world_reset` |
| **Written by** | `scripts/reset_eval_state.py`, once per reset, after every restoring step |
| **Payload** | that reset's counters — `chaos_keys_cleared`, `dlq_reset`, `hot_set_reseeded`, … |
| **Principal** | the evaluator's service account (`incident-commander-chaos`), or a null machine principal where that account has not been seeded |
| **Visible to** | human operators, over REST. **Withheld from the agent** beside `chaos.`, under the same `chaos:invoke` condition ([ADR 0012](ADR/0012-the-lab-is-invisible-to-the-agent.md), 2026-09-20 amendment) — its payload names every mechanism the reset sweeps |

**Its own prefix, not `chaos.`** — because to this page the newest `chaos.*` row *is*
the fault. A boundary filed under that prefix would be read as the very thing it
exists to say did not happen.

Everything derived reads rows and runs strictly newer than the newest boundary: the
platform row and the clock, the run selector, the metric's breach and recovery, the
chart's markers, the DLQ badges. A row sharing the boundary's exact timestamp
belongs to the take being **closed** — the reset writes its row last. On a stack
older than WO-R3-327 there is no boundary row, so the page reads the whole history
exactly as it did before; an inferred boundary would be a guess about which rows to
throw away.

### The fault is latched for the take

The boundary alone was not enough. `newestFaultAt` reads the newest `chaos.*` row
*in the page's window of rows*, and with `make traffic` running the window is mostly
job events — so the row that states the fault fell out of it within a minute and the
platform row dropped back to **healthy** in the middle of the run.

Two fixes, both needed. The audit query asks for the operator streams only (below),
so the row survives far longer. And the fault is **latched**: once a `chaos.*` row
has been seen in this take the page holds its timestamp, releasing it only for a
newer fault row or a new boundary. `platformPhase`/`platformRow` take that latched
value as an input (`faultAt`), and a latched fault at or before the boundary is
dropped, so a latch can never outlive its take.

---

## The two rows

### PLATFORM — `healthy → fault injected → agent acting → recovered`

Four stations, each carrying its own timestamp and, once passed, its duration.
Source: `GET /api/v1/audit/logs` (the operator streams) plus the mode's metric.

| Station | Reached when | Stamped with |
|---|---|---|
| healthy | always — it is where every take starts | the boundary (`lab.world_reset`), which is when this world began |
| fault injected | a `chaos.*` row exists in this take, **or the latch holds one** | the lab's own row time |
| agent acting | an `agent.tool_invoked` row at or after the fault | the first such row; the note says how many reads, and names the Tier-1 action once one fires |
| recovered | the metric was breached after the fault and is back inside its bar, sustained | the sample that started the inside-the-bar run |

The last station reached is the current one; earlier reached stations are `passed`
and carry `next.at − this.at` as their duration.

### AGENT — the seven stations of `phase_history`

Source: the selected run's `phase_history` (ADR 0035), written by the commander over
the two `[commander: telemetry]` MCP tools. This is the **only** source that can tell
investigating from planning from remediating, because a plan leaves no mark on the
platform at all — the agent thinking is invisible from the outside.

The nine reportable states land on seven stations: the three terminals
(`resolved` / `escalated` / `failed`) share the last one, which is labelled with
whichever was actually reached. `awaiting approval` has a station of its own even
though the approvals subsystem is unbuilt — the nine states are the commander's own
`IncidentState` values with no mapping layer on either side of the wire, so that one
arrives the moment Tier-2 approvals ship, and a state with nowhere to land is a
state that silently reads as something else.

A station can be entered **twice** — `verifying` may hand back to `investigating` —
so each one carries its visit count and the sum of its *closed* visits. The station
the run is in now shows `ongoing` rather than a duration. An unrecognised state (this
list can only ever be one release behind) lights nothing and the panel shows the word
verbatim.

The line under the two rows states both readings in plain words, and adds the
metric's own caveats: recovery pending on one sample, or no reading at all.

---

## The metric chart (left)

One series, one axis, 15 minutes wide.

| Mode | Value from | Threshold | Why that number |
|---|---|---|---|
| `consumer_outage` | `GET /admin/consumer-lag`, the `worker-dispatcher` group (only when `lag_known`) | **20** | `remediate_consumer_lag_success` polls until lag ≥ 20 before the agent starts, so below 20 is the world back inside its bar |
| `dlq_backlog` | `GET /admin/dlq/stats`, `total` | **4** | `remediate_dlq_backlog_success` seeds 5 dead letters of which exactly one is replay-safe and grades on `replayed == 1`; four rows left is the fixed world |

**The lag window is the platform's own, and so is the axis.** Since WO-R3-328
`recent_samples` carries about 15 minutes — one sample per metrics pass, the same
reading the agent gets — and the reply states its own shape in
`sample_window_seconds` (900) and `sample_interval_seconds` (60). The chart takes
both from the answer rather than hard-coding them, so the axis says "−10 min, one
every 30s" if the platform's cadence ever changes, instead of mislabelling a
window it no longer has. `recent_samples` arrives **newest first** and is reversed
before it is drawn; fed in as given, the series' own span goes negative and every
point lands off the left edge. `live_group` names the one group whose number
actually moves; the page reads `worker-dispatcher` by name and falls back to
`live_group`, in that order.

**DLQ depth has no server-side history** — the endpoint is one number — so in
`dlq_backlog` mode the line is what *this page* has observed since it opened, and the
caption says so. It is a second **chart**, never a second series on the lag axis: the
two run to different magnitudes and one plot with two scales invents a relationship
between them.

Drawn on the plot:

- a **band** above the threshold, with the threshold line labelled;
- **markers** for the boundary (W), the fault (F), each Tier-1 **action** (A) and the
  **recovery** (R), each listed underneath with its glyph and its time, so identity
  is never colour alone. Reads are deliberately *not* marked — fifteen ticks on a
  fifteen-minute chart is a comb, and the ledger is where every call belongs;
- a **crosshair and tooltip** on hover, and a `samples (N)` table underneath with
  every value in it, so nothing is reachable only by hovering.

Actions come from the run's `steps` when it has them and from `agent.tool_invoked`
audit rows when it does not.

### Recovery takes two samples, not one

The first take's rule was "a breach was seen and the latest reading is inside the
bar", evaluated on one poll — and the cached lag value reads `42 → 0 → 42` as the
sample ages, so the strip announced a recovery in the middle of the incident and
then took it back.

`metricRecovery` reads the platform's samples instead: the breach is the first
sample outside the bar at or after the fault, and the recovery is the first sample of
**two consecutive** samples inside it. While only one sample is back inside, the page
says so — *"back inside its bar since 10:04:12 but only for one sample; recovery
takes 2"* — instead of either lying or going quiet.

Consequence worth knowing: open the page *after* the fault has already been fixed and
the platform row says `fault injected`, not `recovered`. It never saw the breach.
`make demo-live` prints the console URL at baseline for exactly this reason.

---

## The agent panel (centre) — the point of the page

Everything here is the selected run's record, read from
`GET /admin/agent-runs/{id}` (WO-R3-328). All of it was blank in the first take,
because the record carried one hypothesis and one step and both were filled only at
the end.

| Section | Source | When it is absent |
|---|---|---|
| state pill, run label, finished-at | `state`, `scenario`, `finished_at` | an unrecognised state renders verbatim |
| **budget** | `budget` — calls used against the cap, tokens, dollars, wall seconds | "not reported"; a bar with no cap is a bar with an invented denominator, so there is none |
| **hypotheses, ranked** | `hypotheses[]` — name, category, confidence bar, reasoning excerpt (≤ 280 chars) | "None reported yet". Where only `current_hypothesis` exists it becomes a one-entry list **and the panel says so**: that is a commander older than WO-R3-329, not a run that ranked nothing |
| **the plan** | `plan` — tool, arguments, the hypothesis it is aimed at, the rationale excerpt | "No action planned yet" |
| **verification** | `verifications[]` — every verify poll's verdict, attempt *n* of *m*, and its reasoning; capped at 50, and the panel says so when it is full | "Nothing verified yet"; where only the latest `verification` exists, that one row |

**The hypothesis list is rendered in the order it arrives, not sorted.** The
platform stores it best first and states that the order *is* the ranking;
`confidence` is a number the responder attached to each entry. A reader that
re-sorted by it would silently disagree with the run about what it thought most
likely, and it is the run's opinion this panel exists to show.

The verdict vocabulary the commander writes today is `verified`, `not_verified`,
`verified_stabilizer` and `verified_unresolved`, but `verdict` is an **open string**
on the wire: an unknown one renders verbatim in a neutral badge rather than being
guessed at or dropped.

---

## The action ledger (right)

One row per **step**, newest first: sequence number, time, a kind badge
(READ grey / ACTION blue / REPORT purple), the tool, its arguments as compact JSON,
the outcome, the latency, and the **result excerpt** behind one click.

That excerpt is the whole reason the ledger is built from steps: an
`agent.tool_invoked` audit row carries tool, arguments, latency and outcome but
**no result**, so "what did the agent see" could not be shown from the audit log at
all. Where a run reported no steps the ledger falls back to those rows, labels
itself as doing so, and the rows say plainly that the audit log records no result.

Three things about the source, all of them WO-R3-328's rules rather than choices
this page made:

- **The ledger is never read from a list row.** `GET /admin/agent-runs` returns a
  summary that *omits* `steps` — absent, not emptied, because a page of 100 runs ×
  200 entries is megabytes and an empty list would read as "this run made no
  calls". It comes from `GET /admin/agent-runs/{id}` (the whole ledger, once) and
  `.../steps?after_seq=` (the tail, every two seconds), merged by `seq`.
- **The tail read's cursor is the platform's.** `next_after_seq` is the highest
  `seq` *stored*, not the highest returned, so a poll that finds nothing still
  advances; recomputing it from what arrived would re-read the tail forever.
- **Every field but `seq` and `kind` can be null.** A step is the responder's own
  account of a call it made and the platform fills nothing in, so a step with no
  tool name or no timestamp is rendered as such — and placed in the ledger by its
  `seq`, which is the order it happened in.

The ledger holds the newest **200** steps; `steps_dropped` says how many the cap
discarded, and the panel reports it above the rows.

Interleaved by time, from the same audit stream the rest of the page derives from:

| Row | Colour | Rendering |
|---|---|---|
| `chaos.*` | amber | the lab's tool and arguments — exactly what was fired |
| `lab.world_reset` | grey | a **divider** across the ledger, `world reset · HH:MM:SS`, not an event. Every boundary in the window gets one, so two takes' rows are never silently mixed |
| `principal_type: user` | green | a human's own action |
| `event.*` | grey | **off by default**, one toggle |

`chaos.*` is matched on the **action**, not the principal: the evaluator is a service
account too, so a principal-only test would file the lab's rows under "agent" and make
the fault look like the agent's doing. The agent's own `agent.tool_invoked` and
`agent.run_reported` rows are dropped while steps exist — they are the same events
without their results, and drawing both would double every row.

**The job events are the toggle that made this page watchable.** With traffic
running, `event.job.*` was 43 of the 50 rows on screen and the four rows the demo is
about were underneath them. The page therefore asks for
`action_prefix=agent.,lab.,chaos.` (a comma list since WO-R3-328) and fetches
`action_prefix=event.` only while the toggle is on — so the job stream can never
crowd out a derivation.

**Both witnesses are counted**, in one line above the rows: *N steps reported · M
calls the platform recorded*. The reporter is fail-open by design (commander
invariant 5), so it can stop reporting without the run noticing; when the two counts
disagree the line says so.

---

## The briefing (below the fold)

Rendered once `agent_run.briefing` lands, and it **stays** — see the `active=true`
bug below.

- **final state** as a pill (green for `resolved`, red otherwise);
- **alert summary** and **escalation reason**;
- **attempted action** — the tool and its arguments, or "None — the agent escalated
  without acting";
- **verify verdict** — from the run record's own `verification`, not from the
  briefing, which has never carried one. The first build said so in a sentence; now
  that the record carries the verdicts, the card shows the verdict itself;
- **recovery attribution** ([ADR 0071](https://github.com/kudratsingh/incident-commander), commander-side) —
  `attributed` / `cleared_on_its_own` / `cannot_attribute`, with the resource, the
  read tool whose readings answered it, whether the run acted on that resource, and
  the one-sentence detail. A run that read a recovery it cannot claim says so here
  rather than in prose;
- **causes**, as the three slots of commander ADR 0065 — primary, secondary,
  unresolved extra — each with category, name, confidence and whether any attempt
  aimed at it. `unresolved extra` is printed even when empty: the remainder is the
  thing a reader must not have to infer from an absence;
- **what the writer said** — the enrichment prose, or a plain statement that this run
  was not enriched.

**Copy as Markdown** puts the whole card on the clipboard, attribution and verdict
included. On a plain-HTTP stack `navigator.clipboard` is undefined, so it falls back
to `execCommand` and reports failure rather than claiming a copy that did not happen.

---

## The DLQ table (`dlq_backlog` mode)

Rows from `GET /admin/jobs?status=dead_letter`: id, type, error, hint, triage class,
fence state, and what the agent decided.

`remediation_hint: null` prints **not categorised** rather than a dash, because null
means "the platform has not classified this row" and is emphatically *not*
"replay-safe" — triage is off by default, so organically dead-lettered rows stay null.
The fence column reads the `fenced_at` **timestamp**, not the hint: a null `fenced_at`
with a `human_required` hint means triage wrote that hint and no operator fenced
anything.

**"Agent decided" is derived from the run's own steps**, newest `seq` first:

| Badge | Derived from |
|---|---|
| `replay` | `replay_dlq_by_ids` / `replay_dlq_messages` whose `job_ids` contain this row, or `replay_dlq_by_category` whose `category` equals this row's `remediation_hint` (and whose `job_type`, if narrowed, matches) |
| `fence` | `mark_dlq_permanent` whose `job_id` is this row |
| `leave` | nothing above |

Steps rather than audit rows, for three reasons: they are scoped to the run on
screen, `seq` orders two decisions about one row without comparing clocks, and the
lab's own seeding can never be read as the agent's decision. Where a run reported no
steps the badge falls back to the audit-log rule of the first build, which is bounded
to the current take for the sharper version of the same reason: the seeded
dead-letter rows come back under **stable** ids, so last take's `replay_dlq_by_ids`
names this take's row exactly.

`leave` is a **result, not a blank**. For `dlq_backlog` the correct answer is to
replay one row and leave four alone, so four `leave` badges is the agent passing.

---

## What the two principals can and cannot see

This difference *is* the demo's point, and the page is on the human side of it.

| | The agent (`incident-commander`, MCP) | A human operator (this page, REST) |
|---|---|---|
| `chaos.*` audit rows | **withheld** — `list_audit_events` and `get_trace` exclude the prefix in SQL and out of `total` for any principal without `chaos:invoke` ([ADR 0012](ADR/0012-the-lab-is-invisible-to-the-agent.md), 2026-09-15 amendment) | visible |
| `lab.world_reset` — the boundary | **withheld**, same mechanism and condition (2026-09-20 amendment) | visible, payload included |
| `agent_runs` — its own reported run, steps, plan, budget | **not readable at all**. There is no read tool, by design ([ADR 0035](ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md)); the write tools are called by the loop's checkpoint hook, not chosen by the model, and are excluded from the planner surface | visible |
| Consumer lag | `get_consumer_lag` (one group per call) | `GET /admin/consumer-lag` (every group, same 15-minute samples) |
| DLQ rows | `list_dlq_messages` | `GET /admin/jobs?status=dead_letter`, with `remediation_hint` / `dead_lettered_at` / `fenced_at` / `fenced_by` / `triage` |
| Breakers, alerts, SLOs | `get_circuit_breakers`, `list_active_alerts`, `get_slo_status` | the `/admin` twins of the same readings |

So: the agent never learns *that it was a lab*, and never sees what it told the
platform about itself. The operator sees both.

---

## What the first take showed

The owner recorded this page live on 2026-09-20 and it was unwatchable. Six findings,
each one a rule above rather than a tweak:

1. **The strip switched witnesses.** One row of stations with two markers meant the
   agent's word was the only thing lighting anything the moment a run existed, so
   `fault injected` — which only the platform can assert — was never on screen. →
   two rows, always both.
2. **The metric latch released on one sub-threshold poll** and the fault row fell out
   of the page's window, so the reading fell back to `healthy` mid-run. → recovery
   takes two of the platform's own samples; the fault is latched for the take.
3. **The agent panel was empty.** `report_agent_run` sent `current_hypothesis: null`
   and `last_step: null` on every transition. → the run record carries the ranked
   hypotheses, the plan, the verdicts, the budget and the steps (WO-R3-328), and this
   panel is the middle of the page.
4. **The timeline was the traffic loop** — 43 of 50 rows were `event.job.completed`.
   → the operator streams in one request, job events behind one toggle, off.
5. **No audit row carries a result**, so "what the agent saw" could not be shown. →
   the ledger is built from `steps`, which carry a result excerpt per call.
6. **Everything was too small.** → every number is labelled and large enough to read
   in a recording, and the top half is one screen at 1440×900.

One more, found while rebuilding and not in that list: the page asked for
`agent-runs?active=true`. A run's terminal report stamps `finished_at` and the
briefing lands in the same breath, so the run — and the briefing card with it —
disappeared within one poll of resolving. **The take could never show its own
ending.** The page now reads every run and picks by the boundary, which is also what
makes the run selector possible.

---

## Running the console

In this repo's compose it is the `frontend` service on `http://localhost:3000`.

The console also ships as its own image,
`ghcr.io/<owner>/incident-platform-console:vX.Y.Z`, published by
`.github/workflows/release.yml` on the same version as the backend and with its digest
in the run summary. Its nginx config is a template (`frontend/nginx.conf.template`)
whose `/api/` upstream is `${API_UPSTREAM}`:

| Stack | Backend service | `API_UPSTREAM` |
|---|---|---|
| this repo's `docker-compose.yml` | `app` | `http://app:8000` (set in compose) |
| the commander's `demo/compose.yml` | `api` | `http://api:8000` (the image default) |
| ECS | — | unused; the ALB routes `/api/*` before nginx sees it |

One image, two stacks, one variable. The upstream is held in an nginx *variable* so
the name is resolved per request rather than at config load — which is what makes a
value naming a host that does not exist cost nothing until a request arrives
(WO-R2-65).
