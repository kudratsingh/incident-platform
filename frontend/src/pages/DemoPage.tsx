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
  isDemoMode,
  metricRecovery,
  newestFaultAt,
  newestResetAt,
  platformRow,
  runsSinceReset,
  selectRun,
} from '../utils/demoPhase'
import type {
  AgentStation,
  ChartMarker,
  DemoMode,
  MetricSample,
  PlatformStation,
  Station,
} from '../utils/demoPhase'
import {
  budgetMeter,
  buildLedger,
  dlqDecisionFromSteps,
  hypothesesSource,
  labToolName,
  ledgerCounts,
  mergeSteps,
  rankedHypotheses,
  runVerifications,
} from '../utils/demoRun'
import type { LedgerEntry, LedgerKind } from '../utils/demoRun'
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
/** Enough rows for a whole take once the job events are out of the way. */
const AUDIT_ROWS = 100
/**
 * The three streams this page is about, in one request (WO-R3-328).
 *
 * `agent.` is both `agent.tool_invoked` and `agent.run_reported`; `chaos.` is the
 * lab's faults; `lab.` is the reset boundary. Nothing else on the stack writes
 * anything this page derives from, and everything else is what buried it.
 */
const OPERATOR_STREAMS = 'agent.,lab.,chaos.'
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
        no run in this take yet
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
        className="bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-sm text-gray-100 font-mono"
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

function FaultClock({ faultAt, now }: { faultAt: string | null; now: number }) {
  if (faultAt === null) {
    return (
      <div data-testid="fault-clock" className="text-right">
        <p className="text-xs uppercase tracking-wider text-gray-500">since the fault</p>
        <p className="text-2xl font-mono text-gray-500">no fault injected yet</p>
      </div>
    )
  }
  const elapsed = Math.max(0, now - new Date(faultAt).getTime())
  return (
    <div data-testid="fault-clock" className="text-right">
      <p className="text-xs uppercase tracking-wider text-gray-500">since the fault</p>
      <p className="text-3xl font-mono text-amber-300 leading-tight">
        T+ {formatMs(elapsed)}
      </p>
      <p className="text-xs font-mono text-gray-500">injected {clockTime(faultAt)}</p>
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
}: {
  testId: string
  title: string
  source: string
  stations: Station<K>[]
  tone: 'platform' | 'agent'
  now: number
}) {
  return (
    <div
      data-testid={testId}
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3"
    >
      <div className="flex items-baseline gap-3 mb-2">
        <h2
          className={`text-sm font-semibold tracking-wider uppercase ${
            tone === 'platform' ? 'text-blue-300' : 'text-purple-300'
          }`}
        >
          {title}
        </h2>
        <p className="text-xs text-gray-500">{source}</p>
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

/**
 * A clean upper bound just above the data.
 *
 * The ladder is finer than 1/2/5 on purpose: a peak of 53 on a 1/2/5 ladder
 * becomes an axis to 100, which pushes the whole incident into the bottom half
 * of the plot and makes a lag of 42 look like nothing.
 */
function niceMax(value: number): number {
  if (value <= 1) return 1
  const exp = Math.floor(Math.log10(value))
  const base = Math.pow(10, exp)
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (value <= step * base) return step * base
  }
  return 10 * base
}

interface ChartHover {
  sample: MetricSample
  x: number
  y: number
}

/**
 * The metric over the window, with the moments that changed it marked on it.
 *
 * One series, one axis. The DLQ depth in `dlq_backlog` mode is a second CHART
 * rather than a second line: lag runs to tens and the dead-letter depth to five,
 * so one plot with two scales would invent a relationship between them.
 *
 * Reads are deliberately not marked — fifteen ticks on a fifteen-minute chart is a
 * comb, and the ledger is where every call belongs. What the chart marks is the
 * three or four moments that moved the line.
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
  error: string | null
  onRetry: () => void
}) {
  const [hover, setHover] = useState<ChartHover | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)

  // The viewBox's aspect is the plot's aspect: the SVG scales to its column's
  // width, so a wide viewBox in a narrow column letterboxes and the line ends up
  // occupying half the height the panel gave it.
  const W = 480
  const H = 260
  const PAD = { l: 46, r: 14, t: 16, b: 32 }
  const plotW = W - PAD.l - PAD.r
  const plotH = H - PAD.t - PAD.b

  const peak = samples.reduce((m, s) => Math.max(m, s.v), 0)
  const yMax = niceMax(Math.max(threshold * 1.4, peak * 1.15, 1))
  const span = Math.max(1, windowEnd - windowStart)
  const x = (t: number) => PAD.l + ((t - windowStart) / span) * plotW
  const y = (v: number) => PAD.t + plotH - (Math.min(v, yMax) / yMax) * plotH

  const inWindow = samples.filter((s) => s.t >= windowStart && s.t <= windowEnd)
  const points = inWindow.map((s) => `${String(x(s.t))},${y(s.v).toFixed(1)}`).join(' ')
  const last = inWindow[inWindow.length - 1] ?? null
  const breaching = known && value !== null && value > threshold

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
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3"
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
          aria-label={`${title} over the last 15 minutes, threshold ${String(threshold)}`}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {/* The band above the threshold: where the reading is a breach. */}
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
          />
          <text
            x={PAD.l + plotW - 4}
            y={y(threshold) - 5}
            textAnchor="end"
            className="fill-red-300/80"
            style={{ fontSize: '11px' }}
          >
            threshold {threshold}
          </text>

          {/* Axes: hairline, solid, recessive. */}
          <line
            x1={PAD.l}
            x2={PAD.l + plotW}
            y1={PAD.t + plotH}
            y2={PAD.t + plotH}
            className="stroke-gray-700"
            strokeWidth={1}
          />
          <line
            x1={PAD.l}
            x2={PAD.l}
            y1={PAD.t}
            y2={PAD.t + plotH}
            className="stroke-gray-700"
            strokeWidth={1}
          />
          {[0, yMax / 2, yMax].map((tick) => (
            <Fragment key={tick}>
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
          <text
            x={PAD.l}
            y={H - 8}
            className="fill-gray-500"
            style={{ fontSize: '11px' }}
          >
            {clockTime(new Date(windowStart).toISOString())} (−
            {Math.round((windowEnd - windowStart) / 60_000)} min)
          </text>
          <text
            x={PAD.l + plotW}
            y={H - 8}
            textAnchor="end"
            className="fill-gray-500"
            style={{ fontSize: '11px' }}
          >
            now
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

      {/* Identity is never colour alone: every marker on the plot is listed, in
          time order, with its own glyph and time. This is also the chart's table
          view for the markers. */}
      {markers.length > 0 && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 mt-1">
          {markers.map((m) => (
            <li key={`legend-${m.kind}-${m.at}`} className="flex items-center gap-1.5 text-xs">
              <span
                aria-hidden
                className="inline-block w-3.5 h-3.5 rounded-sm text-center leading-[0.875rem] text-gray-950 font-semibold"
                style={{ background: MARKER_STYLE[m.kind].fill, fontSize: '9px' }}
              >
                {MARKER_STYLE[m.kind].glyph}
              </span>
              <span className="text-gray-300">{m.label}</span>
              <span className="font-mono text-gray-500">{clockTime(m.at)}</span>
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-gray-500 mt-1">
        threshold {threshold} — {thresholdWhy}
      </p>
      <p className="text-xs text-gray-600">{caption}</p>

      {/* Every value the hover shows, reachable without hovering. */}
      <details className="mt-1">
        <summary className="text-xs text-gray-500 cursor-pointer">
          samples ({inWindow.length})
        </summary>
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

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.max(0, Math.min(100, confidence * 100))
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2.5 bg-gray-800 rounded overflow-hidden">
        <div className="h-full bg-purple-400" style={{ width: `${String(pct)}%` }} />
      </div>
      <span className="text-sm font-mono text-gray-300 w-12 text-right">
        {Math.round(pct)}%
      </span>
    </div>
  )
}

function Excerpt({ text, testId }: { text: string; testId?: string }) {
  const [open, setOpen] = useState(false)
  const long = text.length > 140
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

function AgentPanel({
  run,
  steps,
  loading,
  error,
  onRetry,
}: {
  run: AgentRun | null
  steps: AgentRunStepRecord[]
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  const hypotheses = rankedHypotheses(run)
  const source = hypothesesSource(run)
  const verifications = runVerifications(run)
  const plan = run?.plan ?? null
  const meter = budgetMeter(run?.budget)
  const lastStep = steps[steps.length - 1] ?? null

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
          {loading ? 'Loading…' : 'Waiting for the responder to report a run.'}
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
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 id="demo-agent" className="sr-only">
            The agent
          </h2>
          <span
            data-testid="agent-state"
            className="inline-block px-3 py-1 rounded-full text-base border bg-purple-500/20 text-purple-100 border-purple-500/50"
          >
            {agentStateLabel(run)}
          </span>
          <p className="text-xs font-mono text-gray-500 mt-1">
            {run.scenario ?? 'no run label'} · {shortId(run.id)}
            {run.finished_at !== null && ` · finished ${clockTime(run.finished_at)}`}
          </p>
        </div>
        <div data-testid="budget-meter" className="w-44">
          <p className="text-xs uppercase tracking-wider text-gray-500">Budget</p>
          {meter.used === null ? (
            <p className="text-sm text-gray-600">not reported</p>
          ) : (
            <>
              <p className="text-lg font-mono text-gray-200">
                {meter.used}
                {meter.max !== null && <span className="text-gray-500"> / {meter.max}</span>}
                <span className="text-xs text-gray-500"> tool calls</span>
              </p>
              {meter.percent !== null && (
                <div className="h-2.5 bg-gray-800 rounded overflow-hidden mt-1">
                  <div
                    className={`h-full ${meter.over ? 'bg-red-400' : 'bg-blue-400'}`}
                    style={{ width: `${String(meter.percent)}%` }}
                  />
                </div>
              )}
              <p className="text-xs font-mono text-gray-500 mt-0.5">
                {meter.tokens === null ? 'tokens —' : `${meter.tokens} tokens`} ·{' '}
                {meter.usd === null ? '$—' : `$${meter.usd.toFixed(4)}`}
                {meter.wallSeconds !== null && ` · ${Math.round(meter.wallSeconds)}s`}
              </p>
            </>
          )}
        </div>
      </div>

      {/* ── hypotheses ─────────────────────────────────────────────────────── */}
      <div>
        <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-1">
          Hypotheses, ranked
        </h3>
        {hypotheses.length === 0 ? (
          <p data-testid="hypotheses-empty" className="text-sm text-gray-600">
            None reported yet — the responder has not ranked a cause.
          </p>
        ) : (
          <ol className="space-y-2">
            {hypotheses.map((h, i) => (
              <li
                key={`${h.category}-${h.name}-${String(i)}`}
                data-testid="hypothesis-row"
                className="bg-gray-950/60 border border-gray-800 rounded px-3 py-2"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-base text-gray-100">{h.name}</span>
                  <span className="text-xs font-mono text-gray-500">{h.category}</span>
                </div>
                <div className="mt-1">
                  <ConfidenceBar confidence={h.confidence} />
                </div>
                {h.reasoning_excerpt ? (
                  <div className="mt-1">
                    <Excerpt text={h.reasoning_excerpt} />
                  </div>
                ) : (
                  <p className="text-xs text-gray-600 mt-1">no reasoning reported</p>
                )}
              </li>
            ))}
          </ol>
        )}
        {source === 'current_only' && (
          <p data-testid="hypotheses-source-note" className="text-xs text-amber-300/80 mt-1">
            Only a top hypothesis was reported, with no ranking and no reasoning — a
            commander older than WO-R3-329 sends one.
          </p>
        )}
      </div>

      {/* ── the plan ───────────────────────────────────────────────────────── */}
      <div>
        <h3 className="text-sm uppercase tracking-wider text-gray-500 mb-1">The plan</h3>
        {plan === null ? (
          <p data-testid="plan-empty" className="text-sm text-gray-600">
            No action planned yet.
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
            Nothing verified yet.
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
  agent_audit: 'border-gray-800 bg-gray-950/50',
  agent_report: 'border-purple-900/60 bg-purple-950/20',
  lab: 'border-amber-700/60 bg-amber-950/25',
  reset: 'border-gray-700 bg-gray-800/30',
  job_event: 'border-gray-800 bg-gray-900/40',
  human: 'border-green-800/50 bg-green-950/20',
}

function StepEntry({ step }: { step: AgentRunStepRecord }) {
  const [open, setOpen] = useState(false)
  // `kind` is an open string on the wire; an unrecognised one is shown verbatim
  // rather than dressed up as a read.
  const badge = KIND_BADGE[step.kind] ?? {
    label: step.kind.toUpperCase(),
    className: 'bg-gray-700/50 text-gray-300 border-gray-600',
  }
  const args = compactJson(step.arguments)
  const excerpt = step.result_excerpt
  const failed = step.outcome != null && step.outcome !== 'success'

  return (
    <div
      data-testid="ledger-entry"
      data-kind={step.kind}
      className={`rounded border px-2.5 py-2 ${LEDGER_TONE.step}`}
    >
      {/* Two lines rather than one: the tool name is the row's subject and a
          narrow column truncates it to `restart…` when everything shares a line. */}
      <div className="flex items-center gap-2">
        <span className={`px-1.5 py-0.5 rounded text-[11px] border font-mono ${badge.className}`}>
          {badge.label}
        </span>
        <span className="text-xs font-mono text-gray-600">#{step.seq}</span>
        <span className="text-xs font-mono text-gray-500 ml-auto">
          {/* Every field but `seq` and `kind` can be null: this is the
              responder's account of its own call and the platform fills nothing in. */}
          {step.at === null ? 'no time reported' : clockTime(step.at)}
        </span>
      </div>
      <p className="text-sm font-mono text-gray-100 break-all leading-snug mt-0.5">
        {step.tool ?? 'no tool reported'}
      </p>
      {args !== null && (
        <pre className="text-xs font-mono text-gray-400 whitespace-pre-wrap break-all mt-1">
          {args}
        </pre>
      )}
      <div className="flex items-center gap-2 mt-1">
        <span
          className={`text-xs font-mono ${failed ? 'text-red-300' : 'text-gray-500'}`}
        >
          {step.outcome ?? 'outcome —'}
        </span>
        {step.latency_ms != null && (
          <span className="text-xs font-mono text-gray-500">
            {step.latency_ms.toFixed(0)} ms
          </span>
        )}
        {excerpt != null && excerpt !== '' && (
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-xs text-blue-300 hover:text-blue-200 ml-auto"
          >
            {open ? 'hide result' : 'result'}
          </button>
        )}
      </div>
      {open && excerpt != null && (
        <pre
          data-testid="ledger-result"
          className="text-xs font-mono text-gray-300 whitespace-pre-wrap break-all mt-1 bg-gray-950 border border-gray-800 rounded p-2"
        >
          {excerpt}
        </pre>
      )}
    </div>
  )
}

function RowEntry({ entry }: { entry: LedgerEntry }) {
  const row = entry.row
  if (!row) return null
  const extra = row.extra_data ?? {}
  const tool = typeof extra.tool_name === 'string' ? extra.tool_name : null
  const args = compactJson(extra.arguments)
  const latency = typeof extra.latency_ms === 'number' ? extra.latency_ms : null

  return (
    <div
      data-testid="ledger-entry"
      data-kind={entry.kind}
      className={`rounded border px-2.5 py-2 ${LEDGER_TONE[entry.kind]}`}
    >
      <div className="flex items-center gap-2">
        <span
          className={`px-1.5 py-0.5 rounded text-[11px] border font-mono ${
            entry.kind === 'lab'
              ? 'bg-amber-500/25 text-amber-200 border-amber-500/50'
              : entry.kind === 'human'
                ? 'bg-green-500/20 text-green-200 border-green-600/50'
                : 'bg-gray-700/50 text-gray-300 border-gray-600'
          }`}
        >
          {entry.kind === 'lab'
            ? 'LAB'
            : entry.kind === 'human'
              ? 'HUMAN'
              : entry.kind === 'job_event'
                ? 'JOB'
                : entry.kind === 'agent_report'
                  ? 'REPORT'
                  : 'AGENT'}
        </span>
        <span className="text-xs font-mono text-gray-500 ml-auto">
          {clockTime(row.created_at)}
        </span>
      </div>
      <p className="text-sm font-mono text-gray-100 break-all leading-snug mt-0.5">
        {tool ?? row.action}
      </p>
      {tool !== null && <p className="text-xs font-mono text-gray-600">{row.action}</p>}
      {args !== null && (
        <pre
          className={`text-xs font-mono whitespace-pre-wrap break-all mt-1 ${
            entry.kind === 'lab' ? 'text-amber-200/80' : 'text-gray-400'
          }`}
        >
          {args}
        </pre>
      )}
      {latency !== null && (
        <p className="text-xs font-mono text-gray-500 mt-0.5">
          {latency.toFixed(0)} ms — the audit log records no result (WO-R3-328)
        </p>
      )}
    </div>
  )
}

/**
 * The boundary, drawn as a line rather than an event.
 *
 * Grey and with no actor: nothing happened to the world here. Everything below it
 * is a take that is over — which is what the page needed to be able to say, because
 * `audit_logs` is append-only and those rows never leave.
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
  stepsDropped,
  usingAudit,
  showJobEvents,
  onToggleJobEvents,
  loading,
  error,
  onRetry,
}: {
  entries: LedgerEntry[]
  counts: { steps: number; calls: number; auditCalls: number; agreed: boolean }
  stepsDropped: number
  usingAudit: boolean
  showJobEvents: boolean
  onToggleJobEvents: (next: boolean) => void
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  return (
    <section
      data-testid="action-ledger"
      aria-labelledby="demo-ledger"
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 flex flex-col min-h-0"
    >
      <div className="flex items-start justify-between gap-2">
        <h2 id="demo-ledger" className="text-base text-gray-200">
          Action ledger
        </h2>
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

      <p data-testid="ledger-counts" className="text-xs text-gray-500 mt-0.5">
        {counts.steps} steps reported · {counts.auditCalls} calls the platform recorded
        {!counts.agreed && (
          <span className="text-amber-300">
            {' '}
            — the two do not agree, so the reporter is behind or has stopped
          </span>
        )}
      </p>
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
        <div className="space-y-1.5 mt-2 overflow-y-auto pr-1 flex-1 min-h-0">
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

      <p className="text-xs text-gray-600 font-mono mt-2">
        …/steps + /audit/logs ({OPERATOR_STREAMS})
      </p>
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
              <p data-testid="briefing-attribution" className="text-sm text-gray-600">
                None recorded — this run&rsquo;s trajectory carries no attribution read.
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
  const loadAudit = useCallback(
    () =>
      adminApi.listAuditLogs({
        page: 1,
        page_size: AUDIT_ROWS,
        action_prefix: OPERATOR_STREAMS,
      }),
    [],
  )
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

  // ── the boundary, the run, the steps ────────────────────────────────────
  const resetAt = useMemo(() => newestResetAt(auditRows), [auditRows])
  const takeRuns = useMemo(
    () => runsSinceReset(runs.data?.items ?? [], resetAt),
    [runs.data, resetAt],
  )
  const listedRun = useMemo(() => selectRun(takeRuns, wantedRun), [takeRuns, wantedRun])
  const runId = listedRun?.id ?? null

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
  const run: AgentRun | null = runDetail.data ?? listedRun

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
  const rowFaultAt = useMemo(() => newestFaultAt(auditRows), [auditRows])
  const latch = useRef<{ take: string; faultAt: string | null }>({ take: '', faultAt: null })
  if (latch.current.take !== (resetAt ?? '')) {
    latch.current = { take: resetAt ?? '', faultAt: null }
  }
  if (
    rowFaultAt !== null &&
    (latch.current.faultAt === null || rowFaultAt > latch.current.faultAt)
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

  const platformStations: PlatformStation[] = useMemo(
    () =>
      platformRow({
        audit: auditRows,
        faultAt,
        recoveredAt: recovery.recoveredAt,
        metricKnown,
        metricInsideThreshold: insideSustained,
        metricBreachedSinceFault: recovery.breachedAt !== null,
      }),
    [auditRows, faultAt, recovery, metricKnown, insideSustained],
  )
  const agentStations: AgentStation[] = useMemo(() => agentRow(run), [run])

  const platformCurrent = platformStations.find((s) => s.state === 'current') ?? null
  const agentCurrent = agentStations.find((s) => s.state === 'current') ?? null

  // ── the chart's markers and the ledger ──────────────────────────────────
  // The window is the platform's, taken from the reply rather than assumed: the
  // reading says how much history it can hold and how far apart the samples are
  // (900 / 60 today), so the axis is labelled from the answer and a change of
  // cadence on the platform does not silently mislabel this chart.
  const windowSeconds = lag.data?.sample_window_seconds ?? WINDOW_MS / 1000
  const sampleInterval = lag.data?.sample_interval_seconds ?? null
  const windowEnd = now
  const windowStart = windowEnd - windowSeconds * 1000
  const markers = useMemo(
    () =>
      chartMarkers({
        faultAt,
        recoveredAt: recovery.recoveredAt,
        resetAt,
        steps,
        audit: auditRows,
        windowStart,
        windowEnd,
      }),
    [faultAt, recovery.recoveredAt, resetAt, steps, auditRows, windowStart, windowEnd],
  )

  const ledgerRows = useMemo(
    () => [...auditRows, ...(showJobEvents ? (jobEvents.data?.items ?? []) : [])],
    [auditRows, showJobEvents, jobEvents.data],
  )
  const ledger: LedgerEntry[] = useMemo(
    () => buildLedger({ steps, audit: ledgerRows, showJobEvents }),
    [steps, ledgerRows, showJobEvents],
  )
  const counts = useMemo(
    () => ledgerCounts({ steps, audit: auditRows }),
    [steps, auditRows],
  )

  const briefing = run?.briefing ?? null
  const latestVerification =
    run?.verification ?? runVerifications(run)[runVerifications(run).length - 1] ?? null
  const activeAlert = (alerts.data?.items ?? [])[0] ?? null
  const openBreakers = (breakers.data?.breakers ?? []).filter((b) => b.state !== 'closed')
  const breakersUnknownReason = breakers.data?.unknown_reason ?? null
  const labRow = useMemo(
    () => auditRows.find((r) => r.action.startsWith('chaos.')) ?? null,
    [auditRows],
  )

  const runsError = runs.error ?? runDetail.error

  return (
    <Layout>
      {/* ── header ───────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-start justify-between gap-4 mb-3">
        <div>
          <h1 className="text-2xl font-semibold text-white leading-tight">
            Agent run — {mode === 'consumer_outage' ? 'consumer outage' : 'DLQ backlog'}
          </h1>
          <div className="flex items-center gap-4 mt-1.5">
            <RunSelector
              runs={takeRuns}
              selected={listedRun}
              onSelect={(id) => setParam('run', id)}
            />
            {labRow !== null && (
              <span className="text-xs font-mono text-amber-300/80">
                lab: {labToolName(labRow)}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-start gap-5">
          <FaultClock faultAt={faultAt} now={now} />
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
      {/* 26rem, not more: the three panels plus the two rows and the header are
          the top half, and the top half has to be one screen at 1440×900 with no
          scroll. Each panel scrolls inside itself instead. */}
      <div className="grid grid-cols-1 xl:grid-cols-12 gap-3 mt-3 xl:h-[26rem]">
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
              error={dlq.error}
              onRetry={dlq.reload}
            />
          )}

          <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 text-sm space-y-1">
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

        <div className="xl:col-span-5 min-h-0 flex flex-col">
          <AgentPanel
            run={run}
            steps={steps}
            loading={runs.loading && runs.data === null}
            error={runsError}
            onRetry={() => {
              runs.reload()
              runDetail.reload()
            }}
          />
        </div>

        <div className="xl:col-span-3 min-h-0 flex flex-col">
          <ActionLedger
            entries={ledger}
            counts={counts}
            stepsDropped={
              stepStore.runId === runId ? stepStore.dropped : (run?.steps_dropped ?? 0)
            }
            usingAudit={steps.length === 0 && run !== null}
            showJobEvents={showJobEvents}
            onToggleJobEvents={setShowJobEvents}
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
