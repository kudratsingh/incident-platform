/**
 * `/demo` — a dashboard of one agent run (WO-R3-330, rebuilt from WO-R3-313).
 *
 * The owner recorded the first take of this page and it was unwatchable. Six
 * findings, and every one of them is a design rule here rather than a tweak:
 *
 *  1. **The strip switched witnesses.** One row of stations with two markers meant
 *     that the moment a run existed, the agent's word was the only thing lighting
 *     anything — so `fault injected`, which only the platform can assert, was never
 *     on screen. There are two rows now, always both, never merged.
 *  2. **The agent panel was empty.** The run record carried one hypothesis and one
 *     step, both filled at the end. It now carries the ranked list, the plan, every
 *     verify verdict, the budget and a step per call (WO-R3-328), and that panel is
 *     the middle of the page because it is the point of the page.
 *  3. **The timeline was the traffic loop.** 43 of 50 rows were `event.job.completed`.
 *     The audit query asks for the operator streams only (`agent.,lab.,chaos.`) and
 *     job events are one toggle, off.
 *  4. **No audit row carries a result.** So the ledger is built from `steps`, which
 *     carry a result excerpt per call; the audit rows are the fallback, and the page
 *     says so when it is using them.
 *  5. **The chart was five points over five minutes.** It is the platform's own
 *     15-minute window now, with the fault, each action and the recovery marked on it.
 *  6. **Everything was too small.** Every number on this page is labelled and large
 *     enough to read in a recording.
 *
 * Two rules from the first build are unchanged and still load-bearing. **An absent
 * reading renders as absent with its reason, never as zero** (ADR 0030 in the UI).
 * **A reset is a boundary and nothing older than the newest one is derived from**
 * (WO-R3-327) — with one addition: the fault is now *latched* for the take, because
 * the row that states it falls off a page of audit rows long before the incident is
 * over, and the first take fell back to `healthy` in the middle of the run.
 *
 * One bug fixed in passing, and it cost the first take its ending: the page asked
 * for `agent-runs?active=true`. A run's terminal report stamps `finished_at` and the
 * briefing lands in the same breath, so the run — and the briefing card with it —
 * disappeared within one poll of resolving. It reads every run and picks by the
 * boundary, so a finished take stays on screen.
 *
 * What the two principals can see still differs, and that difference is the demo:
 * this page reads REST as a human operator, so it sees the `chaos.*` rows the
 * agent's MCP withholds (ADR 0012), the `lab.world_reset` boundary beside them, and
 * the `agent_runs` the agent cannot read at all (ADR 0035).
 *
 * ── WO-R3-334: the page reads ONE take ──────────────────────────────────────────
 *
 * The third live take was recorded, wound down, and the page reloaded two minutes
 * later. Everything on screen was true of a different moment, and the six fixes here
 * all come from that:
 *
 *  1. **A take is the span between two boundaries.** "Since the newest boundary" put
 *     the boundary AFTER the run, so the PLATFORM row read `healthy · no fault yet`
 *     beside an AGENT row that read `escalated`. The page now picks the newest run
 *     with a fault in its OWN take and reads that take end to end — the platform
 *     row, the clock, the chart, the ledger and the badges.
 *  2. **A late report reads as late.** Three stations stamped 08:17:59 with 76 / 9 /
 *     0 ms rendered a 41-second run as an 85-millisecond one, because the reporter
 *     queued its reports and flushed them in one burst with their original times.
 *     Each station now also says when its report ARRIVED, when that was later, and
 *     stations reveal one at a time instead of all lighting in one frame.
 *  3. **Somebody else's reads are not the agent's.** The runner read lag every three
 *     seconds under the agent's token, and the evaluator's guard probes fired seven
 *     more calls after the boundary, so the ledger said "89 calls" for a four-call
 *     run. Rows are matched against the run's own `service_account_id`, `lab.probe`
 *     rows (WO-R3-333) are the lab's, and the excluded count is on screen with a
 *     toggle rather than silently dropped.
 *  4. **The ledger is a transcript.** One line per call with what it answered
 *     (`get_consumer_lag → lag 30, known`), the rest behind a click, actions never
 *     collapsed, newest at the bottom.
 *  5. **The chart is the take.** A fifteen-minute axis drew the incident in its last
 *     eighth; the span is now two minutes before the fault to now, the markers are
 *     only the four moments that matter, and there is a cursor on the right.
 *  6. **An absence a terminal run will never fill is a sentence, not a blank.**
 *
 * ── WO-R3-336: the page starts each take at zero and reads the run as it happens ──
 *
 * The fourth take was green and still not watchable live. Five more rules, all from
 * what the owner saw and what the platform's own record of that run said:
 *
 *  1. **A fresh take starts at zero.** The default take is the take NOW RUNNING, not
 *     the newest run's take: on the fourth take the page opened on the take before it,
 *     so the first thing on screen was history. The choice is re-evaluated on every
 *     poll, so the page adopts a run the moment it reports and moves on the moment a
 *     new boundary appears. Earlier takes are offered as history, explicitly labelled.
 *  2. **The ledger is newest at the TOP** (the owner's rule, reversing WO-R3-334), and
 *     its audit read pages back until it holds the take's opening boundary — the
 *     fourth take's ledger showed a `kill_consumer` from the take before, because the
 *     boundary was past the end of the one page of rows the page asked for.
 *  3. **The agent's thinking is a live timeline.** Each planner call arrives as a
 *     `report`-kind step (WO-R3-337) and renders as a purple THINK row whose click
 *     opens the ranking and the chosen next action; the hypotheses panel is "what the
 *     agent thinks now" — the newest ranking with its top cause's full reasoning, a
 *     confidence sparkline over the planner's calls, and the older rankings below it.
 *  4. **The counts warning has to earn itself.** "4 steps reported · 5 calls the
 *     platform recorded — the two do not agree" was false: the fifth call was the
 *     runner's own precondition probe. The warning now needs more rows than steps, a
 *     terminal run, and ten seconds of silence.
 *  5. **The stations are unchanged and still load-bearing** — real durations from
 *     `phase_history` (which WO-R3-337 fixes at the source), the late-report label, and
 *     one station at a time.
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Layout from '../components/Layout'
import ErrorState from '../components/ErrorState'
import { useToast } from '../components/Toast'
import { copyToClipboard } from '../components/TraceId'
import { adminApi } from '../api/admin'
import { usePolling } from '../hooks/usePolling'
import { JOB_TYPE_LABELS } from '../utils/format'
import {
  MODE_METRICS,
  agentRow,
  agentStateLabel,
  chartMarkers,
  chartWindow,
  faultInTake,
  faultRowsInTake,
  isDemoMode,
  metricRecovery,
  platformRow,
  reportArrivals,
  rowsInTake,
  rowsInTakeWithEdges,
  selectTake,
  takeKey,
  takeOptions,
  yAxisTicks,
} from '../utils/demoPhase'
import type {
  AgentStation,
  ChartMarker,
  DemoMode,
  MetricSample,
  PlatformStation,
  Station,
  Take,
  TakeOption,
} from '../utils/demoPhase'
import {
  budgetMeter,
  buildLedger,
  confidenceTrend,
  dlqDecisionFromSteps,
  hypothesesSource,
  isThinkStep,
  labToolName,
  lastReportAt,
  ledgerCounts,
  mergeSteps,
  plannerReport,
  rankingHistory,
  runVerifications,
  summariseStep,
} from '../utils/demoRun'
import type {
  ConfidencePoint,
  LedgerEntry,
  LedgerKind,
  RankedCause,
  RankingSnapshot,
} from '../utils/demoRun'
import type {
  AgentBriefing,
  AgentBriefingSlot,
  AgentRun,
  AgentRunStepRecord,
  AgentRunVerification,
  Job,
} from '../types'

/** Everything on this page shares one cadence, so the panels cannot disagree about "now". */
const POLL_MS = 2000
/** Alerts and breakers move far more slowly than lag does. */
const SLOW_POLL_MS = 10_000
/** The group the `consumer_outage` scenario is about. */
const DISPATCHER_GROUP = 'worker-dispatcher'
/**
 * The chart's window when the reply does not state one.
 *
 * `/admin/consumer-lag` carries `sample_window_seconds` and
 * `sample_interval_seconds` since WO-R3-328, and the chart labels its axis from
 * those; this is the fallback for an older stack, not the number to trust.
 */
const WINDOW_MS = 15 * 60 * 1000
/** The platform's cap on `verifications` — past it the oldest are dropped. */
const VERIFICATIONS_CAP = 50
/** One station at a time, so a burst of reports reads as a sequence (WO-R3-334). */
const STATION_REVEAL_MS = 300
/** The bar a hypothesis has to clear before the loop may act on it (ADR 0009). */
const REMEDIATE_THRESHOLD = 0.7
/** Enough rows for a whole take once the job events are out of the way. */
const AUDIT_ROWS = 100
/**
 * How far back the audit read may page to find the take's opening boundary.
 *
 * Five pages of 100 operator rows is about half an hour of a stack with the traffic
 * loop running — well past the platform's own 15-minute metric window, which is the
 * span the brief bounds this at. Past it there is nothing to find and the ledger says
 * so, because paging back forever on a 2-second poll would cost the demo its cadence.
 */
const MAX_AUDIT_PAGES = 5
/**
 * The three streams this page is about, in one request (WO-R3-328).
 *
 * `agent.` is both `agent.tool_invoked` and `agent.run_reported`; `chaos.` is the
 * lab's faults; `lab.` is the reset boundary; `alert.` is the platform paging on its
 * own metric (WO-R3-338, O-36), which is the station between the fault and the agent.
 * Nothing else on the stack writes anything this page derives from, and everything else
 * is what buried it.
 */
const OPERATOR_STREAMS = 'agent.,lab.,chaos.,alert.'
/** The job lifecycle, fetched only when the operator asks for it. */
const JOB_EVENT_STREAM = 'event.'
const JOBS_STRIP_ROWS = 20
const DLQ_ROWS = 20

// ───────────────────────────────────────────────────────────── small utilities

/** A clock that ticks, so "T+ 1m 04s" moves without a poll. */
function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(t)
  }, [intervalMs])
  return now
}

/**
 * A count that walks up to its target one step at a time.
 *
 * The reporter can queue four step reports and a terminal state and flush them in one
 * burst — on the third take all four arrived at 15:18:40 — and four stations lighting
 * in a single frame does not read as a run advancing, it reads as a page catching up.
 * So the row advances one station per tick.
 *
 * The first target for a key is taken WHOLE, with no animation: a run the page is
 * opening on has already happened, and replaying its stations one by one on every
 * reload would be theatre rather than information.
 */
function useStaggeredReveal(
  key: string | null,
  target: number,
  stepMs: number = STATION_REVEAL_MS,
): number {
  const [state, setState] = useState<{ key: string | null; shown: number }>({
    key,
    shown: target,
  })

  useEffect(() => {
    setState((prev) => {
      if (prev.key !== key) return { key, shown: target }
      // A run that lost stations (a switch, a re-read) snaps rather than counting down.
      return target < prev.shown ? { key, shown: target } : prev
    })
  }, [key, target])

  useEffect(() => {
    if (state.key !== key || state.shown >= target) return
    const timer = setTimeout(() => {
      setState((prev) =>
        prev.key === key && prev.shown < target ? { key, shown: prev.shown + 1 } : prev,
      )
    }, stepMs)
    return () => clearTimeout(timer)
  }, [key, target, state, stepMs])

  return state.key === key ? state.shown : target
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${String(Math.max(0, Math.round(ms)))}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${String(Math.floor(ms / 60_000))}m ${String(Math.floor((ms % 60_000) / 1000)).padStart(2, '0')}s`
}

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** Arguments, compact enough for one line and never `{}` where there are none. */
function compactJson(value: unknown): string | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'object' && Object.keys(value as object).length === 0) return null
  return JSON.stringify(value)
}

function shortId(id: string): string {
  return id.slice(0, 8)
}

// ─────────────────────────────────────────────────────── header: run + mode

/**
 * The runs of the take on screen, newest first.
 *
 * Scoped to one take since WO-R3-336: the take selector beside it is what crosses a
 * boundary, so a list mixing runs from three takes made "which run" and "which take"
 * one question with two answers. A take with no run of its own says so — that is the
 * reading a fresh take is supposed to give.
 */
function RunSelector({
  runs,
  selected,
  onSelect,
}: {
  runs: AgentRun[]
  selected: AgentRun | null
  onSelect: (id: string) => void
}) {
  if (runs.length === 0) {
    return (
      <span data-testid="run-selector-empty" className="text-sm text-gray-500">
        no run reported yet
      </span>
    )
  }
  return (
    <label className="flex items-center gap-2 text-sm text-gray-400">
      <span>Run</span>
      <select
        data-testid="run-selector"
        aria-label="Run"
        value={selected?.id ?? ''}
        onChange={(e) => onSelect(e.target.value)}
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 font-mono max-w-[17rem] truncate"
      >
        {runs.map((r, i) => (
          <option key={r.id} value={r.id}>
            {shortId(r.id)} · {r.scenario ?? 'no label'} · {r.state}
            {i === 0 ? ' · newest' : ''}
          </option>
        ))}
      </select>
    </label>
  )
}

/** One take, as the selector prints it: the live one, or a line of history. */
function takeOptionLabel(option: TakeOption): string {
  const span =
    option.take.startAt === null
      ? 'start not in view'
      : clockTime(option.take.startAt)
  const run = option.run
  const outcome =
    run === null
      ? 'no run yet'
      : `${run.scenario ?? 'no label'} · ${run.state}${
          option.runs.length > 1 ? ` · ${String(option.runs.length)} runs` : ''
        }`
  if (option.current) return `this take · ${span} → live · ${outcome}`
  const end = option.take.endAt === null ? 'now' : clockTime(option.take.endAt)
  return `history · ${span} → ${end} · ${outcome}`
}

/**
 * Which take the page reads — the one now running, or an earlier one as history.
 *
 * The owner's rule from the fourth take: a fresh demo starts at zero and earlier takes
 * are history, so crossing a boundary is an explicit, labelled choice rather than
 * something the default rule does on the operator's behalf. Choosing the live take
 * clears `?run=`, which is what puts the page back on "whatever this take reports
 * next".
 */
function TakeSelector({
  options,
  selectedKey,
  onSelect,
}: {
  options: TakeOption[]
  selectedKey: string
  onSelect: (option: TakeOption) => void
}) {
  if (options.length <= 1) return null
  return (
    <label className="flex items-center gap-2 text-sm text-gray-400">
      <span>Take</span>
      <select
        data-testid="take-selector"
        aria-label="Take"
        value={selectedKey}
        onChange={(e) => {
          const chosen = options.find((o) => o.key === e.target.value)
          if (chosen) onSelect(chosen)
        }}
        // Capped, because a select is as wide as its longest option and a history
        // line is long: uncapped it pushed the run selector onto a second row, which
        // is 47px of the one screen the top half has to fit in.
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 font-mono max-w-[19rem] truncate"
      >
        {options.map((option) => (
          <option key={option.key} value={option.key}>
            {takeOptionLabel(option)}
          </option>
        ))}
      </select>
    </label>
  )
}

/**
 * Which take is on screen, whether it is still running, and whether it is history.
 *
 * "take ended at 08:20" is the one sentence the third take's screen needed: it is
 * what turns a page full of past readings from wrong into history. WO-R3-336 adds the
 * other half — a take on screen because the operator CHOSE it says so, and the live
 * take with no run yet says what it is waiting for, so neither can be mistaken for
 * the other.
 */
function TakeLabel({
  take,
  current,
  hasRun,
  newerTakeRunning,
}: {
  take: Take
  /** True while this is the take now running. */
  current: boolean
  hasRun: boolean
  newerTakeRunning: boolean
}) {
  return (
    <span data-testid="take-label" className="text-xs font-mono text-gray-500">
      {take.startAt === null ? 'take start not in view' : `take from ${clockTime(take.startAt)}`}
      {take.endAt === null ? ' · live' : ` · take ended at ${clockTime(take.endAt)}`}
      {!current && (
        <span className="text-amber-300/80"> · history, chosen from the take selector</span>
      )}
      {current && !hasRun && (
        <span className="text-blue-200/80"> · waiting for this take’s run</span>
      )}
      {!current && newerTakeRunning && (
        <span className="text-amber-300/80"> · the take now running has no run yet</span>
      )}
    </span>
  )
}

/**
 * Time since this take's fault — and no further than the take.
 *
 * A clock that keeps counting on a take that ended at 08:20 is the same class of
 * error as the one WO-R3-327 fixed: a true number about a world that is gone. It
 * stops at the boundary and says so.
 */
function FaultClock({
  faultAt,
  now,
  takeEndAt,
}: {
  faultAt: string | null
  now: number
  takeEndAt: string | null
}) {
  if (faultAt === null) {
    return (
      <div data-testid="fault-clock" className="text-right">
        <p className="text-xs uppercase tracking-wider text-gray-500">since the fault</p>
        <p className="text-2xl font-mono text-gray-500">no fault injected yet</p>
      </div>
    )
  }
  const stoppedAt = takeEndAt === null ? now : new Date(takeEndAt).getTime()
  const elapsed = Math.max(0, stoppedAt - new Date(faultAt).getTime())
  return (
    <div data-testid="fault-clock" className="text-right">
      <p className="text-xs uppercase tracking-wider text-gray-500">since the fault</p>
      <p
        className={`text-3xl font-mono leading-tight ${
          takeEndAt === null ? 'text-amber-300' : 'text-amber-300/60'
        }`}
      >
        T+ {formatMs(elapsed)}
      </p>
      <p className="text-xs font-mono text-gray-500">
        injected {clockTime(faultAt)}
        {takeEndAt !== null && ' · stopped at the take’s end'}
      </p>
    </div>
  )
}

// ──────────────────────────────────────────────── the two phase rows

const STATION_TONE = {
  platform: {
    current: 'bg-blue-500/20 border-blue-400/70 text-blue-100',
    passed: 'bg-blue-500/10 border-blue-800/70 text-blue-200/80',
  },
  agent: {
    current: 'bg-purple-500/20 border-purple-400/70 text-purple-100',
    passed: 'bg-purple-500/10 border-purple-800/70 text-purple-200/80',
  },
} as const

function StationCell<K extends string>({
  station,
  tone,
  now,
}: {
  station: Station<K>
  tone: 'platform' | 'agent'
  now: number
}) {
  const styles =
    station.state === 'current'
      ? STATION_TONE[tone].current
      : station.state === 'passed'
        ? STATION_TONE[tone].passed
        : 'bg-gray-900 border-gray-800 text-gray-600'

  const elapsed =
    station.state === 'current' && station.at !== null
      ? Math.max(0, now - new Date(station.at).getTime())
      : null

  return (
    <li
      data-testid={`station-${station.key}`}
      data-state={station.state}
      aria-current={station.state === 'current' ? 'step' : undefined}
      className={`flex-1 min-w-[8.5rem] rounded-lg border px-3 py-2 ${styles}`}
    >
      <span className="block text-base leading-tight">{station.label}</span>
      <span className="block text-xs font-mono mt-1 opacity-80 whitespace-nowrap">
        {station.at === null ? '—' : clockTime(station.at)}
        {station.durationMs !== null && ` · ${formatMs(station.durationMs)}`}
        {elapsed !== null && station.durationMs === null && ` · ${formatMs(elapsed)}`}
      </span>
      {/* The event's time is above; this is when the page could have known it. A
          station with both is a station whose report arrived late (WO-R3-334, F2). */}
      {station.reportedAt !== null && (
        <span
          data-testid={`station-${station.key}-reported`}
          className="block text-[11px] mt-0.5 font-mono text-amber-300/90 leading-tight whitespace-nowrap"
        >
          reported {clockTime(station.reportedAt)}
        </span>
      )}
      {station.note !== null && (
        <span className="block text-[11px] mt-0.5 opacity-70 leading-tight">
          {station.note}
        </span>
      )}
    </li>
  )
}

/**
 * One witness's row.
 *
 * Rendered in full whether or not that witness has anything to say: a row that
 * disappears when it is quiet is a row the viewer stops trusting, and the empty
 * agent row IS the reading before the responder reports (the first take had no way
 * to show "the platform sees a fault and the agent has said nothing").
 */
function PhaseRow<K extends string>({
  testId,
  title,
  source,
  stations,
  tone,
  now,
  note = null,
}: {
  testId: string
  title: string
  source: string
  stations: Station<K>[]
  tone: 'platform' | 'agent'
  now: number
  /** What this row is waiting for, when it has nothing of its own to say yet. */
  note?: string | null
}) {
  return (
    <div
      data-testid={testId}
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-2"
    >
      <div className="flex items-baseline gap-3 mb-1.5">
        <h2
          className={`text-sm font-semibold tracking-wider uppercase ${
            tone === 'platform' ? 'text-blue-300' : 'text-purple-300'
          }`}
        >
          {title}
        </h2>
        <p className="text-xs text-gray-500">{source}</p>
        {note !== null && (
          <p data-testid={`${testId}-note`} className="text-xs text-blue-200/80">
            {note}
          </p>
        )}
      </div>
      <ol aria-label={`${title} phases`} className="flex flex-wrap items-stretch gap-1.5">
        {stations.map((station) => (
          <StationCell key={station.key} station={station} tone={tone} now={now} />
        ))}
      </ol>
    </div>
  )
}

// ────────────────────────────────────────────────────────────── the chart

const MARKER_STYLE: Record<
  ChartMarker['kind'],
  { stroke: string; fill: string; glyph: string; label: string }
> = {
  reset: { stroke: '#6b7280', fill: '#6b7280', glyph: 'W', label: 'world reset' },
  fault: { stroke: '#f59e0b', fill: '#f59e0b', glyph: 'F', label: 'fault injected' },
  action: { stroke: '#60a5fa', fill: '#60a5fa', glyph: 'A', label: 'agent action' },
  recovery: { stroke: '#34d399', fill: '#34d399', glyph: 'R', label: 'recovered' },
}

interface ChartHover {
  sample: MetricSample
  x: number
  y: number
}

/**
 * The metric over the take, with the moments that changed it marked on it.
 *
 * One series, one axis. The DLQ depth in `dlq_backlog` mode is a second CHART
 * rather than a second line: lag runs to tens and the dead-letter depth to five,
 * so one plot with two scales would invent a relationship between them.
 *
 * Four rules, all from the third take's screenshot (WO-R3-334):
 *
 *  - **the x-axis is the take**, two minutes before the fault to now, because a
 *    fixed fifteen minutes drew a two-minute climb in the last eighth of the plot.
 *    The `full window` button zooms back out to everything the platform still holds;
 *  - **the marker set is closed** — the boundaries, the lab's fault, the run's own
 *    Tier-1 actions, the recovery. Reads are not marked (fifteen ticks is a comb),
 *    and neither are the runner's or the evaluator's calls, which is what put three
 *    blue `A` markers on a take where the agent never acted;
 *  - **a cursor on the right edge** with the latest reading beside it, so the end of
 *    the line has a number on it without hovering;
 *  - **the plot is the panel minus two lines of caption.** Everything else about the
 *    chart — the samples, the marker times — is in those two lines or behind them.
 */
function MetricChart({
  testId,
  title,
  unit,
  source,
  caption,
  value,
  known,
  unknownReason,
  threshold,
  thresholdWhy,
  samples,
  markers,
  windowStart,
  windowEnd,
  zoomed,
  onToggleZoom,
  liveEdge,
  error,
  onRetry,
}: {
  testId: string
  title: string
  unit: string
  source: string
  caption: string
  value: number | null
  known: boolean
  unknownReason: string | null
  threshold: number
  thresholdWhy: string
  samples: MetricSample[]
  markers: ChartMarker[]
  windowStart: number
  windowEnd: number
  /** True while the axis is the take's span rather than the platform's whole window. */
  zoomed: boolean
  onToggleZoom: () => void
  /** False on a take that has ended: the right edge is the boundary, not "now". */
  liveEdge: boolean
  error: string | null
  onRetry: () => void
}) {
  const [hover, setHover] = useState<ChartHover | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)

  // The viewBox's aspect is the plot's aspect: the SVG scales to its column's
  // width, so a wide viewBox in a narrow column letterboxes and the line ends up
  // occupying half the height the panel gave it.
  const W = 560
  const H = 220
  const PAD = { l: 50, r: 18, t: 16, b: 30 }
  const plotW = W - PAD.l - PAD.r
  const plotH = H - PAD.t - PAD.b

  const peak = samples.reduce((m, s) => Math.max(m, s.v), 0)
  const ticks = yAxisTicks(Math.max(threshold * 1.4, peak * 1.15, 1))
  const yMax = ticks[ticks.length - 1]
  const span = Math.max(1, windowEnd - windowStart)
  const x = (t: number) => PAD.l + ((t - windowStart) / span) * plotW
  const y = (v: number) => PAD.t + plotH - (Math.min(v, yMax) / yMax) * plotH

  const inWindow = samples.filter((s) => s.t >= windowStart && s.t <= windowEnd)
  const points = inWindow.map((s) => `${String(x(s.t))},${y(s.v).toFixed(1)}`).join(' ')
  const last = inWindow[inWindow.length - 1] ?? null
  const breaching = known && value !== null && value > threshold
  const spanMinutes = Math.max(1, Math.round(span / 60_000))

  function onMove(event: React.PointerEvent<SVGSVGElement>) {
    const svg = svgRef.current
    if (svg === null || inWindow.length === 0) return
    const rect = svg.getBoundingClientRect()
    if (rect.width === 0) return
    // The pointer is in CSS pixels and the plot is in viewBox units.
    const vx = ((event.clientX - rect.left) / rect.width) * W
    const t = windowStart + ((vx - PAD.l) / plotW) * span
    const nearest = inWindow.reduce((best, s) =>
      Math.abs(s.t - t) < Math.abs(best.t - t) ? s : best,
    )
    setHover({ sample: nearest, x: x(nearest.t), y: y(nearest.v) })
  }

  return (
    <section
      data-testid={testId}
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 flex flex-col shrink-0"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-base text-gray-200">{title}</h3>
          <p className="text-xs text-gray-500 font-mono">{source}</p>
        </div>
        <div className="text-right">
          {known && value !== null ? (
            <p
              data-testid={`${testId}-value`}
              className={`text-4xl font-mono leading-none ${
                breaching ? 'text-red-300' : 'text-green-300'
              }`}
            >
              {value}
            </p>
          ) : (
            // Never a zero: an absent reading is not a healthy one (ADR 0030).
            <p
              data-testid={`${testId}-value`}
              className="text-2xl font-mono text-gray-500 leading-none"
            >
              unknown
            </p>
          )}
          <p className="text-xs text-gray-500 mt-1">{unit}</p>
        </div>
      </div>

      {!known && (
        <p className="text-xs text-amber-300/90 mt-1">
          {unknownReason ?? 'the platform did not say why'}
        </p>
      )}
      {error !== null && <ErrorState message={error} onRetry={onRetry} className="py-4" />}

      <div className="relative mt-2">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${String(W)} ${String(H)}`}
          className="w-full h-auto"
          role="img"
          aria-label={`${title} over ${String(spanMinutes)} minutes, threshold ${String(threshold)}`}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {/* The band above the threshold: where the reading is a breach. Labelled
              on the LEFT, where the eye starts and where no marker can cover it. */}
          <rect
            x={PAD.l}
            y={PAD.t}
            width={plotW}
            height={Math.max(0, y(threshold) - PAD.t)}
            className="fill-red-500/10"
          />
          <line
            x1={PAD.l}
            x2={PAD.l + plotW}
            y1={y(threshold)}
            y2={y(threshold)}
            className="stroke-red-400/70"
            strokeWidth={1}
            strokeDasharray="4 3"
          />
          <text
            x={PAD.l + 5}
            y={y(threshold) - 5}
            textAnchor="start"
            className="fill-red-300/90"
            style={{ fontSize: '11px' }}
          >
            threshold {threshold}
          </text>

          {/* Axes and gridlines: hairline, recessive, and zero always drawn. */}
          {ticks.map((tick) => (
            <Fragment key={tick}>
              <line
                x1={PAD.l}
                x2={PAD.l + plotW}
                y1={y(tick)}
                y2={y(tick)}
                className={tick === 0 ? 'stroke-gray-600' : 'stroke-gray-800'}
                strokeWidth={1}
              />
              <text
                x={PAD.l - 8}
                y={y(tick) + 4}
                textAnchor="end"
                className="fill-gray-500"
                style={{ fontSize: '11px', fontVariantNumeric: 'tabular-nums' }}
              >
                {tick}
              </text>
            </Fragment>
          ))}
          <line
            x1={PAD.l}
            x2={PAD.l}
            y1={PAD.t}
            y2={PAD.t + plotH}
            className="stroke-gray-700"
            strokeWidth={1}
          />
          <text
            x={PAD.l}
            y={H - 8}
            className="fill-gray-500"
            style={{ fontSize: '11px' }}
          >
            {clockTime(new Date(windowStart).toISOString())} (−{spanMinutes} min)
          </text>
          <text
            x={PAD.l + plotW}
            y={H - 8}
            textAnchor="end"
            className="fill-gray-500"
            style={{ fontSize: '11px' }}
          >
            {liveEdge ? 'now' : 'take ended'}
          </text>

          {/* Markers, under the line so the data stays on top. */}
          {markers.map((m) => (
            <g key={`${m.kind}-${m.at}`} data-testid={`chart-marker-${m.kind}`}>
              <line
                x1={x(m.t)}
                x2={x(m.t)}
                y1={PAD.t}
                y2={PAD.t + plotH}
                stroke={MARKER_STYLE[m.kind].stroke}
                strokeOpacity={0.75}
                strokeWidth={1.5}
              />
              <rect
                x={x(m.t) - 7}
                y={PAD.t - 1}
                width={14}
                height={14}
                rx={3}
                fill={MARKER_STYLE[m.kind].fill}
                fillOpacity={0.9}
              />
              <text
                x={x(m.t)}
                y={PAD.t + 10}
                textAnchor="middle"
                className="fill-gray-950"
                style={{ fontSize: '10px', fontWeight: 600 }}
              >
                {MARKER_STYLE[m.kind].glyph}
              </text>
            </g>
          ))}

          {inWindow.length === 0 ? (
            <text
              x={PAD.l + plotW / 2}
              y={PAD.t + plotH / 2}
              textAnchor="middle"
              className="fill-gray-600"
              style={{ fontSize: '13px' }}
            >
              no samples in this window yet
            </text>
          ) : (
            <>
              <polyline
                points={points}
                fill="none"
                stroke="#60a5fa"
                strokeWidth={2}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
              {last !== null && (
                <circle
                  cx={x(last.t)}
                  cy={y(last.v)}
                  r={4.5}
                  fill="#60a5fa"
                  stroke="#111827"
                  strokeWidth={2}
                />
              )}
            </>
          )}

          {/* The cursor on the right edge, with the newest reading on it: the end of
              the line is where a viewer looks, and it had no number of its own. */}
          <g data-testid={`${testId}-cursor`}>
            <line
              x1={PAD.l + plotW}
              x2={PAD.l + plotW}
              y1={PAD.t}
              y2={PAD.t + plotH}
              className={liveEdge ? 'stroke-blue-300/70' : 'stroke-gray-500/70'}
              strokeWidth={1}
              strokeDasharray="3 3"
            />
            {last !== null && (
              <text
                x={PAD.l + plotW - 6}
                y={Math.min(Math.max(y(last.v) - 10, PAD.t + 26), PAD.t + plotH - 6)}
                textAnchor="end"
                className={liveEdge ? 'fill-blue-200' : 'fill-gray-400'}
                style={{ fontSize: '22px', fontVariantNumeric: 'tabular-nums' }}
              >
                {last.v}
              </text>
            )}
          </g>

          {hover !== null && (
            <>
              <line
                x1={hover.x}
                x2={hover.x}
                y1={PAD.t}
                y2={PAD.t + plotH}
                className="stroke-gray-400/60"
                strokeWidth={1}
              />
              <circle
                cx={hover.x}
                cy={hover.y}
                r={4.5}
                fill="#93c5fd"
                stroke="#111827"
                strokeWidth={2}
              />
            </>
          )}
        </svg>

        {hover !== null && (
          <div
            className="absolute pointer-events-none bg-gray-950 border border-gray-700 rounded px-2 py-1 text-xs"
            style={{ left: `${String((hover.x / W) * 100)}%`, top: 0 }}
          >
            <span className="font-mono text-base text-gray-100">{hover.sample.v}</span>{' '}
            <span className="text-gray-400">{clockTime(hover.sample.at)}</span>
          </div>
        )}
      </div>

      {/* Two lines of caption, and the plot gets the rest of the panel. Line one is
          the threshold and the window; line two is every marker, named, plus the
          samples themselves — identity is never colour alone and no value on this
          chart is reachable only by hovering. */}
      <p className="text-xs text-gray-500 mt-1 truncate">
        threshold {threshold} — {thresholdWhy}
        {' · '}
        <button
          data-testid={`${testId}-zoom`}
          onClick={onToggleZoom}
          className="text-blue-300 hover:text-blue-200"
        >
          {zoomed ? 'full window' : 'zoom to the take'}
        </button>
      </p>
      <div className="flex items-center gap-x-3 gap-y-1 flex-wrap text-xs text-gray-600">
        {markers.map((m) => (
          <span key={`legend-${m.kind}-${m.at}`} className="flex items-center gap-1.5">
            <span
              aria-hidden
              className="inline-block w-3.5 h-3.5 rounded-sm text-center leading-[0.875rem] text-gray-950 font-semibold"
              style={{ background: MARKER_STYLE[m.kind].fill, fontSize: '9px' }}
            >
              {MARKER_STYLE[m.kind].glyph}
            </span>
            <span className="text-gray-300">{m.label}</span>
            <span className="font-mono text-gray-500">{clockTime(m.at)}</span>
          </span>
        ))}
        <details className="ml-auto">
          <summary className="cursor-pointer whitespace-nowrap">
            samples ({inWindow.length})
          </summary>
          <p className="text-gray-600">{caption}</p>
          <table className="mt-1 text-xs w-full">
            <thead>
              <tr className="text-left text-gray-500">
                <th className="font-medium">measured at</th>
                <th className="font-medium">{unit}</th>
              </tr>
            </thead>
            <tbody className="font-mono text-gray-400">
              {inWindow.map((s) => (
                <tr key={s.at}>
                  <td>{clockTime(s.at)}</td>
                  <td>{s.v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </div>
    </section>
  )
}

// ──────────────────────────────────────────────────── the agent panel (centre)

const VERDICT_STYLE: Record<string, string> = {
  verified: 'bg-green-500/20 text-green-200 border-green-500/50',
  verified_stabilizer: 'bg-blue-500/20 text-blue-200 border-blue-500/50',
  verified_unresolved: 'bg-amber-500/20 text-amber-200 border-amber-500/50',
  not_verified: 'bg-red-500/20 text-red-200 border-red-500/50',
}

function verdictStyle(verdict: string): string {
  return VERDICT_STYLE[verdict] ?? 'bg-gray-700/40 text-gray-300 border-gray-600'
}

/**
 * Confidence, with the bar the loop actually gates on drawn on it.
 *
 * A number on its own does not say whether the agent was allowed to act. The tick at
 * 0.7 is the remediate threshold, so a viewer can see at a glance that the third
 * take's 0.82 was over the bar for the whole run and the agent still did not act —
 * which is the finding the demo exists to show (INC-004).
 */
function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.max(0, Math.min(100, confidence * 100))
  const over = confidence >= REMEDIATE_THRESHOLD
  return (
    <div className="flex items-center gap-2">
      <div className="relative flex-1 h-2.5 bg-gray-800 rounded overflow-hidden">
        <div
          className={`h-full ${over ? 'bg-purple-300' : 'bg-purple-400/60'}`}
          style={{ width: `${String(pct)}%` }}
        />
        <span
          data-testid="confidence-threshold-tick"
          aria-hidden
          title={`the ${String(REMEDIATE_THRESHOLD)} remediate threshold`}
          className="absolute top-0 bottom-0 w-0.5 bg-gray-300/80"
          style={{ left: `${String(REMEDIATE_THRESHOLD * 100)}%` }}
        />
      </div>
      <span className="text-sm font-mono text-gray-300 w-12 text-right">
        {Math.round(pct)}%
      </span>
    </div>
  )
}

/**
 * A reasoning excerpt, truncated unless it is the one that matters.
 *
 * The top hypothesis is shown whole: the third take's screenshot truncated it with
 * "more…" — so the one sentence explaining why the agent believed what it believed
 * was the one sentence not on screen. Every other excerpt still truncates, because
 * five of them at full length is the panel.
 */
function Excerpt({
  text,
  testId,
  full = false,
}: {
  text: string
  testId?: string
  full?: boolean
}) {
  const [open, setOpen] = useState(false)
  const long = !full && text.length > 140
  return (
    <p data-testid={testId} className="text-sm text-gray-400 leading-snug">
      {open || !long ? text : `${text.slice(0, 140)}…`}
      {long && (
        <button
          onClick={() => setOpen((o) => !o)}
          className="ml-1 text-xs text-blue-300 hover:text-blue-200"
        >
          {open ? 'less' : 'more'}
        </button>
      )}
    </p>
  )
}

/**
 * The three terminal states, which is when an absence stops being "not yet".
 *
 * `plan`, `verification` and `attribution` were all null in the third take and all
 * three were correct — the agent never acted, so there was nothing to plan, verify or
 * attribute. "No action planned yet" on a run that has escalated is the page implying
 * a wait that will never end, so a terminal run gets the sentence instead.
 */
const TERMINAL_RUN_STATES = ['resolved', 'escalated', 'failed']

function isTerminalRun(run: AgentRun | null): boolean {
  return run !== null && (run.finished_at !== null || TERMINAL_RUN_STATES.includes(run.state))
}

/**
 * One ranked cause, as the run ranked it.
 *
 * `confidence` can be absent — a planner ranking carries name, category and confidence
 * and any of them can be missing — and an absent number gets a sentence rather than a
 * bar at zero, which would read as "the agent had no confidence in this" (ADR 0030 in
 * the UI, again).
 */
function CauseRow({ cause, top }: { cause: RankedCause; top: boolean }) {
  return (
    <li
      data-testid="hypothesis-row"
      className="bg-gray-950/60 border border-gray-800 rounded px-3 py-2"
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-base text-gray-100">{cause.name}</span>
        <span className="text-xs font-mono text-gray-500">
          {cause.category ?? 'no category reported'}
        </span>
      </div>
      <div className="mt-1">
        {cause.confidence === null ? (
          <p className="text-xs text-gray-600">no confidence reported</p>
        ) : (
          <ConfidenceBar confidence={cause.confidence} />
        )}
      </div>
      {cause.reasoning_excerpt ? (
        <div className="mt-1">
          {/* The top one whole; the rest truncated. */}
          <Excerpt text={cause.reasoning_excerpt} full={top} />
        </div>
      ) : (
        <p className="text-xs text-gray-600 mt-1">no reasoning reported</p>
      )}
    </li>
  )
}

/** Where the planner said it was going next, and why — one line, its own words. */
function NextActionLine({ snapshot }: { snapshot: RankingSnapshot }) {
  const next = snapshot.nextAction
  if (next === null && snapshot.reason === null) return null
  return (
    <p data-testid="ranking-next-action" className="text-xs text-gray-400 mt-1">
      {next !== null && (
        <>
          next:{' '}
          <span className="font-mono text-purple-200">
            {next.kind ?? 'no kind reported'}
            {next.tool === null ? '' : ` ${next.tool}`}
          </span>
        </>
      )}
      {snapshot.reason !== null && (
        <span className="text-gray-400">
          {next === null ? '' : ' — '}
          {snapshot.reason}
        </span>
      )}
    </p>
  )
}

/**
 * The top cause's confidence over the planner's own calls, with the bar it has to clear.
 *
 * One point per planner call (WO-R3-337's `report` steps), so the shape of the
 * investigation is on screen rather than only its conclusion: the fourth take's run held
 * one cause over 0.7 for three consecutive rankings and still did not act, and a single
 * number could never show that. Every value is printed under the line as well, because
 * nothing on this page may be reachable only by hovering.
 */
function ConfidenceSparkline({ points }: { points: ConfidencePoint[] }) {
  if (points.length < 2) return null
  const W = 240
  const H = 44
  const PAD = 5
  const x = (i: number) => PAD + (i / (points.length - 1)) * (W - 2 * PAD)
  const y = (v: number) => PAD + (1 - Math.max(0, Math.min(1, v))) * (H - 2 * PAD)
  const line = points.map((p, i) => `${String(x(i))},${y(p.confidence).toFixed(1)}`).join(' ')
  return (
    <div data-testid="confidence-sparkline" className="mt-1.5">
      <svg
        viewBox={`0 0 ${String(W)} ${String(H)}`}
        className="w-full h-11"
        role="img"
        aria-label={`top hypothesis confidence over ${String(points.length)} planner calls, threshold ${String(REMEDIATE_THRESHOLD)}`}
      >
        <line
          x1={PAD}
          x2={W - PAD}
          y1={y(REMEDIATE_THRESHOLD)}
          y2={y(REMEDIATE_THRESHOLD)}
          className="stroke-gray-500"
          strokeWidth={1}
          strokeDasharray="4 3"
        />
        <text
          x={W - PAD}
          y={y(REMEDIATE_THRESHOLD) - 3}
          textAnchor="end"
          className="fill-gray-500"
          style={{ fontSize: '9px' }}
        >
          {REMEDIATE_THRESHOLD}
        </text>
        <polyline
          points={line}
          fill="none"
          stroke="#c4b5fd"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        {points.map((p, i) => (
          <circle
            key={p.seq}
            cx={x(i)}
            cy={y(p.confidence)}
            r={2.5}
            fill={p.confidence >= REMEDIATE_THRESHOLD ? '#d8b4fe' : '#a78bfa'}
          />
        ))}
      </svg>
      <p className="text-[11px] font-mono text-gray-500">
        {points.map((p) => p.confidence.toFixed(2)).join(' → ')} over{' '}
        {points.length} planner calls
      </p>
    </div>
  )
}

/**
 * What the agent thinks now, and what it thought before that.
 *
 * The newest ranking is on top with its top cause's reasoning whole; the older ones are
 * collapsed below with their own timestamps, because three rankings existed during the
 * fourth take's 22-second investigation and the page could only ever show the last.
 */
function HypothesesPanel({
  history,
  trend,
  source,
}: {
  history: RankingSnapshot[]
  trend: ConfidencePoint[]
  source: ReturnType<typeof hypothesesSource>
}) {
  const [head, ...older] = history
  return (
    <div>
      <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-1">
        What the agent thinks now
      </h3>
      {head === undefined ? (
        <p data-testid="hypotheses-empty" className="text-sm text-gray-600">
          None reported yet — the responder has not ranked a cause.
        </p>
      ) : (
        <div data-testid="hypotheses-now">
          <p className="text-xs font-mono text-gray-500">
            {head.at === null ? 'the run’s latest reading' : `ranked ${clockTime(head.at)}`}
            {head.tool !== null && ` · ${head.tool}`}
            {head.seq !== null && ` · step #${String(head.seq)}`}
          </p>
          <ol className="space-y-2 mt-1">
            {head.ranking.map((cause, i) => (
              <CauseRow
                key={`${cause.category ?? 'none'}-${cause.name}-${String(i)}`}
                cause={cause}
                top={i === 0}
              />
            ))}
          </ol>
          <NextActionLine snapshot={head} />
          <ConfidenceSparkline points={trend} />
          {older.length > 0 && (
            <details data-testid="ranking-history" className="mt-1.5">
              <summary className="text-xs text-blue-300 cursor-pointer">
                {older.length} earlier ranking{older.length === 1 ? '' : 's'}
              </summary>
              <ul className="mt-1 space-y-1">
                {older.map((snapshot) => (
                  <li
                    key={`${String(snapshot.seq ?? 0)}-${snapshot.at ?? 'no-time'}`}
                    data-testid="ranking-history-entry"
                    className="border-l-2 border-purple-900/60 pl-2"
                  >
                    <p className="text-[11px] font-mono text-gray-500">
                      {snapshot.at === null ? 'no time reported' : clockTime(snapshot.at)}
                      {snapshot.tool !== null && ` · ${snapshot.tool}`}
                      {snapshot.seq !== null && ` · step #${String(snapshot.seq)}`}
                    </p>
                    <ul className="text-xs text-gray-400">
                      {snapshot.ranking.map((cause, i) => (
                        <li key={`${cause.name}-${String(i)}`} className="font-mono">
                          {cause.confidence === null
                            ? '—'
                            : cause.confidence.toFixed(2)}{' '}
                          {cause.name}
                          {cause.category === null ? '' : ` · ${cause.category}`}
                        </li>
                      ))}
                    </ul>
                    <NextActionLine snapshot={snapshot} />
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      {source === 'current_only' && (
        <p data-testid="hypotheses-source-note" className="text-xs text-amber-300/80 mt-1">
          Only a top hypothesis was reported, with no ranking and no reasoning — a
          commander older than WO-R3-329 sends one.
        </p>
      )}
    </div>
  )
}

function AgentPanel({
  run,
  steps,
  takeStartAt,
  currentTake,
  loading,
  error,
  onRetry,
}: {
  run: AgentRun | null
  steps: AgentRunStepRecord[]
  /** The opening boundary of the take on screen, for the waiting sentence. */
  takeStartAt: string | null
  /** True while the take on screen is the take now running. */
  currentTake: boolean
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  const history = rankingHistory(run, steps)
  const trend = confidenceTrend(steps)
  const source = hypothesesSource(run)
  const verifications = runVerifications(run)
  const plan = run?.plan ?? null
  const meter = budgetMeter(run?.budget)
  const lastStep = steps[steps.length - 1] ?? null
  const terminal = isTerminalRun(run)
  const acted = plan !== null || steps.some((s) => s.kind === 'action')

  if (error !== null) {
    return (
      <section
        data-testid="agent-panel"
        className="bg-gray-900 border border-gray-800 rounded-lg"
      >
        <ErrorState message={error} onRetry={onRetry} />
        <p className="px-6 pb-4 text-sm text-gray-500 text-center">
          Agent runs are operator-only. A 403 here means this login may not read
          them, or the stack predates the <code>agent_runs</code> table.
        </p>
      </section>
    )
  }

  if (run === null) {
    return (
      <section
        data-testid="agent-panel"
        className="bg-gray-900 border border-gray-800 rounded-lg px-6 py-10 text-center"
      >
        <p className="text-lg text-gray-400">
          {loading
            ? 'Loading…'
            : currentTake
              ? 'Waiting for this take’s run.'
              : 'This take reported no run.'}
        </p>
        {/* A fresh take is supposed to look like this, and saying which world it is
            waiting in is the difference between "at zero" and "broken". */}
        <p data-testid="agent-panel-waiting" className="text-sm text-gray-500 mt-1">
          {takeStartAt === null
            ? 'No reset boundary is in view, so this is everything the page can see.'
            : `Nothing reported since the reset at ${clockTime(takeStartAt)}; the page adopts the run on the poll after its first report.`}
        </p>
        <p className="text-sm text-gray-600 mt-2">
          The commander reports itself over MCP (ADR 0035). Nothing on this panel is
          readable by the agent&rsquo;s own principal.
        </p>
      </section>
    )
  }

  return (
    <section
      data-testid="agent-panel"
      aria-labelledby="demo-agent"
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 space-y-4 overflow-y-auto"
    >
      <div className="space-y-1.5">
        <div className="flex items-baseline gap-2 flex-wrap">
          <h2 id="demo-agent" className="sr-only">
            The agent
          </h2>
          <span
            data-testid="agent-state"
            className="inline-block px-3 py-1 rounded-full text-base border bg-purple-500/20 text-purple-100 border-purple-500/50"
          >
            {agentStateLabel(run)}
          </span>
          <p className="text-xs font-mono text-gray-500">
            {run.scenario ?? 'no run label'} · {shortId(run.id)}
            {run.finished_at !== null && ` · finished ${clockTime(run.finished_at)}`}
          </p>
        </div>
        <div data-testid="budget-meter">
          {meter.used === null ? (
            <p className="text-sm text-gray-600">
              <span className="text-xs uppercase tracking-wider text-gray-500">Budget</span> not
              reported
            </p>
          ) : (
            <>
              <p className="text-sm font-mono text-gray-300">
                <span className="text-xs uppercase tracking-wider text-gray-500">Budget </span>
                <span className="text-lg text-gray-100">{meter.used}</span>
                {meter.max !== null && <span className="text-gray-500">/{meter.max}</span>}
                <span className="text-xs text-gray-500"> calls</span> ·{' '}
                {meter.tokens === null ? 'tokens —' : `${meter.tokens} tokens`} ·{' '}
                {meter.usd === null ? '$—' : `$${meter.usd.toFixed(4)}`}
                {meter.wallSeconds !== null && ` · ${Math.round(meter.wallSeconds)}s`}
              </p>
              {meter.percent !== null && (
                <div className="h-2 bg-gray-800 rounded overflow-hidden mt-1">
                  <div
                    className={`h-full ${meter.over ? 'bg-red-400' : 'bg-blue-400'}`}
                    style={{ width: `${String(meter.percent)}%` }}
                  />
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── what the agent thinks now (WO-R3-336) ──────────────────────────── */}
      <HypothesesPanel history={history} trend={trend} source={source} />

      {/* ── the plan ───────────────────────────────────────────────────────── */}
      <div>
        <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-1">The plan</h3>
        {plan === null ? (
          <p data-testid="plan-empty" className="text-sm text-gray-600">
            {terminal
              ? 'The agent handed off without acting — it never planned an action.'
              : 'No action planned yet.'}
          </p>
        ) : (
          <div
            data-testid="plan-card"
            className="bg-blue-950/30 border border-blue-900/60 rounded px-3 py-2"
          >
            <p className="text-base font-mono text-blue-100">{plan.action_tool}</p>
            <pre className="text-xs font-mono text-gray-300 whitespace-pre-wrap break-all mt-1">
              {compactJson(plan.action_arguments) ?? 'no arguments'}
            </pre>
            <p className="text-xs text-gray-400 mt-1">
              aimed at:{' '}
              <span className="text-gray-200">
                {plan.target_hypothesis ?? 'no hypothesis named'}
              </span>
            </p>
            {plan.rationale_excerpt && (
              <div className="mt-1">
                <Excerpt text={plan.rationale_excerpt} />
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── verification ───────────────────────────────────────────────────── */}
      <div>
        <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-1">
          Verification
        </h3>
        {verifications.length === 0 ? (
          <p data-testid="verifications-empty" className="text-sm text-gray-600">
            {terminal
              ? acted
                ? 'No verification recorded, although the run acted — the reporter sent none.'
                : 'No verification because no action: there was nothing to check.'
              : 'Nothing verified yet.'}
          </p>
        ) : (
          <ul className="space-y-1.5">
            {verifications.map((v, i) => (
              <li
                key={`${v.verdict}-${String(v.attempt ?? i)}`}
                data-testid="verification-row"
                className="flex items-start gap-2"
              >
                <span
                  className={`px-2 py-0.5 rounded text-xs border font-mono shrink-0 ${verdictStyle(v.verdict)}`}
                >
                  {v.verdict}
                </span>
                <span className="text-xs font-mono text-gray-500 shrink-0">
                  {v.attempt != null ? `attempt ${v.attempt}` : 'attempt —'}
                  {v.of != null && ` of ${v.of}`}
                </span>
                {v.reasoning_excerpt && (
                  <span className="text-sm text-gray-400 leading-snug">
                    {v.reasoning_excerpt}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
        {verifications.length >= VERIFICATIONS_CAP && (
          <p className="text-xs text-amber-300/90 mt-1">
            The oldest verdicts past {VERIFICATIONS_CAP} are dropped by the platform&rsquo;s
            cap, so this list is the newest part of the run.
          </p>
        )}
      </div>

      <p className="text-xs text-gray-600 font-mono">
        GET /admin/agent-runs/{shortId(run.id)} — last step{' '}
        {lastStep === null
          ? 'none reported'
          : `#${String(lastStep.seq)} ${lastStep.tool ?? lastStep.kind}${
              lastStep.at === null ? '' : ` at ${clockTime(lastStep.at)}`
            }`}
      </p>
    </section>
  )
}

// ─────────────────────────────────────────────────────── the ledger (right)
//
// WO-R3-334 rebuilt this panel around one question: what did the call answer?
//
// The third take's ledger was raw audit rows — tool, arguments, latency, no result —
// and most of them were not even the agent's: the runner read lag every three seconds
// under the agent's token (F3) and the evaluator's guard probes fired seven more calls
// after the boundary (F4). So the panel now reads like a transcript of ONE run:
//
//   * one line per call, with the answer summarised on it;
//   * the arguments, the whole excerpt and the latency behind a click — except on an
//     ACTION row, which is never collapsed, because the action is the point;
//   * newest at the BOTTOM, pinned there unless the operator scrolls up;
//   * everything that is not this run's own call counted and hidden behind a toggle,
//     never silently dropped.

const KIND_BADGE: Record<string, { label: string; className: string }> = {
  read: { label: 'READ', className: 'bg-gray-700/50 text-gray-300 border-gray-600' },
  action: { label: 'ACTION', className: 'bg-blue-500/25 text-blue-200 border-blue-500/50' },
  report: {
    label: 'REPORT',
    className: 'bg-purple-500/25 text-purple-200 border-purple-500/50',
  },
}

const LEDGER_TONE: Record<LedgerKind, string> = {
  step: 'border-gray-800 bg-gray-950/50',
  alert: 'border-red-800/60 bg-red-950/20',
  agent_audit: 'border-gray-800 bg-gray-950/50',
  agent_report: 'border-purple-900/60 bg-purple-950/20',
  lab: 'border-amber-700/60 bg-amber-950/25',
  lab_probe: 'border-gray-800 bg-gray-950/40',
  other_principal: 'border-gray-800 bg-gray-950/40',
  reset: 'border-gray-700 bg-gray-800/30',
  job_event: 'border-gray-800 bg-gray-900/40',
  human: 'border-green-800/50 bg-green-950/20',
}

/** The one line every row shares: when, what kind, which tool, what it answered. */
function LedgerLine({
  at,
  badge,
  badgeClassName,
  subject,
  summary,
  keepSubject = false,
}: {
  at: string | null
  badge: string
  badgeClassName: string
  subject: string
  summary: string | null
  /** True where the subject is short by construction and the summary is the sentence. */
  keepSubject?: boolean
}) {
  return (
    <span className="flex items-center gap-1.5 min-w-0">
      <span className="text-[10px] font-mono text-gray-500 shrink-0">
        {/* Every field but `seq` and `kind` can be null: this is the responder's own
            account of its own call and the platform fills nothing in. */}
        {at === null ? 'no time reported' : clockTime(at)}
      </span>
      <span
        className={`px-1.5 py-0.5 rounded text-[10px] border font-mono shrink-0 ${badgeClassName}`}
      >
        {badge}
      </span>
      <span
        className={`text-[13px] font-mono text-gray-100 ${
          keepSubject ? 'shrink-0 whitespace-nowrap' : 'truncate'
        }`}
      >
        {subject}
      </span>
      {summary !== null && (
        <span className="text-xs text-gray-400 truncate shrink-[2]">→ {summary}</span>
      )}
    </span>
  )
}

/**
 * The badge a planner's own report wears (WO-R3-336, item 3).
 *
 * Its own badge rather than `REPORT`, because a planner call and a status report are
 * different events to a viewer: one is the agent thinking, the other is the agent
 * telling the platform where it is.
 */
const THINK_BADGE = {
  label: 'THINK',
  className: 'bg-purple-500/30 text-purple-100 border-purple-400/60',
}

/** The ranking a THINK row opens: what was on the table, and what it led to. */
function ThinkDetail({ report }: { report: ReturnType<typeof plannerReport> }) {
  if (report === null) return null
  return (
    <div data-testid="think-detail" className="mt-1 space-y-1">
      {report.ranking.length === 0 ? (
        <p className="text-[11px] text-gray-500">no ranking reported on this step</p>
      ) : (
        <ul className="space-y-1">
          {report.ranking.map((cause, i) => (
            <li key={`${cause.name}-${String(i)}`}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs text-gray-200">{cause.name}</span>
                <span className="text-[10px] font-mono text-gray-500">
                  {cause.category ?? 'no category'}
                </span>
              </div>
              {cause.confidence === null ? (
                <p className="text-[10px] text-gray-600">no confidence reported</p>
              ) : (
                <ConfidenceBar confidence={cause.confidence} />
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="text-[11px] font-mono text-gray-500">{report.tool}</p>
      <p className="text-[11px] font-mono text-gray-400">
        {report.nextAction === null
          ? 'no next action reported'
          : `next: ${report.nextAction.kind ?? 'no kind'}${
              report.nextAction.tool === null ? '' : ` ${report.nextAction.tool}`
            }`}
      </p>
      {report.reason !== null && (
        <p className="text-[11px] text-gray-400 leading-snug">{report.reason}</p>
      )}
    </div>
  )
}

/** The three thinkers, as a ledger line should name them — the full tool is in the detail. */
const THINK_SUBJECT: Record<string, string> = {
  investigation_planner: 'planner',
  reflection: 'reflection',
  verify_judge: 'verify judge',
}

function StepEntry({ step }: { step: AgentRunStepRecord }) {
  const planner = plannerReport(step)
  const think = planner !== null
  // `kind` is an open string on the wire; an unrecognised one is shown verbatim
  // rather than dressed up as a read.
  const badge = think
    ? THINK_BADGE
    : (KIND_BADGE[step.kind] ?? {
        label: step.kind.toUpperCase(),
        className: 'bg-gray-700/50 text-gray-300 border-gray-600',
      })
  const isAction = step.kind === 'action'
  const [open, setOpen] = useState(false)
  const expanded = open || isAction
  const args = think ? null : compactJson(step.arguments)
  const excerpt = think ? null : step.result_excerpt
  const failed = step.outcome != null && step.outcome !== 'success'

  return (
    <div
      data-testid="ledger-entry"
      data-kind={think ? 'think' : step.kind}
      className={`rounded border px-2 py-1 ${
        isAction
          ? 'border-blue-500/60 bg-blue-950/30'
          : think
            ? 'border-purple-800/60 bg-purple-950/20'
            : failed
              ? 'border-red-900/60 bg-red-950/20'
              : LEDGER_TONE.step
      }`}
    >
      {isAction ? (
        <LedgerLine
          at={step.at}
          badge={badge.label}
          badgeClassName={badge.className}
          subject={step.tool ?? 'no tool reported'}
          summary={summariseStep(step)}
        />
      ) : (
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="w-full text-left"
        >
          <LedgerLine
            at={step.at}
            badge={badge.label}
            badgeClassName={badge.className}
            subject={
              think
                ? (THINK_SUBJECT[planner.tool] ?? planner.tool)
                : (step.tool ?? 'no tool reported')
            }
            summary={summariseStep(step)}
            keepSubject={think}
          />
        </button>
      )}

      {expanded && (
        <div className="mt-1 space-y-1">
          {think && open && <ThinkDetail report={planner} />}
          {args !== null && (
            <pre className="text-[11px] font-mono text-gray-400 whitespace-pre-wrap break-all">
              {args}
            </pre>
          )}
          {excerpt != null && excerpt !== '' && (
            <pre
              data-testid="ledger-result"
              className="text-[11px] font-mono text-gray-300 whitespace-pre-wrap break-all bg-gray-950 border border-gray-800 rounded p-1.5"
            >
              {excerpt}
            </pre>
          )}
          <p className="text-[11px] font-mono text-gray-500">
            step #{step.seq} ·{' '}
            <span className={failed ? 'text-red-300' : undefined}>
              {step.outcome ?? 'outcome —'}
            </span>
            {step.latency_ms != null && ` · ${step.latency_ms.toFixed(0)} ms`}
            {/* A planner call spends no budget and makes no MCP call: saying so on the
                row is what stops a viewer counting it as one (WO-R3-337). */}
            {think && ' · the agent thinking, not a call'}
          </p>
        </div>
      )}
    </div>
  )
}

/** The badge an audit-derived row wears, which is the answer to "whose call was this?". */
const ROW_BADGE: Record<string, { label: string; className: string }> = {
  lab: { label: 'LAB', className: 'bg-amber-500/25 text-amber-200 border-amber-500/50' },
  alert: { label: 'PAGED', className: 'bg-red-500/25 text-red-200 border-red-500/50' },
  lab_probe: {
    label: 'LAB PROBE',
    className: 'bg-amber-500/10 text-amber-200/70 border-amber-700/40',
  },
  other_principal: {
    label: 'NOT THIS RUN',
    className: 'bg-gray-700/30 text-gray-400 border-gray-700',
  },
  human: { label: 'HUMAN', className: 'bg-green-500/20 text-green-200 border-green-600/50' },
  job_event: { label: 'JOB', className: 'bg-gray-700/40 text-gray-400 border-gray-700' },
  agent_report: {
    label: 'REPORT',
    className: 'bg-purple-500/25 text-purple-200 border-purple-500/50',
  },
  agent_audit: { label: 'AGENT', className: 'bg-gray-700/50 text-gray-300 border-gray-600' },
}

function RowEntry({ entry }: { entry: LedgerEntry }) {
  const row = entry.row
  const [open, setOpen] = useState(false)
  if (!row) return null
  const extra = row.extra_data ?? {}
  const tool = typeof extra.tool_name === 'string' ? extra.tool_name : null
  const args = compactJson(extra.arguments)
  const latency = typeof extra.latency_ms === 'number' ? extra.latency_ms : null
  const badge = ROW_BADGE[entry.kind] ?? {
    label: 'ROW',
    className: 'bg-gray-700/50 text-gray-300 border-gray-600',
  }
  const alertText = (key: string): string | null =>
    typeof extra[key] === 'string' && extra[key] !== '' ? (extra[key] as string) : null
  const reason =
    entry.kind === 'lab_probe'
      ? 'the lab labelled this read as its own'
      : entry.kind === 'other_principal'
        ? 'another principal, not this run’s'
        : // An alert has no tool and no arguments; what it has is what it said.
          entry.kind === 'alert'
          ? alertText('summary')
          : null

  return (
    <div
      data-testid="ledger-entry"
      data-kind={entry.kind}
      className={`rounded border px-2 py-1 ${LEDGER_TONE[entry.kind]}`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="w-full text-left"
      >
        <LedgerLine
          at={row.created_at}
          badge={badge.label}
          badgeClassName={badge.className}
          subject={tool ?? (entry.kind === 'alert' ? (alertText('fingerprint') ?? row.action) : row.action)}
          summary={reason}
        />
      </button>
      {open && (
        <div className="mt-1 space-y-0.5">
          <p className="text-[11px] font-mono text-gray-600">{row.action}</p>
          {args !== null && (
            <pre
              className={`text-[11px] font-mono whitespace-pre-wrap break-all ${
                entry.kind === 'lab' ? 'text-amber-200/80' : 'text-gray-400'
              }`}
            >
              {args}
            </pre>
          )}
          <p className="text-[11px] font-mono text-gray-500">
            {latency === null
              ? 'the audit log records no result (WO-R3-328)'
              : `${latency.toFixed(0)} ms — the audit log records no result (WO-R3-328)`}
          </p>
        </div>
      )}
    </div>
  )
}

/**
 * The boundary, drawn as a line rather than an event.
 *
 * Grey and with no actor: nothing happened to the world here. It is the edge of a
 * take — which is what the page needed to be able to say, because `audit_logs` is
 * append-only and those rows never leave.
 */
function ResetDivider({ at }: { at: string }) {
  return (
    <div
      data-testid="ledger-reset-divider"
      role="separator"
      aria-label="world reset"
      className="flex items-center gap-2 py-1"
    >
      <span className="flex-1 border-t border-gray-700" />
      <span className="text-xs uppercase tracking-wider font-mono text-gray-500 whitespace-nowrap">
        world reset · {clockTime(at)}
      </span>
      <span className="flex-1 border-t border-gray-700" />
    </div>
  )
}

function ActionLedger({
  entries,
  counts,
  thinkCount,
  boundaryInView,
  stepsDropped,
  usingAudit,
  showJobEvents,
  onToggleJobEvents,
  showHiddenReads,
  onToggleHiddenReads,
  loading,
  error,
  onRetry,
}: {
  entries: LedgerEntry[]
  counts: {
    steps: number
    calls: number
    auditCalls: number
    hiddenReads: number
    agreed: boolean
    warn: boolean
  }
  /** The run's own planner calls — steps that spend no budget and make no call. */
  thinkCount: number
  /** False while the take's opening boundary is still past the end of the rows read. */
  boundaryInView: boolean
  stepsDropped: number
  usingAudit: boolean
  showJobEvents: boolean
  onToggleJobEvents: (next: boolean) => void
  showHiddenReads: boolean
  onToggleHiddenReads: (next: boolean) => void
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  // Newest at the TOP since WO-R3-336 — the owner's rule from the fourth take, which
  // reverses WO-R3-334's transcript order. The newest row is the one the eye wants
  // first, so the panel pins to the top and stops following the moment the operator
  // scrolls down into the history.
  const scroller = useRef<HTMLDivElement | null>(null)
  const [pinned, setPinned] = useState(true)
  const count = entries.length

  useEffect(() => {
    const box = scroller.current
    if (box === null || !pinned) return
    box.scrollTop = 0
  }, [count, pinned])

  function onScroll() {
    const box = scroller.current
    if (box === null) return
    setPinned(box.scrollTop < 24)
  }

  function toTop() {
    const box = scroller.current
    if (box === null) return
    box.scrollTop = 0
    setPinned(true)
  }

  return (
    <section
      data-testid="action-ledger"
      aria-labelledby="demo-ledger"
      className="bg-gray-900 border border-gray-800 rounded-lg px-3 py-3 flex flex-col min-h-0"
    >
      <div className="flex items-start justify-between gap-2">
        <h2 id="demo-ledger" className="text-base text-gray-200">
          Action ledger
        </h2>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-gray-400">
            <input
              data-testid="hidden-reads-toggle"
              type="checkbox"
              checked={showHiddenReads}
              onChange={(e) => onToggleHiddenReads(e.target.checked)}
              className="accent-blue-500"
            />
            other reads
          </label>
          <label className="flex items-center gap-1.5 text-xs text-gray-400">
            <input
              data-testid="job-events-toggle"
              type="checkbox"
              checked={showJobEvents}
              onChange={(e) => onToggleJobEvents(e.target.checked)}
              className="accent-blue-500"
            />
            job events
          </label>
        </div>
      </div>

      {/* Read and action steps only, against this run's own `agent.tool_invoked` rows.
          The planner's own reports are counted beside them rather than in them: they
          make no MCP call, so counting them would recreate the disagreement the fourth
          take's page invented. */}
      <p data-testid="ledger-counts" className="text-xs text-gray-500 mt-0.5">
        {counts.calls} steps reported · {counts.auditCalls} calls the platform recorded
        {thinkCount > 0 && ` · ${String(thinkCount)} planner calls, which make none`}
        {counts.warn && (
          <span className="text-amber-300">
            {' '}
            — the run is over and has been quiet for 10s, so the reporter stopped before
            it finished
          </span>
        )}
      </p>
      {!boundaryInView && (
        <p data-testid="ledger-boundary-missing" className="text-xs text-amber-300/90">
          This take&rsquo;s opening boundary is older than every audit row the page could
          read, so the rows above may include the take before it.
        </p>
      )}
      {counts.hiddenReads > 0 && (
        <p data-testid="ledger-hidden-reads" className="text-xs text-gray-500">
          {counts.hiddenReads} evaluator/traffic reads hidden — the lab&rsquo;s probes and
          other principals&rsquo; calls
        </p>
      )}
      {stepsDropped > 0 && (
        <p className="text-xs text-amber-300/90">
          {stepsDropped} earlier steps dropped by the platform&rsquo;s 200-step cap.
        </p>
      )}
      {usingAudit && (
        <p data-testid="ledger-audit-fallback" className="text-xs text-amber-300/90">
          No steps reported for this run — these rows are the audit log, which records
          the call and not its result.
        </p>
      )}

      {error !== null ? (
        <ErrorState message={error} onRetry={onRetry} className="py-4" />
      ) : loading && entries.length === 0 ? (
        <p className="text-sm text-gray-600 mt-3">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="text-sm text-gray-600 mt-3">
          Nothing on the operator streams yet.
        </p>
      ) : (
        <div
          ref={scroller}
          onScroll={onScroll}
          data-testid="ledger-scroller"
          className="space-y-1 mt-2 overflow-y-auto pr-1 flex-1 min-h-0"
        >
          {entries.map((entry) =>
            entry.kind === 'reset' ? (
              <ResetDivider key={entry.id} at={entry.at} />
            ) : entry.kind === 'step' && entry.step ? (
              <StepEntry key={entry.id} step={entry.step} />
            ) : (
              <RowEntry key={entry.id} entry={entry} />
            ),
          )}
        </div>
      )}

      <div className="flex items-center justify-between gap-2 mt-2">
        <p className="text-xs text-gray-600 font-mono truncate">
          …/steps + /audit/logs — newest first
        </p>
        {!pinned && (
          <button
            data-testid="ledger-to-top"
            onClick={toTop}
            className="text-xs text-blue-300 hover:text-blue-200 shrink-0"
          >
            newest ↑
          </button>
        )}
      </div>
    </section>
  )
}

// ────────────────────────────────────────────────── the DLQ table (dlq mode)

const DECISION_STYLES: Record<string, string> = {
  replay: 'bg-blue-500/20 text-blue-200 border-blue-500/40',
  fence: 'bg-red-500/20 text-red-200 border-red-500/40',
  leave: 'bg-gray-700/40 text-gray-400 border-gray-700',
}

function DlqTable({
  rows,
  steps,
  loading,
  error,
  onRetry,
}: {
  rows: Job[]
  steps: AgentRunStepRecord[]
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  return (
    <section className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 mt-4">
      <h2 className="text-base text-gray-200 mb-2">Dead-letter rows</h2>
      {error !== null ? (
        <ErrorState message={error} onRetry={onRetry} className="py-4" />
      ) : loading ? (
        <p className="text-sm text-gray-600">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-gray-600">No dead-letter rows right now.</p>
      ) : (
        <table data-testid="dlq-table" className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left">
              <th className="font-medium pb-1">Row</th>
              <th className="font-medium pb-1">Type</th>
              <th className="font-medium pb-1">Error</th>
              <th className="font-medium pb-1">Hint</th>
              <th className="font-medium pb-1">Triage</th>
              <th className="font-medium pb-1">Fenced</th>
              <th className="font-medium pb-1">Agent decided</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {rows.map((job) => {
              const decision = dlqDecisionFromSteps(job, steps)
              return (
                <tr key={job.id} className="align-top">
                  <td className="py-2 pr-2 font-mono text-gray-400">{shortId(job.id)}</td>
                  <td className="py-2 pr-2 text-gray-400">
                    {JOB_TYPE_LABELS[job.type] ?? job.type}
                  </td>
                  <td className="py-2 pr-2 text-red-300/90 max-w-[18rem] break-words">
                    {job.error_message ?? '—'}
                  </td>
                  <td className="py-2 pr-2 font-mono text-gray-300">
                    {/* null is "not categorised", which is emphatically not
                        replay-safe — say so rather than printing a dash. */}
                    {job.remediation_hint ?? 'not categorised'}
                  </td>
                  <td className="py-2 pr-2 font-mono text-gray-400">
                    {job.triage?.root_cause_category ?? 'none'}
                  </td>
                  <td
                    className="py-2 pr-2 font-mono text-gray-400 break-all"
                    title={job.fenced_by ?? undefined}
                  >
                    {job.fenced_at ? (job.fenced_by?.slice(0, 24) ?? 'yes') : 'no'}
                  </td>
                  <td className="py-2">
                    <span
                      data-testid="dlq-decision"
                      className={`px-2 py-0.5 rounded border ${DECISION_STYLES[decision]}`}
                    >
                      {decision}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <p className="text-xs text-gray-600 font-mono mt-2">
        GET /admin/jobs?status=dead_letter — the badge is derived from the run&rsquo;s own
        steps, so the lab&rsquo;s seeding is never read as the agent&rsquo;s decision.
      </p>
    </section>
  )
}

// ─────────────────────────────────────────────────────── the briefing (bottom)

function slotLine(slot: AgentBriefingSlot): string {
  return `${slot.category} / ${slot.name} (confidence ${slot.confidence.toFixed(2)}, ${
    slot.addressed ? 'addressed' : 'not addressed'
  })`
}

/** The card's own content, as Markdown, for pasting into an incident channel. */
export function briefingMarkdown(
  briefing: AgentBriefing,
  verification: AgentRunVerification | null,
): string {
  const slots = briefing.incidents
  const lines: string[] = [
    '# Escalation briefing',
    '',
    `- **Final state**: ${briefing.final_state}`,
    `- **Incident**: ${briefing.incident_id}`,
    `- **Alert**: ${briefing.alert_summary}`,
  ]
  if (briefing.escalation_reason) {
    lines.push(`- **Escalation reason**: ${briefing.escalation_reason}`)
  }
  if (briefing.attempted_action) {
    lines.push(
      `- **Attempted action**: ${briefing.attempted_action.tool} ` +
        `${JSON.stringify(briefing.attempted_action.arguments)}`,
    )
  } else {
    lines.push('- **Attempted action**: none — the agent escalated without acting')
  }
  lines.push(
    `- **Verify verdict**: ${
      verification === null
        ? 'none reported'
        : `${verification.verdict}${verification.attempt != null ? ` (attempt ${String(verification.attempt)}${verification.of != null ? ` of ${String(verification.of)}` : ''})` : ''}`
    }`,
  )
  const attribution = briefing.attribution ?? null
  lines.push(
    `- **Recovery attribution**: ${
      attribution === null
        ? 'none recorded'
        : `${attribution.verdict} — ${attribution.resource} via ${attribution.probe_tool}; ${attribution.detail}`
    }`,
  )
  lines.push('')
  lines.push('## Causes')
  lines.push(`- Primary: ${slots?.primary ? slotLine(slots.primary) : 'none named'}`)
  lines.push(
    `- Secondary: ${
      slots?.secondary?.length ? slots.secondary.map(slotLine).join('; ') : 'none'
    }`,
  )
  // Always printed, even when empty: the remainder is the thing a reader must
  // not have to infer from an absence (ADR 0065).
  lines.push(
    `- Unresolved extra: ${
      slots?.unresolved_extra?.length
        ? slots.unresolved_extra.map(slotLine).join('; ')
        : 'none'
    }`,
  )
  if (briefing.findings) lines.push('', '## Findings', briefing.findings)
  if (briefing.recommendation) lines.push('', '## Recommendation', briefing.recommendation)
  lines.push(
    '',
    '## Writer',
    briefing.prose ? briefing.prose : '_no prose — this run was not enriched._',
  )
  return lines.join('\n')
}

function SlotRow({ role, slots }: { role: string; slots: AgentBriefingSlot[] }) {
  return (
    <tr className="align-top">
      <td className="py-1 pr-3 text-gray-500 whitespace-nowrap">{role}</td>
      <td className="py-1 text-gray-300">
        {slots.length === 0 ? (
          <span className="text-gray-600">none</span>
        ) : (
          <ul className="space-y-0.5">
            {slots.map((s, i) => (
              <li key={`${s.category}-${s.name}-${String(i)}`}>
                <span className="font-mono text-xs text-gray-400">{s.category}</span>{' '}
                {s.name}{' '}
                <span className="font-mono text-xs text-gray-500">
                  {s.confidence.toFixed(2)} · {s.addressed ? 'addressed' : 'not addressed'}
                </span>
              </li>
            ))}
          </ul>
        )}
      </td>
    </tr>
  )
}

const ATTRIBUTION_STYLE: Record<string, string> = {
  attributed: 'bg-green-500/20 text-green-200 border-green-500/50',
  cleared_on_its_own: 'bg-amber-500/20 text-amber-200 border-amber-500/50',
  cannot_attribute: 'bg-amber-500/20 text-amber-200 border-amber-500/50',
}

function BriefingCard({
  briefing,
  verification,
}: {
  briefing: AgentBriefing
  verification: AgentRunVerification | null
}) {
  const toast = useToast()
  const slots = briefing.incidents
  const resolved = briefing.final_state === 'resolved'
  const attribution = briefing.attribution ?? null

  async function copy() {
    const ok = await copyToClipboard(briefingMarkdown(briefing, verification))
    if (ok) toast.info('Copied the briefing as Markdown')
    else toast.error('Could not copy the briefing — select it and copy manually')
  }

  return (
    <section
      data-testid="briefing-card"
      aria-labelledby="demo-briefing"
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 mt-4"
    >
      <div className="flex items-center justify-between gap-2 mb-3">
        <h2 id="demo-briefing" className="text-base text-gray-200">
          Escalation briefing
        </h2>
        <div className="flex items-center gap-2">
          <span
            data-testid="briefing-final-state"
            className={`px-2.5 py-0.5 rounded-full text-sm border ${
              resolved
                ? 'bg-green-500/20 text-green-300 border-green-500/40'
                : 'bg-red-500/20 text-red-300 border-red-500/40'
            }`}
          >
            {briefing.final_state}
          </span>
          <button
            onClick={() => void copy()}
            className="text-sm px-2.5 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500"
          >
            Copy as Markdown
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 text-sm">
        <div className="space-y-2">
          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">Alert</p>
            <p className="text-gray-200">{briefing.alert_summary}</p>
          </div>
          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">
              Escalation reason
            </p>
            <p className="text-gray-300">
              {briefing.escalation_reason || <span className="text-gray-600">none given</span>}
            </p>
          </div>
        </div>

        <div className="space-y-2">
          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">
              Attempted action, and how it was judged
            </p>
            {briefing.attempted_action ? (
              <p className="text-sm font-mono text-gray-300 break-all">
                {briefing.attempted_action.tool}{' '}
                {JSON.stringify(briefing.attempted_action.arguments)}
              </p>
            ) : (
              <p className="text-sm text-gray-600">
                None — the agent escalated without acting.
              </p>
            )}
            {/* The verdict is the run's own verify verdict, from the run record —
                not a field of the briefing, which has never carried one. */}
            <p
              data-testid="briefing-verify-verdict"
              className="text-sm text-gray-400 mt-1"
            >
              Verify verdict:{' '}
              {verification === null ? (
                <span className="text-gray-600">none reported</span>
              ) : (
                <span
                  className={`px-1.5 py-0.5 rounded text-xs border font-mono ${verdictStyle(verification.verdict)}`}
                >
                  {verification.verdict}
                  {verification.attempt != null && ` · attempt ${verification.attempt}`}
                  {verification.of != null && ` of ${verification.of}`}
                </span>
              )}
            </p>
          </div>

          <div>
            <p className="text-xs uppercase tracking-wider text-gray-500">
              Recovery attribution (ADR 0071)
            </p>
            {attribution === null ? (
              // A terminal run that never acted will never have one, and saying
              // "none recorded" about it reads as a gap in the record rather than as
              // the consequence of the run's own decision (WO-R3-334).
              <p data-testid="briefing-attribution" className="text-sm text-gray-600">
                {briefing.attempted_action
                  ? 'None recorded — this run’s trajectory carries no attribution read.'
                  : 'No attribution because no action: there is nothing to credit a recovery to.'}
              </p>
            ) : (
              <div data-testid="briefing-attribution">
                <span
                  className={`px-2 py-0.5 rounded text-xs border font-mono ${
                    ATTRIBUTION_STYLE[attribution.verdict] ??
                    'bg-gray-700/40 text-gray-300 border-gray-600'
                  }`}
                >
                  {attribution.verdict}
                </span>
                <p className="text-sm text-gray-300 mt-1">
                  <span className="font-mono text-xs text-gray-500">
                    {attribution.resource}
                  </span>{' '}
                  read by{' '}
                  <span className="font-mono text-xs text-gray-500">
                    {attribution.probe_tool}
                  </span>{' '}
                  · {attribution.acted ? 'the run acted on it' : 'the run did not act on it'}
                </p>
                <p className="text-sm text-gray-400">{attribution.detail}</p>
              </div>
            )}
          </div>
        </div>

        <div>
          <p className="text-xs uppercase tracking-wider text-gray-500 mb-1">
            Causes (ADR 0065 slots)
          </p>
          <table className="w-full text-sm">
            <tbody>
              <SlotRow role="primary" slots={slots?.primary ? [slots.primary] : []} />
              <SlotRow role="secondary" slots={slots?.secondary ?? []} />
              <SlotRow role="unresolved extra" slots={slots?.unresolved_extra ?? []} />
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-3">
        <p className="text-xs uppercase tracking-wider text-gray-500">
          What the writer said
        </p>
        <p className="text-sm text-gray-300 leading-relaxed">
          {briefing.prose ?? (
            <span className="text-gray-600">
              No prose — this run was not enriched, so the deterministic template above
              is the whole briefing.
            </span>
          )}
        </p>
      </div>
    </section>
  )
}

// ──────────────────────────────────────────────────────────────── the page

export default function DemoPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const modeParam = searchParams.get('mode')
  const mode: DemoMode = isDemoMode(modeParam) ? modeParam : 'consumer_outage'
  const wantedRun = searchParams.get('run')
  const now = useNow(1000)
  const [showJobEvents, setShowJobEvents] = useState(false)

  const setParam = useCallback(
    (key: string, value: string) => {
      const params = new URLSearchParams(searchParams)
      params.set(key, value)
      // `replace` so the demo's back button is not a list of toggles.
      setSearchParams(params, { replace: true })
    },
    [searchParams, setSearchParams],
  )

  /** Unpinning matters as much as pinning: without `run` the page follows the take. */
  const clearParam = useCallback(
    (key: string) => {
      const params = new URLSearchParams(searchParams)
      params.delete(key)
      setSearchParams(params, { replace: true })
    },
    [searchParams, setSearchParams],
  )

  // ── the readings, all on one cadence ────────────────────────────────────
  // Every run, not just the active ones: see `adminApi.listAgentRuns`.
  const loadRuns = useCallback(() => adminApi.listAgentRuns(), [])
  const runs = usePolling(loadRuns, POLL_MS, {
    errorMessage: 'Could not read the agent’s runs.',
  })

  const loadLag = useCallback(() => adminApi.consumerLag(), [])
  const lag = usePolling(loadLag, POLL_MS, {
    errorMessage: 'Could not read consumer lag.',
  })

  const loadDlqStats = useCallback(() => adminApi.dlqStats(), [])
  const dlq = usePolling(loadDlqStats, POLL_MS, {
    errorMessage: 'Could not read the dead-letter depth.',
  })

  const loadJobs = useCallback(
    () => adminApi.listJobs({ page: 1, page_size: JOBS_STRIP_ROWS }),
    [],
  )
  const jobs = usePolling(loadJobs, POLL_MS, { errorMessage: 'Could not read jobs.' })

  const loadDlqRows = useCallback(
    () => adminApi.listJobs({ page: 1, page_size: DLQ_ROWS, status: 'dead_letter' }),
    [],
  )
  const dlqRows = usePolling(loadDlqRows, POLL_MS, {
    enabled: mode === 'dlq_backlog',
    errorMessage: 'Could not read the dead-letter rows.',
  })

  /**
   * The operator streams, in one request.
   *
   * The first take's query was unfiltered, and with `make traffic` running that is
   * 40-odd `event.job.*` rows out of every 50 — which buried the agent's rows AND
   * pushed the one `chaos.*` row that states the fault off the page within a minute,
   * so the platform row fell back to `healthy` mid-incident.
   */
  /**
   * How many pages of operator rows to read — one, until the take's opening boundary
   * turns out to be further back than that (WO-R3-336, item 2).
   *
   * The fourth take's ledger showed `kill_consumer 08:18:05` from the take BEFORE the
   * one on screen, and the take label said `take start not in view`: the boundary that
   * opened the take was past the end of the single page of 100 rows the page asked for,
   * so there was no boundary to cut the rows at and every older row leaked in. The page
   * now asks for another page whenever the selected take has no opening boundary and
   * the server says there are more rows, up to a bound — an unbounded walk back through
   * an append-only table on a 2-second poll is not a fix.
   */
  const [auditPages, setAuditPages] = useState(1)
  const loadAudit = useCallback(async () => {
    const pages = []
    for (let page = 1; page <= auditPages; page += 1) {
      pages.push(
        await adminApi.listAuditLogs({
          page,
          page_size: AUDIT_ROWS,
          action_prefix: OPERATOR_STREAMS,
        }),
      )
    }
    const last = pages[pages.length - 1]
    return {
      items: pages.flatMap((p) => p.items),
      // The oldest page's own flag: whether anything the page has not read still exists.
      hasMore: last?.has_next ?? false,
    }
  }, [auditPages])
  const audit = usePolling(loadAudit, POLL_MS, {
    errorMessage: 'Could not read the audit log.',
  })
  const auditRows = useMemo(() => audit.data?.items ?? [], [audit.data])

  // The job lifecycle, fetched only while the toggle is on, so the derivations
  // above can never be crowded out by it.
  const loadJobEvents = useCallback(
    () =>
      adminApi.listAuditLogs({
        page: 1,
        page_size: AUDIT_ROWS,
        action_prefix: JOB_EVENT_STREAM,
      }),
    [],
  )
  const jobEvents = usePolling(loadJobEvents, POLL_MS, {
    enabled: showJobEvents,
    errorMessage: 'Could not read the job events.',
  })

  const loadAlerts = useCallback(() => adminApi.listAlerts(true), [])
  const alerts = usePolling(loadAlerts, SLOW_POLL_MS, {
    errorMessage: 'Could not read alerts.',
  })
  const loadBreakers = useCallback(() => adminApi.circuitBreakers(), [])
  const breakers = usePolling(loadBreakers, SLOW_POLL_MS, {
    errorMessage: 'Could not read circuit breakers.',
  })

  // ── the take, the run, the steps ─────────────────────────────────────────
  //
  // The take NOW RUNNING by default (WO-R3-336), re-evaluated on every poll: a fresh
  // demo starts the page at zero, the run is adopted on the poll after its first
  // report, and a new boundary moves the page on. `?run=` pins a take explicitly, and
  // everything earlier is history the operator chooses from the take selector.
  const selection = useMemo(
    () => selectTake({ runs: runs.data?.items ?? [], audit: auditRows, wanted: wantedRun }),
    [runs.data, auditRows, wantedRun],
  )
  const take = selection.take
  const listedRun = selection.run
  const runId = listedRun?.id ?? null
  const takes = useMemo(
    () => takeOptions({ runs: runs.data?.items ?? [], audit: auditRows }),
    [runs.data, auditRows],
  )

  /**
   * Read another page of audit rows while the take's opening boundary is not in view.
   *
   * Only while the server says there are more rows, and only up to `MAX_AUDIT_PAGES`:
   * a take older than the platform's whole audit window has no boundary to find, and
   * the ledger says so rather than the page walking back forever.
   *
   * The count only ever GROWS. Dropping back to one page the moment the boundary is in
   * view drops the row that put it there, which un-finds it on the next poll and
   * re-finds it on the one after — a page oscillating between two takes on a two-second
   * cadence. An extra page of rows the take filter discards is the cheaper mistake.
   */
  const boundaryInView = take.startAt !== null
  const moreAuditRows = audit.data?.hasMore === true
  useEffect(() => {
    if (boundaryInView || !moreAuditRows) return
    setAuditPages((p) => (p < MAX_AUDIT_PAGES ? p + 1 : p))
    // `audit.data` is in the deps because one page further back may still not hold the
    // boundary: each answer is a chance to decide to read one more, and at the bound
    // the state stops changing, so the walk ends by itself.
  }, [boundaryInView, moreAuditRows, audit.data])

  /**
   * True when the take on screen has been closed and the take after it has reported
   * no run yet — the state the third take's reload was in, and the one sentence that
   * makes "why am I looking at 08:17" answerable.
   */
  const newerTakeRunning = useMemo(() => {
    const endAt = take.endAt
    if (endAt === null) return false
    return !(runs.data?.items ?? []).some((r) => r.started_at > endAt)
  }, [take.endAt, runs.data])

  // The detail carries everything the list summary does not — the ledger above
  // all, which the LISTING omits rather than empties (WO-R3-328).
  const loadRunDetail = useCallback(
    () => (runId === null ? Promise.resolve(null) : adminApi.getAgentRun(runId)),
    [runId],
  )
  const runDetail = usePolling(loadRunDetail, POLL_MS, {
    enabled: runId !== null,
    errorMessage: 'Could not read the run.',
  })
  // The list row is the fallback for the run's own fields while the first detail
  // answer is in flight. It never contributes steps: the listing has none, and an
  // empty list there means "not sent", not "this run made no calls".
  //
  // The detail is used ONLY when it is the detail of the selected run (WO-R3-336). A
  // parked poll keeps its last answer, so without the id check a page that moves on to
  // a fresh take — the whole point of item 1 — would keep rendering the previous take's
  // run in the agent row and the panel, which is the thing the owner saw.
  const detailRun =
    runDetail.data !== null && runId !== null && runDetail.data.id === runId
      ? runDetail.data
      : null
  const run: AgentRun | null = runId === null ? null : (detailRun ?? listedRun)

  // The steps, merged from the two reads that carry them, reset on a run change.
  const [stepStore, setStepStore] = useState<{
    runId: string | null
    steps: AgentRunStepRecord[]
    /** The platform's own cursor, echoed back on the next poll. */
    cursor: number | null
    dropped: number
  }>({ runId: null, steps: [], cursor: null, dropped: 0 })
  // Memoized on the store so the panels' own memos do not re-run every render
  // while a run switch is still in flight.
  const steps = useMemo(
    () => (stepStore.runId === runId ? stepStore.steps : []),
    [stepStore, runId],
  )
  const cursor = stepStore.runId === runId ? stepStore.cursor : null

  const loadSteps = useCallback(
    () => (runId === null ? Promise.resolve(null) : adminApi.agentRunSteps(runId, cursor)),
    [runId, cursor],
  )
  const stepPoll = usePolling(loadSteps, POLL_MS, {
    enabled: runId !== null,
    errorMessage: 'Could not read the run’s steps.',
  })

  useEffect(() => {
    if (runId === null) return
    const tail = stepPoll.data
    const incoming = [...(runDetail.data?.steps ?? []), ...(tail?.steps ?? [])]
    // The platform's cursor, not one computed from what arrived: `next_after_seq`
    // is the highest `seq` STORED, so a poll that returns nothing still advances.
    const nextCursor = tail?.next_after_seq ?? null
    const dropped = tail?.steps_dropped ?? runDetail.data?.steps_dropped ?? 0
    setStepStore((prev) => {
      const base = prev.runId === runId ? prev.steps : []
      if (incoming.length === 0) {
        return prev.runId === runId && prev.cursor === nextCursor && prev.dropped === dropped
          ? prev
          : { runId, steps: base, cursor: nextCursor, dropped }
      }
      const merged = mergeSteps(base, incoming)
      // The detail poll re-sends the whole list every two seconds, so compare
      // contents rather than identity: otherwise every poll replaces the store
      // with an equal array and re-runs every panel's memo for nothing.
      const unchanged =
        prev.runId === runId &&
        prev.cursor === nextCursor &&
        prev.dropped === dropped &&
        merged.length === base.length &&
        merged.every(
          (s, i) =>
            s.seq === base[i].seq &&
            s.at === base[i].at &&
            s.result_excerpt === base[i].result_excerpt &&
            s.outcome === base[i].outcome,
        )
      return unchanged ? prev : { runId, steps: merged, cursor: nextCursor, dropped }
    })
  }, [runId, runDetail.data, stepPoll.data])

  // ── the fault, latched for the take ─────────────────────────────────────
  // Mutating a ref during render is safe here: the update is idempotent, schedules
  // nothing, and the render that sets it already reads the new value.
  //
  // Keyed on the whole take rather than on its opening boundary since WO-R3-334:
  // switching from a closed take to the live one shares no key, so a latch can
  // neither outlive its take nor leak backwards into an earlier one.
  //
  // The EARLIEST successful injection wins since WO-R3-336 item 7: a re-arm is not a new
  // incident, and the fourth take's page measured everything from one.
  const rowFaultAt = useMemo(() => faultInTake(auditRows, take), [auditRows, take])
  const latch = useRef<{ take: string; faultAt: string | null }>({ take: '', faultAt: null })
  if (latch.current.take !== takeKey(take)) {
    latch.current = { take: takeKey(take), faultAt: null }
  }
  if (
    rowFaultAt !== null &&
    (latch.current.faultAt === null || rowFaultAt < latch.current.faultAt)
  ) {
    latch.current.faultAt = rowFaultAt
  }
  const faultAt = latch.current.faultAt

  // ── the metric this mode is about ───────────────────────────────────────
  const dispatcher = useMemo(() => {
    const groups = lag.data?.groups ?? []
    // `live_group` is the fallback rather than the first choice: it names the one
    // group whose number moves, so it is right if the group were ever renamed and
    // wrong to prefer while the named group is present.
    return (
      groups.find((g) => g.consumer_group === DISPATCHER_GROUP) ??
      groups.find((g) => g.consumer_group === lag.data?.live_group) ??
      null
    )
  }, [lag.data])

  const lagValue = dispatcher?.lag_known ? (dispatcher.lag ?? null) : null
  /** The platform's own 15-minute window, oldest first (it arrives newest first). */
  const lagSamples = useMemo<MetricSample[]>(
    () =>
      (dispatcher?.recent_samples ?? [])
        .map((s) => ({ t: new Date(s.measured_at).getTime(), v: s.lag, at: s.measured_at }))
        .sort((a, b) => a.t - b.t),
    [dispatcher?.recent_samples],
  )

  // DLQ depth has no server-side history — `GET /admin/dlq/stats` is one number —
  // so this series is the page's own observation and the caption says so.
  const dlqDepth = dlq.data?.total ?? null
  const [dlqSeries, setDlqSeries] = useState<MetricSample[]>([])
  useEffect(() => {
    if (dlq.data === null || dlqDepth === null) return
    const at = new Date().toISOString()
    setDlqSeries((prev) =>
      [...prev, { t: Date.now(), v: dlqDepth, at }].filter(
        (s) => Date.now() - s.t <= WINDOW_MS,
      ),
    )
    // Trimmed to the same nominal window as the lag chart so the two read alike;
    // this one is the page's own observation either way.
  }, [dlq.data, dlqDepth])

  const metric = MODE_METRICS[mode]
  const metricSamples = mode === 'consumer_outage' ? lagSamples : dlqSeries
  const metricValue = mode === 'consumer_outage' ? lagValue : dlqDepth
  const metricKnown = metricValue !== null
  const metricInsideNow = metricValue !== null ? metricValue <= metric.threshold : false

  /**
   * Recovery, from the platform's own samples rather than from one poll.
   *
   * The first take's rule asked "is the latest reading inside the bar, and was a
   * breach seen" — and the cached lag value reads 42 → 0 → 42 as it ages, so the
   * strip announced a recovery in the middle of the incident. Two consecutive
   * samples inside the bar is the rule now, and while only one is the page says so
   * instead of either lying or going quiet.
   */
  const recovery = useMemo(
    () => metricRecovery(metricSamples, metric.threshold, faultAt),
    [metricSamples, metric.threshold, faultAt],
  )
  const insideSustained = metricSamples.length > 0 ? recovery.sustained : metricInsideNow

  /**
   * The principal every row on this page is measured against.
   *
   * The run says who wrote it (`service_account_id`), so a call by anyone else — the
   * demo runner reading lag under the agent's token, the evaluator's guard probes —
   * is not this run's work (F3/F4). With no run selected there is nothing to compare
   * against and every row counts, which the ledger's own line says.
   */
  const runPrincipalId = run?.service_account_id ?? null

  const platformStations: PlatformStation[] = useMemo(
    () =>
      platformRow({
        audit: auditRows,
        take,
        runPrincipalId,
        faultAt,
        recoveredAt: recovery.recoveredAt,
        metricKnown,
        metricInsideThreshold: insideSustained,
        metricBreachedSinceFault: recovery.breachedAt !== null,
      }),
    [auditRows, take, runPrincipalId, faultAt, recovery, metricKnown, insideSustained],
  )

  // When each reported state reached the platform, so a station whose report arrived
  // in a late burst says so instead of reading as instantaneous (F2).
  const arrivals = useMemo(() => reportArrivals(auditRows, runId), [auditRows, runId])
  const reachedStations = useMemo(
    () => agentRow(run, { arrivals }).filter((s) => s.state !== 'pending').length,
    [run, arrivals],
  )
  const reveal = useStaggeredReveal(runId, reachedStations)
  const agentStations: AgentStation[] = useMemo(
    () => agentRow(run, { arrivals, reveal }),
    [run, arrivals, reveal],
  )

  const platformCurrent = platformStations.find((s) => s.state === 'current') ?? null
  const agentCurrent = agentStations.find((s) => s.state === 'current') ?? null

  // The take's rows, which is the scope of the chart's markers, the ledger and both
  // counts: a row from the take after this one belongs to that take, and the third
  // take's page put seven of them under a run that had already finished.
  const takeRows = useMemo(() => rowsInTake(auditRows, take), [auditRows, take])

  // ── the chart's window, its markers and the ledger ───────────────────────
  // The platform's window is taken from the reply rather than assumed: the reading
  // says how much history it can hold and how far apart the samples are (900 / 60
  // today), so a change of cadence on the platform cannot silently mislabel this
  // chart. What the axis SHOWS is the take, though — see `chartWindow`.
  const windowSeconds = lag.data?.sample_window_seconds ?? WINDOW_MS / 1000
  const sampleInterval = lag.data?.sample_interval_seconds ?? null
  const [fullWindow, setFullWindow] = useState(false)
  const chartSpan = chartWindow({
    faultAt,
    takeStartAt: take.startAt,
    takeEndAt: take.endAt,
    now,
    windowSeconds,
    full: fullWindow,
  })
  const windowStart = chartSpan.start
  const windowEnd = chartSpan.end
  const markers = useMemo(
    () =>
      chartMarkers({
        faultAt,
        recoveredAt: recovery.recoveredAt,
        resetAts: [take.startAt, take.endAt],
        steps,
        // The take's rows, not the page's whole window: the axis can be zoomed out past
        // the take (the `full window` button), and a marker outside it would say the
        // agent or the lab did something in a world this chart is not about.
        audit: takeRows,
        runPrincipalId,
        windowStart,
        windowEnd,
      }),
    [
      faultAt,
      recovery.recoveredAt,
      take.startAt,
      take.endAt,
      steps,
      takeRows,
      runPrincipalId,
      windowStart,
      windowEnd,
    ],
  )

  const ledgerRows = useMemo(
    () => [
      // With the edges: the boundary that opened the take is the divider the ledger
      // starts from, and the one that closed it is the divider it ends on.
      ...rowsInTakeWithEdges(auditRows, take),
      ...(showJobEvents ? rowsInTake(jobEvents.data?.items ?? [], take) : []),
    ],
    [auditRows, showJobEvents, jobEvents.data, take],
  )
  const [showHiddenReads, setShowHiddenReads] = useState(false)
  const ledger: LedgerEntry[] = useMemo(
    () =>
      buildLedger({
        steps,
        audit: ledgerRows,
        showJobEvents,
        runPrincipalId,
        showHiddenReads,
        // Newest at the TOP (the owner's rule from the fourth take), which is this
        // helper's default — the page no longer asks it to reverse.
      }),
    [steps, ledgerRows, showJobEvents, runPrincipalId, showHiddenReads],
  )
  /**
   * The two witnesses' counts, and the rule for when their disagreeing means anything.
   *
   * `now` comes from the page's own ticking clock rather than from `Date.now()` inside
   * the helper, so the ten-second silence is re-evaluated on every tick instead of only
   * when a poll happens to change something.
   */
  const reportedAt = useMemo(
    () => lastReportAt({ steps, audit: takeRows, runId }),
    [steps, takeRows, runId],
  )
  const counts = useMemo(
    () =>
      ledgerCounts({
        steps,
        audit: takeRows,
        runPrincipalId,
        terminal: isTerminalRun(run),
        lastReportAt: reportedAt,
        now,
      }),
    [steps, takeRows, runPrincipalId, run, reportedAt, now],
  )
  const thinkCount = useMemo(() => steps.filter(isThinkStep).length, [steps])

  const briefing = run?.briefing ?? null
  const latestVerification =
    run?.verification ?? runVerifications(run)[runVerifications(run).length - 1] ?? null
  const activeAlert = (alerts.data?.items ?? [])[0] ?? null
  const openBreakers = (breakers.data?.breakers ?? []).filter((b) => b.state !== 'closed')
  const breakersUnknownReason = breakers.data?.unknown_reason ?? null
  // The hook that broke this world — the take's FIRST injection, never a refused guard
  // probe and never the re-arm that followed it (WO-R3-336 item 7).
  const labRow = useMemo(() => faultRowsInTake(auditRows, take)[0] ?? null, [auditRows, take])

  const runsError = runs.error ?? runDetail.error

  return (
    <Layout>
      {/* ── header ───────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-start justify-between gap-4 mb-2">
        <div>
          <h1 className="text-xl font-semibold text-white leading-tight">
            Agent run — {mode === 'consumer_outage' ? 'consumer outage' : 'DLQ backlog'}
          </h1>
          <div className="flex items-center gap-4 mt-1.5 flex-wrap">
            <TakeSelector
              options={takes}
              selectedKey={takeKey(take)}
              onSelect={(option) => {
                // Choosing the live take clears the pin, which is what puts the page
                // back on "whatever this take reports next"; choosing history pins its
                // newest run, so the page stays there while the world moves on.
                if (option.current && option.run === null) clearParam('run')
                else if (option.run !== null) setParam('run', option.run.id)
                else clearParam('run')
              }}
            />
            <RunSelector
              runs={selection.takeRuns}
              selected={listedRun}
              onSelect={(id) => setParam('run', id)}
            />
          </div>
          {/* Its own line: the take label is a sentence, and beside a run selector
              full of ids it pushed the clock and the mode buttons onto a third row. */}
          <div className="flex items-center gap-4 mt-1">
            <TakeLabel
              take={take}
              current={selection.current}
              hasRun={listedRun !== null}
              newerTakeRunning={newerTakeRunning}
            />
            {labRow !== null && (
              <span className="text-xs font-mono text-amber-300/80">
                lab: {labToolName(labRow)}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-start gap-5">
          <FaultClock faultAt={faultAt} now={now} takeEndAt={take.endAt} />
          <div className="flex gap-1 bg-gray-800/60 rounded-lg p-1">
            <button
              onClick={() => setParam('mode', 'consumer_outage')}
              className={`px-3 py-2 rounded text-sm font-medium transition-colors ${
                mode === 'consumer_outage'
                  ? 'bg-gray-700 text-white'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              Consumer outage
            </button>
            <button
              onClick={() => setParam('mode', 'dlq_backlog')}
              className={`px-3 py-2 rounded text-sm font-medium transition-colors ${
                mode === 'dlq_backlog'
                  ? 'bg-gray-700 text-white'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              DLQ backlog
            </button>
          </div>
        </div>
      </div>

      {/* ── the two rows, always both ─────────────────────────────────────── */}
      <div className="space-y-2">
        <PhaseRow
          testId="phase-row-platform"
          title="Platform"
          source="what the platform can see for itself — its audit log and its own metric"
          stations={platformStations}
          tone="platform"
          now={now}
        />
        <PhaseRow
          testId="phase-row-agent"
          title="Agent"
          source="what the responder reports about itself — phase_history (ADR 0035)"
          stations={agentStations}
          tone="agent"
          now={now}
          note={
            listedRun === null
              ? selection.current
                ? 'waiting for this take’s run'
                : 'this take reported no run'
              : null
          }
        />
        <p data-testid="phase-reading" className="text-sm text-gray-400">
          The platform reads{' '}
          <strong className="font-medium text-blue-200">
            {platformCurrent?.label ?? 'nothing yet'}
          </strong>
          ; the agent says{' '}
          <strong className="font-medium text-purple-200">
            {run === null ? 'nothing yet' : agentStateLabel(run)}
          </strong>
          {agentCurrent !== null && agentCurrent.label !== agentStateLabel(run) && (
            <> (station {agentCurrent.label})</>
          )}
          . Neither row is corrected against the other.
          {recovery.insideSince !== null && (
            <span data-testid="recovery-pending" className="text-amber-300">
              {' '}
              The metric has been back inside its bar since{' '}
              {clockTime(recovery.insideSince)} but only for one sample; recovery takes{' '}
              {recovery.required}.
            </span>
          )}
          {!metricKnown && (
            <span className="text-amber-300">
              {' '}
              The metric has no reading right now, so the platform cannot confirm a
              recovery.
            </span>
          )}
        </p>
      </div>

      {/* ── the three panels: world, agent, ledger ────────────────────────── */}
      {/* 21rem, not more: the three panels plus the two rows and the header are
          the top half, and the top half has to be one screen at 1440×900 with no
          scroll — measured in a real browser, not guessed. A station carries a
          "reported" line now (WO-R3-334), so the rows are taller and this is
          shorter. Each panel scrolls inside itself instead. 21rem since WO-R3-336:
          the header carries a take selector as well, and the measurement that
          matters is where the ledger's bottom lands — 886 of a 900-pixel viewport
          on the live state, measured in a real browser. */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-3 mt-2 xl:h-[21rem]">
        <div className="xl:col-span-4 flex flex-col gap-3 min-h-0 overflow-y-auto">
          {mode === 'consumer_outage' ? (
            <MetricChart
              testId="metric-chart-lag"
              title="worker-dispatcher consumer lag"
              unit="messages behind"
              source="GET /admin/consumer-lag"
              caption={`The platform's own samples: ${String(Math.round(windowSeconds / 60))} minutes${
                sampleInterval === null
                  ? ''
                  : `, one every ${String(sampleInterval)}s`
              }, from the reply's own window (WO-R3-328).`}
              value={lagValue}
              known={dispatcher !== null && dispatcher.lag_known}
              unknownReason={
                dispatcher === null
                  ? 'no reading for this group yet'
                  : dispatcher.lag_unknown_reason
              }
              threshold={MODE_METRICS.consumer_outage.threshold}
              thresholdWhy={MODE_METRICS.consumer_outage.rationale}
              samples={lagSamples}
              markers={markers}
              windowStart={windowStart}
              windowEnd={windowEnd}
              zoomed={chartSpan.zoomed}
              onToggleZoom={() => setFullWindow((f) => !f)}
              liveEdge={take.endAt === null}
              error={lag.error}
              onRetry={lag.reload}
            />
          ) : (
            <MetricChart
              testId="metric-chart-dlq"
              title="dead-letter depth"
              unit="rows"
              source="GET /admin/dlq/stats"
              caption="No server-side history exists for this reading, so the line is what this page has observed since it opened."
              value={dlqDepth}
              known={dlqDepth !== null}
              unknownReason="the dead-letter stats endpoint has not answered yet"
              threshold={MODE_METRICS.dlq_backlog.threshold}
              thresholdWhy={MODE_METRICS.dlq_backlog.rationale}
              samples={dlqSeries}
              markers={markers}
              windowStart={windowStart}
              windowEnd={windowEnd}
              zoomed={chartSpan.zoomed}
              onToggleZoom={() => setFullWindow((f) => !f)}
              liveEdge={take.endAt === null}
              error={dlq.error}
              onRetry={dlq.reload}
            />
          )}

          <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-2 text-xs space-y-0.5 shrink-0">
            <p className="text-gray-300">Platform readings</p>
            {alerts.error !== null ? (
              <p className="text-amber-300/80">{alerts.error}</p>
            ) : activeAlert === null ? (
              <p className="text-gray-600">No active alert.</p>
            ) : (
              <p className="text-gray-400">
                <span className="font-mono text-gray-300">{activeAlert.severity}</span>{' '}
                {activeAlert.title}
              </p>
            )}
            {breakers.error !== null ? (
              <p className="text-amber-300/80">{breakers.error}</p>
            ) : breakersUnknownReason !== null ? (
              // The platform could say nothing. Not the same as "nothing is open",
              // and the difference is the whole reason this field exists (ADR 0030).
              <p className="text-amber-300/80">
                Breaker state unknown — {breakersUnknownReason}
              </p>
            ) : (
              <p className="text-gray-400">
                {openBreakers.length === 0
                  ? 'No breaker open among those publishing state.'
                  : openBreakers.map((b) => `${b.name}: ${b.state}`).join(' · ')}
              </p>
            )}
            <p className="text-gray-400">
              {jobs.error !== null
                ? jobs.error
                : `last ${String((jobs.data?.items ?? []).length)} jobs: ${
                    Object.entries(
                      (jobs.data?.items ?? []).reduce<Record<string, number>>((acc, j) => {
                        acc[j.status] = (acc[j.status] ?? 0) + 1
                        return acc
                      }, {}),
                    )
                      .map(([status, n]) => `${status} ${String(n)}`)
                      .join(' · ') || 'none submitted yet'
                  }`}
            </p>
          </div>
        </div>

        <div className="xl:col-span-4 min-h-0 flex flex-col">
          <AgentPanel
            run={run}
            steps={steps}
            takeStartAt={take.startAt}
            currentTake={selection.current}
            loading={runs.loading && runs.data === null}
            error={runsError}
            onRetry={() => {
              runs.reload()
              runDetail.reload()
            }}
          />
        </div>

        <div className="xl:col-span-4 min-h-0 flex flex-col">
          <ActionLedger
            entries={ledger}
            counts={counts}
            thinkCount={thinkCount}
            boundaryInView={
              // Either the boundary is in the rows, or there are no more rows to read —
              // anything else means the page stopped short of it and must say so.
              boundaryInView || !moreAuditRows
            }
            stepsDropped={
              stepStore.runId === runId ? stepStore.dropped : (run?.steps_dropped ?? 0)
            }
            usingAudit={steps.length === 0 && run !== null}
            showJobEvents={showJobEvents}
            onToggleJobEvents={setShowJobEvents}
            showHiddenReads={showHiddenReads}
            onToggleHiddenReads={setShowHiddenReads}
            loading={audit.loading && audit.data === null}
            error={audit.error ?? stepPoll.error}
            onRetry={() => {
              audit.reload()
              stepPoll.reload()
            }}
          />
        </div>
      </div>

      {briefing !== null && (
        <BriefingCard briefing={briefing} verification={latestVerification} />
      )}

      {mode === 'dlq_backlog' && (
        <DlqTable
          rows={dlqRows.data?.items ?? []}
          steps={steps}
          loading={dlqRows.loading && dlqRows.data === null}
          error={dlqRows.error}
          onRetry={dlqRows.reload}
        />
      )}

      <p className="text-xs text-gray-600 mt-4">
        Every number on this page is a human operator&rsquo;s reading over REST. The
        agent&rsquo;s own principal cannot see the <code>chaos.*</code> rows, the{' '}
        <code>lab.world_reset</code> boundary or any of <code>agent_runs</code> — see{' '}
        <code>docs/DEMO.md</code>.
      </p>
    </Layout>
  )
}
