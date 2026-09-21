# The `/demo` page — an agent-run dashboard

What is on the screen, where every number comes from, and what the two principals
can and cannot see.

An operator opens one page, records it, and narrates an incident from injection to
resolution without switching tabs. WO-R3-313 built the first version, WO-R3-327
added the reset boundary, **WO-R3-330 rebuilt it around one run** after the owner's
first live take, **WO-R3-334 made it read one TAKE** after the third, and
**WO-R3-336 made it start each take at zero and read the run as it happens** after
the fourth — see "What the takes showed" at the end, which is the whole reason the
page has the shape it does.

The other half of the demo — the step machine that fires the fault, runs the agent
and resets the world — lives in the commander repo (`scripts/demo_live.py`,
`docs/demo-runbook.md`, WO-R3-329). This file is only about the screen.

| | |
|---|---|
| **Route** | `/demo`, `ProtectedRoute requiredRole="support"` — the same bar as `/admin`, because everything on it is operator-only |
| **Mode** | `?mode=consumer_outage` (default) or `?mode=dlq_backlog`; the header buttons write the URL |
| **Take** | the span between two `lab.world_reset` boundaries, and **the default is the take now running** (WO-R3-336). The take selector offers it first and the earlier takes as **history**, each labelled `start → end · scenario · outcome`; a closed take is labelled `take ended at HH:MM:SS` and, when it is on screen, `history, chosen from the take selector` |
| **Run** | `?run=<id>` pins a run and therefore its take. Without it the page reads the **newest run of the take now running**, or none — and adopts a run on the poll after its first report. The run selector lists that take's own runs |
| **Cadence** | every panel polls every **2 s** through `usePolling`, so no two panels disagree about *now*; alerts and breakers poll at 10 s |
| **Code** | `frontend/src/pages/DemoPage.tsx`, with every derivation in `frontend/src/utils/demoPhase.ts` (the incident and its takes) and `frontend/src/utils/demoRun.ts` (the run record and the ledger) — all pure functions, all driven from fixtures in `src/test/demoPhase.test.ts`, `src/test/demoRun.test.ts` and `src/test/DemoPage.test.tsx` |
| **Backend** | the shapes WO-R3-328 added to `agent_runs` and to the read endpoints (plat #230, → v0.6.16): the ranked `hypotheses`, `plan`, `verification`/`verifications`, `steps` with `steps_dropped`, `budget`; `GET /admin/agent-runs/{id}/steps?after_seq=`; the 15-minute lag window with `sample_window_seconds` / `sample_interval_seconds`; comma-list `action_prefix` and `exclude_prefix` on the human audit filter |

Layout, at 1440×900 with no scroll for the top half:

```
  header      take selector · run selector · which take · mode · T+ since the fault
  PLATFORM    healthy → fault injected → paged → agent acting → recovered
  AGENT       triage → investigating → planning → awaiting approval
              → remediating → verifying → resolved | escalated | failed
  ┌───────────────┬─────────────────────────┬──────────────────┐
  │ metric chart  │ the agent               │ action ledger    │
  │ (the take)    │ what it thinks NOW      │ newest at the TOP│
  │ + readings    │ plan · verify · budget  │ THINK + lab + W  │
  └───────────────┴─────────────────────────┴──────────────────┘
  briefing (when it lands) · DLQ table (dlq mode)
```

---

## Seven rules the page is built around

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
4. **A reset is a boundary, a take is the span between two of them, and a fresh take
   starts at zero** (WO-R3-327, WO-R3-334, WO-R3-336) — see below.
5. **An absence is named, not filled.** A run with no `hypotheses` is not a run
   that ranked nothing: the panel says which of the two it is looking at, because
   a stack older than v0.6.16 and a commander older than WO-R3-329 are different
   absences and both are possible. And an absence a **terminal** run will never
   fill is a sentence rather than a "not yet": "the agent handed off without
   acting", "no verification because no action".
6. **The whole page reads ONE take, and only this run's own calls** (WO-R3-334).
   Every panel is scoped to the take the selected run belongs to, and every
   `agent.tool_invoked` row is measured against that run's `service_account_id`.
   A call by the demo runner or by the evaluator's guard probes is counted, named
   and hidden — never drawn as the agent's work.
7. **The fault is the take's FIRST successful injection, and a probe is never a
   fault** (WO-R3-336) — see below. Everything measured from the fault is measured
   from that one row.

---

## A take is the span between two boundaries

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

### "Since the newest boundary" was the wrong unit (WO-R3-334)

The third live take was recorded, the runner wound the world down, and the page was
reloaded two minutes later — so the newest boundary was **after** the run. Read
"since the newest boundary", the page then described a freshly wiped world: the
header said *no run in this take yet*, the PLATFORM row said *healthy · since the
reset*, and the AGENT row beside it said *escalated*. Every number was true of some
moment and none of them were true of the same one.

So a **take** is the span between two boundaries, a run belongs to exactly one of
them, and the page reads that run's take end to end:

| | |
|---|---|
| **Which take** | the take of the selected run. The default run is the newest one **with a fault row in its own take** — not the newest run, and not the newest boundary. `?run=` picks a run and therefore its take the same way |
| **Its rows** | strictly after the opening boundary, up to **and including** the closing one (the reset writes its row last, so a row sharing that timestamp belongs to the take being closed). The ledger also asks for the opening boundary, because it draws a divider at each edge |
| **Its runs** | the runs that **started** inside it; the selector still lists every run the page can see, each labelled with its own take |
| **When it ended** | the header says `take ended at HH:MM:SS`, and adds *a newer take is running with no run yet* when that is the case. The `T+` clock stops at the boundary rather than counting on into a world that is gone |
| **No boundary in view** | `startAt` is null and the take is open at that end — the same honest fallback a stack older than WO-R3-327 has always had. A boundary is never inferred |

Everything derived reads that take: the platform row and the clock, the metric's
breach and recovery, the chart's span and markers, the ledger, the counts and the DLQ
badges.

### A fresh take starts at zero (WO-R3-336)

WO-R3-334's default — *the newest run with a fault row in its own take* — is the right
answer to "the page was reloaded after the wind-down" and the wrong answer to what the
demo actually does: **start a take.** On the owner's fourth take the page opened on the
take before the one running, so the first thing on screen was history.

| | |
|---|---|
| **The default** | the take **now running**: the span after the newest `lab.world_reset` row, run or no run |
| **With no run yet** | the PLATFORM row reads this take's own rows (healthy, then fault injected, then paged), the AGENT row is empty with *waiting for this take's run*, the panel says what it is waiting for and since when, the chart's axis starts at the boundary and the ledger holds this take's lab rows and nothing else |
| **Adoption** | the choice is re-evaluated on **every poll**, not latched at load, so the page picks the run up on the poll after its first report — and moves on to the next take the moment a new boundary appears |
| **History** | the take selector lists the earlier takes with runs as `history · HH:MM:SS → HH:MM:SS · scenario · outcome`. Choosing one pins its newest run (`?run=`), which is what makes it a deliberate act; choosing the live take clears the pin |
| **`?run=`** | still wins, and still opens that run's take. A `?run=` naming a run the page does not have falls back to the current take rather than emptying the screen |

One bug this closed on the way: the run-detail poll parks when the selection has no
run, and a parked poll keeps its last answer — so the page went on rendering the
previous take's run in the agent row. The detail is used only when its id is the
selected run's.

### The fault is the take's FIRST successful injection (WO-R3-336, item 7)

The fourth take's chaos rows, from the platform's own audit stream:

```
  03:18:05.952  chaos.tool_invoked  kill_consumer   success              ← THE fault
  03:19:48.552  chaos.tool_denied   inject_latency  (guard, labelled)
  03:19:48.573  chaos.tool_invoked  inject_latency  error, labelled
  03:19:48.592  chaos.tool_invoked  kill_consumer   success              ← a re-arm
```

The page anchored on the **newest** `chaos.*` row, so `injected 08:19:48`, `T+ 43.9 s`,
`fault injected · 17 ms`, the chart's F marker and `agent acting · 58 s` were all
measured from a re-arm that happened **1 m 43 s after the fault** — and the two
refusals were drawn in amber as if the lab had injected them.

A **fault row** is now all three of:

- `chaos.tool_invoked` — a refusal (`chaos.tool_denied`) is not a fault, the hook never ran;
- carrying no `lab_probe_reason` — a hook the evaluator fired to prove a guard refuses it
  is the lab probing, not the lab injecting ([ADR 0038](ADR/0038-a-probe-by-the-lab-is-labelled-by-the-lab.md)
  lets a chaos row carry that label);
- not a failed invocation — `outcome` present and anything but `success` means it raised.
  An **absent** `outcome` is a row that did not say, and is read as an injection: the
  other reading would let a stack that stops writing the field report a healthy world
  through a fault.

The take's **first** such row is the anchor for the fault station, `injected HH:MM:SS`,
the `T+` clock, the chart's F marker and `agent acting fired after N reads`. Later ones
are extra F markers, labelled `<hook> re-armed` when the hook and its arguments match
and named plainly when they do not — a re-arm is not a new incident, and the world was
already broken. Every other chaos row is hidden behind the *other reads* toggle and
badged `LAB PROBE`.

### The platform pages, and the row says so (WO-R3-336, item 8)

Until v0.6.18 the alert the agent triaged was synthesized by the scenario's YAML
(`alert:` block, fingerprint `consumer_stalled`) and the platform's own alert stream
never moved — `list_active_alerts` read the same three seeded rows before, during and
after the fourth take. The platform raises it itself now, on its own metric and its own
clock (platform WO-R3-338, owner decision O-36), and audits one `alert.raised` row per
episode.

So the PLATFORM row has a fifth station, **paged**, between `fault injected` and
`agent acting`: stamped with the take's first `alert.raised` row, carrying that alert's
fingerprint and summary under it, and reading `not paged` when the platform raised
nothing. The `T+` clock stays anchored on the **fault** — the page is how long the
platform took to notice, not a second incident. The row is drawn in the ledger too,
badged `PAGED`. The audit query asks for `alert.` beside the other three streams; that
prefix is **not** withheld from the agent, because the agent may see its own alert.

### Whose call was it?

A second rule with the same shape, from findings F3 and F4 of the third take. The
demo runner built two of its clients with the **agent's** token and read lag every
three seconds; the evaluator's principal-guard probes and its world audit wear that
token on purpose, and fired seven more calls after the boundary. Both are real calls
the platform really served, and neither is the agent's work.

| Row | How the page tells | What it does with it |
|---|---|---|
| `agent.tool_invoked` with the run's `service_account_id` | the run record names its own principal | the run's own call |
| `agent.tool_invoked` with any other principal | principal comparison | hidden, counted, badged `NOT THIS RUN` on the toggle |
| `lab.probe` (WO-R3-333) | the lab labels its own reads, because it cannot be told apart by principal | hidden, counted, badged `LAB PROBE` on the toggle |
| no run selected | there is no principal to compare against | every row counts, which is the honest reading rather than a guess |

The platform row's `agent acting` station, the chart's action markers and the
ledger's "N calls the platform recorded" all use that rule.

### The fault is latched for the take

The boundary alone was not enough. The fault is the newest `chaos.*` row *in the
page's window of rows*, and with `make traffic` running the window is mostly job
events — so the row that states the fault fell out of it within a minute and the
platform row dropped back to **healthy** in the middle of the run.

Two fixes, both needed. The audit query asks for the operator streams only (below),
so the row survives far longer. And the fault is **latched**: once a `chaos.*` row
has been seen in this take the page holds its timestamp, releasing it only for a
newer fault row or a new boundary. `platformPhase`/`platformRow` take that latched
value as an input (`faultAt`), and a latched fault at or before the boundary is
dropped, so a latch can never outlive its take.

---

## The two rows

### PLATFORM — `healthy → fault injected → paged → agent acting → recovered`

Five stations, each carrying its own timestamp and, once passed, its duration.
Source: `GET /api/v1/audit/logs` (the operator streams) plus the mode's metric.

| Station | Reached when | Stamped with |
|---|---|---|
| healthy | always — it is where every take starts | the boundary (`lab.world_reset`), which is when this world began |
| fault injected | a **fault row** exists in this take, **or the latch holds one** | the take's first injection (above), never a probe and never a re-arm |
| paged | an `alert.raised` row exists in this take | its first one; the note is the alert's fingerprint and summary, or `not paged` |
| agent acting | an `agent.tool_invoked` row **by this run's own principal** at or after the fault | the first such row; the note says how many reads, and names the Tier-1 action once one fires |
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

**A late report reads as late** (WO-R3-334, finding F2). The third take's three
stations were all stamped `08:17:59` with durations of 76 ms / 9 ms / 0 ms, so a run
that took 41 seconds rendered as one that took 85 milliseconds: the reporter queued
its step reports and flushed them on the next transition, in one burst carrying the
original timestamps. The event's time is the truth about the run and stays the
station's stamp; the arrival is the truth about the reporting, and each station now
also says `reported HH:MM:SS` when its report landed **more than five seconds** after
the event. The arrival comes from the `agent.run_reported` audit rows, whose
`created_at` is the platform's own clock and whose `extra_data.arguments` carry the
run id and the state (ADR 0035).

**Stations advance one at a time.** A burst that arrives in one poll reveals one
station per ~300 ms, because four stations lighting in a single frame does not read
as a run advancing — it reads as a page catching up. A run the page is *opening* on
is shown whole: it has already happened, and replaying it on every reload would be
theatre rather than information.

The line under the two rows states both readings in plain words, and adds the
metric's own caveats: recovery pending on one sample, or no reading at all.

---

## The metric chart (left)

One series, one axis, and the axis is **the take**.

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

**And the window now survives `make eval-reset`** (WO-R3-333, [ADR 0038](ADR/0038-a-probe-by-the-lab-is-labelled-by-the-lab.md)).
The reset used to delete it as residue, so the chart opened on nought to two points
while the fault it exists to show was climbing 0 → 10 → 30 — the third live take's F5.
The samples are history, each carrying its own `measured_at`, and the reset's
`lab.world_reset` row is the boundary to read them against; `lag_samples_cleared` is
still in the reset's summary, permanently `0`. So the first frame the operator sees can
hold the climb rather than the two measurements that outlived the sweep.

**DLQ depth has no server-side history** — the endpoint is one number — so in
`dlq_backlog` mode the line is what *this page* has observed since it opened, and the
caption says so. It is a second **chart**, never a second series on the lag axis: the
two run to different magnitudes and one plot with two scales invents a relationship
between them.

**The x-axis is the take's span, not a fixed fifteen minutes** (WO-R3-334). The third
take's fault climbed 0 → 10 → 30 in about two minutes and a fifteen-minute axis drew
it in the last eighth of the plot, from two samples. The span is **two minutes before
the fault to now** — or to the closing boundary on a take that has ended, where the
right edge is labelled `take ended` rather than `now`. Two clamps: never narrower
than five minutes (a take one tick old is not a chart of one point) and never wider
than the history the platform actually holds (an empty stretch of axis reads as a
flat line). The `full window` button zooms back out to that whole history, and
`zoom to the take` returns.

Drawn on the plot:

- a **band** above the threshold, with the threshold line labelled **on the left**,
  where the eye starts and no marker can cover it;
- **y ticks at round numbers with zero always drawn**, because zero is the line a
  viewer measures a recovery against;
- **markers, and the set is closed**: the boundaries (W), the lab's fault (F), each
  Tier-1 **action** the run took (A) and the **recovery** (R), each listed underneath
  with its glyph and its time, so identity is never colour alone. Reads are
  deliberately *not* marked — fifteen ticks is a comb, and the ledger is where every
  call belongs — and neither are the runner's or the evaluator's calls: the third
  take drew three blue `A` markers for `mark_dlq_permanent` and `get_cache_key_info`
  calls the **evaluator's guard probes** had made, on a take where the agent never
  acted at all;
- a **cursor on the right edge** with the newest reading beside it, so the end of the
  line has a number on it without hovering;
- a **crosshair and tooltip** on hover, and a `samples (N)` table behind the caption
  with every value in it, so nothing is reachable only by hovering.

The plot gets the panel minus **two lines of caption**: the threshold and the zoom
control on one, the markers and the samples toggle on the other.

Actions come from the run's `steps` when it has them and from its own
`agent.tool_invoked` audit rows when it does not.

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
| **what the agent thinks now** | the newest ranking on top — `hypotheses[]` (name, category, confidence bar, reasoning excerpt ≤ 280 chars), stamped with the newest planner call's time and `seq`, with its chosen next action and reason under it. The **top one is shown whole**; the rest truncate. The bar carries a tick at **0.7**, the confidence the loop gates a remediation on. Below it a **confidence sparkline** (one point per planner call, with the 0.7 line and every value printed) and the **earlier rankings**, collapsed, each with its own timestamp | "None reported yet". Where only `current_hypothesis` exists it becomes a one-entry list **and the panel says so**: that is a commander older than WO-R3-329, not a run that ranked nothing. A ranking entry with no confidence gets a sentence, never a bar at zero |
| **the plan** | `plan` — tool, arguments, the hypothesis it is aimed at, the rationale excerpt | "No action planned yet" while the run is live; on a **terminal** run, "the agent handed off without acting" |
| **verification** | `verifications[]` — every verify poll's verdict, attempt *n* of *m*, and its reasoning; capped at 50, and the panel says so when it is full | "Nothing verified yet" while the run is live; on a terminal run that never acted, "no verification because no action" |

**The panel is a timeline since WO-R3-336.** The fourth take's run called its planner
three times during a 22-second investigation and the platform saw the first two rankings
only at the end: `hypotheses` rode on transition reports and no transition happens inside
an investigation, so the panel went from empty to finished in one poll. The commander now
reports one `report`-kind step per planner call (WO-R3-337, `tool` =
`investigation_planner` / `reflection` / `verify_judge`, `arguments` = the ranking, the
chosen `next_action` and a reason), and this panel reads them: the newest ranking with its
reasoning whole, the confidence over the planner's own calls, and the older rankings
underneath with their times. The run record's `hypotheses` is still the head of the list —
it is the latest reading by definition and the only one carrying the reasoning excerpts.

The third take's screenshot truncated the **top** hypothesis with "more…", so the one
sentence explaining why the agent believed what it believed was the one sentence not
on screen. It is shown whole now. The 0.7 tick is there for the same reading: that
take's top hypothesis sat at 0.75–0.82 for five steps, over the bar, in a category
with a Tier-1 fix — and the agent still did not act (`../context/INCIDENTS.md`,
INC-004). The bar says so at a glance.

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

One row per **step**, **one line each**, **newest at the top** (the owner's rule from
the fourth take, reversing WO-R3-334's transcript order):

```
  08:19:52  THINK   planner            → top consumer_saturation 0.85 → probe get_consumer_lag
  08:19:44  READ    get_consumer_lag   → lag 30, known
```

the time, a kind badge (READ grey / ACTION blue / THINK purple / REPORT purple), the
tool, and what the call **answered**. Click a row for its arguments, the whole result excerpt, its
sequence number, its outcome and its latency. An **ACTION row is highlighted and
never collapsed** — the action is the point of the run, so it shows its arguments and
its result without being asked.

The summary after the arrow comes from a per-tool table, because "lag 30, known" is a
sentence and the first sixty characters of a JSON body is not:

| Tool | Summary |
|---|---|
| `get_consumer_lag` | `lag 30, known` / `lag unknown` |
| `list_dlq_messages` | `DLQ total 5` |
| `get_circuit_breakers` | `bulk-api-sync open` / `3 breakers, none open` |
| `get_cache_key_info` | `exists, 512 bytes` / `absent` |
| `restart_consumer_group` | `accepted, kill key cleared` |
| `replay_dlq_*` | `replayed 1, scheduled 0` |
| `mark_dlq_permanent` | `fenced` |
| anything else | the excerpt itself, collapsed to one line and cut — better read than hidden, and the whole thing is one click away |

Each one reads the parsed excerpt where it can and the text where it cannot (a
400-character excerpt of a longer body is usually truncated JSON, which is not
parseable and not a defect), and falls through to the generic line rather than
inventing a reading.

**Newest at the top, and the panel pins there** — the newest row is the one the eye
wants first. Scroll down into the history and it stops following; a `newest ↑` button
brings it back.

**A planner call is a THINK row** (WO-R3-336, item 3). The `report`-kind steps whose
tool is `investigation_planner`, `reflection` or `verify_judge` are the agent thinking,
not calls it made: the row shows the one readable sentence the step carries, a click
opens the ranking it accepted (name, category, confidence bar) and the next action it
chose with its reason, and the expanded row says *the agent thinking, not a call* —
because a planner call spends no budget and writes no `agent.tool_invoked` row.

**The ledger pages back to the take's opening boundary** (item 2). The fourth take's
ledger showed a `kill_consumer` from the take *before* the one on screen and the header
said `take start not in view`: the boundary was past the end of the single page of 100
rows the page asked for, so there was no boundary to cut the rows at. The audit read now
asks for another page whenever the selected take has no opening boundary and the server
says there are more rows, up to **five pages**; past that it says so in a line above the
rows rather than drawing them as if they were this take's. The page count only ever
grows within a session — dropping back to one page would lose the row that found the
boundary and the page would oscillate between two takes on a two-second cadence.

That excerpt is the whole reason the ledger is built from steps: an
`agent.tool_invoked` audit row carries tool, arguments, latency and outcome but
**no result**, so "what did the agent see" could not be shown from the audit log at
all. Where a run reported no steps the ledger falls back to those rows, labels
itself as doing so, and the rows say plainly that the audit log records no result.

**A row the evaluator produced is no longer in that stream** (WO-R3-333,
[ADR 0038](ADR/0038-a-probe-by-the-lab-is-labelled-by-the-lab.md)). The principal
guards and the world audit call the platform under the agent's own token on purpose,
and their calls used to land in `agent.tool_invoked` — which is how the third live
take's ledger came to show seven reads the agent never made. Those calls now carry
`lab.probe`, in the `lab.` stream beside the boundary row. The page's audit poll asks
for `agent.,lab.,chaos.`, so the rows still arrive: excluding them from the ledger and
from the phase strip's "agent acting" station, behind a grey **evaluator probe** toggle
that is off by default, is WO-R3-334's half.

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
| a **fault row** (`chaos.tool_invoked`, successful, unlabelled) | amber | the lab's tool and arguments — exactly what was fired |
| any other `chaos.*` row — a refusal, a hook that raised, a labelled probe | grey | **hidden**, counted, badged `LAB PROBE` on the toggle |
| `alert.raised` | red | badged `PAGED`, named by the alert's fingerprint with its summary beside it |
| `lab.world_reset` | grey | a **divider** across the ledger, `world reset · HH:MM:SS`, not an event. Every boundary in the window gets one, so two takes' rows are never silently mixed |
| `principal_type: user` | green | a human's own action |
| `event.*` | grey | **off by default**, one toggle |

`chaos.*` is matched on the **action**, not the principal: the evaluator is a service
account too, so a principal-only test would file the lab's rows under "agent" and make
the fault look like the agent's doing. The agent's own `agent.tool_invoked` and
`agent.run_reported` rows are dropped while steps exist — they are the same events
without their results, and drawing both would double every row.

**The reads that are not this run's are counted and hidden** (WO-R3-334): a line
saying *"N evaluator/traffic reads hidden — the lab's probes and other principals'
calls"*, with a toggle that shows them badged `LAB PROBE` and `NOT THIS RUN`. The
third take's ledger was a wall of `get_consumer_lag` every three seconds under the
agent principal, none of it the agent's, and its count line read *"0 steps reported ·
89 calls the platform recorded — the two do not agree"* over a four-call run. The
comparison is only meaningful between the run's steps and the run's own rows.

**The job events are the toggle that made this page watchable.** With traffic
running, `event.job.*` was 43 of the 50 rows on screen and the four rows the demo is
about were underneath them. The page therefore asks for
`action_prefix=agent.,lab.,chaos.` (a comma list since WO-R3-328) and fetches
`action_prefix=event.` only while the toggle is on — so the job stream can never
crowd out a derivation.

**Both witnesses are counted**, in one line above the rows: *N steps reported · M calls
the platform recorded*, where N is the run's `read` and `action` steps and M is its own
principal's `agent.tool_invoked` rows. The planner's reports are counted **beside** them
(*K planner calls, which make none*), never in them.

**The disagreement warning has to earn itself** (item 4). The fourth take's page said
*"4 steps reported · 5 calls the platform recorded — the two do not agree"* about a run
that had reported everything it did: the fifth call was the eval runner's own
precondition probe, unlabelled at the time (WO-R3-337 labels it). The warning now needs
all three of **more rows than steps**, a **terminal** run, and **ten seconds** of
silence since its last report. More steps than rows is a report the audit page has not
caught up with; a live run is always one report behind; a run that finished a second ago
is still flushing. On camera a false warning is worse than none.

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
  rather than in prose; a run that never acted reads *"no attribution because no
  action"* rather than "none recorded", which would suggest a gap in the record
  instead of the consequence of the run's own decision;
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
| `alert.raised` — the platform paging on its own metric | **visible**, deliberately: the agent may see its own alert, and the `alert.` prefix is not withheld (platform WO-R3-338) | visible |
| `lab.world_reset` — the boundary | **withheld**, same mechanism and condition (2026-09-20 amendment) | visible, payload included |
| `agent_runs` — its own reported run, steps, plan, budget | **not readable at all**. There is no read tool, by design ([ADR 0035](ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md)); the write tools are called by the loop's checkpoint hook, not chosen by the model, and are excluded from the planner surface | visible |
| Consumer lag | `get_consumer_lag` (one group per call) | `GET /admin/consumer-lag` (every group, same 15-minute samples) |
| DLQ rows | `list_dlq_messages` | `GET /admin/jobs?status=dead_letter`, with `remediation_hint` / `dead_lettered_at` / `fenced_at` / `fenced_by` / `triage` |
| Breakers, alerts, SLOs | `get_circuit_breakers`, `list_active_alerts`, `get_slo_status` | the `/admin` twins of the same readings |

So: the agent never learns *that it was a lab*, and never sees what it told the
platform about itself. The operator sees both.

---

## What the takes showed

### The fourth take (2026-09-20, $0.25) — green, and still not watchable live

The run resolved and every number on the page was measured from the wrong moment or
arrived in a burst. Eight findings, each a rule above:

1. **The page opened on the take before the one running**, because the default was "the
   newest run with a fault in its own take" and that run was in the previous take. → a
   fresh take starts at zero and adopts its run on the next poll; earlier takes are
   history, chosen explicitly.
2. **The ledger showed a `kill_consumer` from the take before**, and the header said
   `take start not in view`: the boundary was past the end of the one page of rows the
   page read. → the audit read pages back to the take's opening boundary, and says so
   when it cannot reach it. The ledger is also **newest at the top** now, which is the
   owner's rule.
3. **Three planner rankings existed during a 22-second investigation and none was
   visible until the last**, because `hypotheses` rode on transition reports. → each
   planner call is a `report` step (WO-R3-337), a purple THINK row in the ledger, and
   the hypotheses panel is *what the agent thinks now* with a confidence sparkline and
   the older rankings underneath.
4. **"4 steps reported · 5 calls the platform recorded — the two do not agree" was
   false**; the fifth call was the runner's own precondition probe. → the warning needs
   more rows than steps, a terminal run, and ten seconds of silence.
5. **The stations read `investigating · 10 ms` and `planning · 21.9 s`** for a run that
   investigated for 22 seconds, because every transition stamped the *iteration's* start
   time. → fixed at the source by WO-R3-337; the console's durations, late-report labels
   and one-at-a-time reveal are unchanged and now describe real times.
6. **Everything fault-relative was measured from a re-arm** 1 m 43 s after the real
   injection, and two refused guard probes were drawn as faults. → a fault row is a
   successful, unlabelled `chaos.tool_invoked`, and the take's **first** one is the
   anchor; later ones are extra markers labelled `re-armed`.
7. **The platform never paged.** The alert was canned in the scenario's YAML. → the
   platform raises it itself (WO-R3-338) and the PLATFORM row has a `paged` station
   between the fault and the agent.

### The third take (2026-09-20, $0.21)

The owner recorded it, the runner wound the world down, and the page was reloaded two
minutes later. Six findings, each a rule above:

1. **The page read the wrong take.** The newest boundary was *after* the run, so the
   header said "no run in this take yet" and the PLATFORM row said `healthy · no
   fault yet` beside an AGENT row that said `escalated`. → a take is the span between
   two boundaries, and the page reads the take of the run it is showing.
2. **A 41-second run rendered as an 85-millisecond one.** The reporter queued its
   step reports and flushed them in one burst carrying their original timestamps. →
   each station says when its report *arrived* when that was late, and stations
   advance one at a time.
3. **89 calls, four of them the agent's.** The demo runner read lag every three
   seconds under the agent's token and the evaluator's guard probes fired seven calls
   after the boundary. → rows are matched against the run's own principal, `lab.probe`
   rows are the lab's, and the excluded count is on screen with a toggle.
4. **Three blue action markers on a take where the agent never acted** — the same
   probes, read as Tier-1 actions. → the marker set is closed and principal-scoped.
5. **The chart drew a two-minute incident in the last eighth of a fifteen-minute
   axis**, from two samples (the reset was clearing the lag history; platform
   WO-R3-333 keeps it). → the axis is the take, with a zoom-out to the full window.
6. **The ledger was raw audit rows and the top hypothesis was truncated with
   "more…".** → one line per call with what it answered, and the top hypothesis whole.

`plan`, `verification` and `attribution` were all null, and all three were correct:
the agent handed off without acting. Those absences are sentences now.

### The first take

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
