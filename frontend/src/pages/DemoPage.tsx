/**
 * `/demo` — one screen for the live demo (WO-R3-313, owner decisions A–D).
 *
 * The recording is a single take with no tab switching, so everything is on one
 * page and everything refreshes on the same 2s cadence (`usePolling`):
 *
 *   header   the mode, the phase strip, the clock since the fault
 *   left     the world: lag and DLQ sparklines, the jobs strip, the DLQ rows
 *   middle   the agent: state, hypothesis, last step, phase history  (decision C)
 *   right    the audit timeline, colour-coded by who wrote the row
 *   bottom   the escalation briefing, once the agent files it       (decision D)
 *
 * Three rules this page is built around, all of them about not lying on camera:
 *
 *  1. **An absent reading renders as absent, with its reason.** `lag_known:
 *     false` is not lag 0, and a breaker with no published record is missing
 *     from the list rather than closed (ADR 0030). Every panel has a degraded
 *     state as well as an empty one.
 *  2. **The agent's word and the platform's reading are never merged.** They
 *     come from different witnesses (`utils/demoPhase.ts` has the reasoning);
 *     when they disagree the strip shows both, labelled.
 *  3. **One panel's failure is one panel's failure.** Each panel owns its own
 *     request, so a 403 on the agent-run endpoint — which is exactly what a
 *     support-role operator on a stack without WO-R3-312 gets — degrades that
 *     panel and leaves the world and the audit log up.
 *
 * What the two principals can see differs, and that difference is the demo's
 * point: this page reads the REST surface as a HUMAN operator, so it sees the
 * `chaos.*` rows the agent's own MCP reads withhold (ADR 0012) and the
 * `agent_runs` the agent cannot read at all (ADR 0035).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Layout from '../components/Layout'
import ErrorState from '../components/ErrorState'
import { useToast } from '../components/Toast'
import { copyToClipboard } from '../components/TraceId'
import { adminApi } from '../api/admin'
import type { AuditListParams } from '../api/admin'
import { usePolling } from '../hooks/usePolling'
import { formatDate, JOB_TYPE_LABELS, STATUS_COLORS } from '../utils/format'
import {
  AGENT_RUN_REPORT_ACTION,
  AGENT_TOOL_ACTION,
  ACTION_TOOLS,
  LAB_ACTION_PREFIX,
  MODE_METRICS,
  PHASE_LABELS,
  PHASE_STRIP,
  derivePhase,
  dlqDecision,
  isDemoMode,
  newestFaultAt,
  phaseTimeline,
  toolCall,
} from '../utils/demoPhase'
import type { DemoMode, DemoPhase, DlqDecision, PhaseReading } from '../utils/demoPhase'
import type {
  AgentBriefing,
  AgentBriefingSlot,
  AgentRun,
  AuditLog,
  Job,
} from '../types'

/** Everything on this page shares one cadence, so the panels cannot disagree about "now". */
const POLL_MS = 2000
/** The group the `consumer_outage` scenario is about. */
const DISPATCHER_GROUP = 'worker-dispatcher'
/** The sparklines' window. */
const SERIES_WINDOW_MS = 5 * 60 * 1000
/** Enough rows for the whole run to fit on screen without paging on camera. */
const AUDIT_ROWS = 100
const JOBS_STRIP_ROWS = 20

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
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${String(Math.floor((ms % 60_000) / 1000)).padStart(2, '0')}s`
}

function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

interface Sample {
  t: number
  v: number
}

/**
 * The last five minutes of one reading, as this page has observed it.
 *
 * Deliberately client-side. The REST lag reading carries only the handful of
 * samples the metrics loop cached, and DLQ depth carries none at all, so a
 * server-side five-minute series does not exist to be fetched. The honest
 * consequence, stated in `docs/DEMO.md`: a page opened thirty seconds ago shows
 * thirty seconds. `make demo-live` prints the console URL at baseline, before
 * the fault, for exactly this reason.
 *
 * `seed` backfills from whatever samples the reading did bring, so a page
 * opened mid-run is not starting from nothing.
 */
function useSeries(reading: unknown, value: number | null, seed?: Sample[]): Sample[] {
  const [series, setSeries] = useState<Sample[]>([])
  const seeded = useRef(false)

  useEffect(() => {
    if (seeded.current || !seed || seed.length === 0) return
    seeded.current = true
    setSeries((prev) => (prev.length > 0 ? prev : seed))
  }, [seed])

  useEffect(() => {
    // Keyed on the response object's identity: one append per answered poll,
    // including a poll that answered with the same number as the last one (a
    // flat line is a reading, not a gap).
    if (reading === null || reading === undefined || value === null) return
    const now = Date.now()
    setSeries((prev) =>
      [...prev, { t: now, v: value }].filter((s) => now - s.t <= SERIES_WINDOW_MS),
    )
  }, [reading, value])

  return series
}

// ─────────────────────────────────────────────────────────────── header pieces

function PhaseStrip({ reading }: { reading: PhaseReading }) {
  const agent = reading.agent
  const platform = reading.platform
  // The last slot is the terminal PAIR. It names whichever terminal was
  // actually reached, and both when the two witnesses reached different ones.
  const terminals = new Set<DemoPhase>()
  if (agent?.phase === 'escalated' || agent?.phase === 'recovered') terminals.add(agent.phase)
  if (platform.phase === 'recovered') terminals.add('recovered')

  function slotLabel(phase: DemoPhase): string {
    if (phase !== 'recovered') return PHASE_LABELS[phase]
    if (terminals.size === 0) return 'recovered | escalated'
    return [...terminals].map((p) => PHASE_LABELS[p]).join(' | ')
  }

  function marksFor(phase: DemoPhase): string[] {
    const marks: string[] = []
    const agentHere =
      agent !== null &&
      (agent.phase === phase || (phase === 'recovered' && agent.phase === 'escalated'))
    if (agentHere) marks.push('agent')
    if (platform.phase === phase) marks.push('platform')
    return marks
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3">
      <ol aria-label="Demo phase" className="flex flex-wrap items-stretch gap-1">
        {PHASE_STRIP.map((phase) => {
          const marks = marksFor(phase)
          const lit = marks.length > 0
          return (
            <li
              key={phase}
              aria-current={lit ? 'step' : undefined}
              className={`flex-1 min-w-[6.5rem] rounded px-2 py-1.5 border text-center ${
                lit
                  ? 'bg-blue-500/15 border-blue-500/50 text-blue-200'
                  : 'bg-gray-800/40 border-gray-800 text-gray-500'
              }`}
            >
              <span className="block text-[11px] leading-tight">{slotLabel(phase)}</span>
              <span className="mt-1 flex items-center justify-center gap-1 h-3">
                {marks.map((m) => (
                  <span
                    key={m}
                    title={
                      m === 'agent'
                        ? 'what the agent reported about itself'
                        : 'what the platform can see for itself'
                    }
                    className={`text-[9px] uppercase tracking-wider px-1 rounded ${
                      m === 'agent'
                        ? 'bg-purple-500/20 text-purple-300'
                        : 'bg-blue-500/20 text-blue-300'
                    }`}
                  >
                    {m}
                  </span>
                ))}
              </span>
            </li>
          )
        })}
      </ol>

      {reading.disagree && agent && (
        <p
          data-testid="phase-disagreement"
          className="mt-2 text-xs text-amber-300 bg-amber-900/15 border border-amber-800/40 rounded px-3 py-1.5"
        >
          The two sources disagree, so both are shown: the agent says{' '}
          <strong className="font-medium">{PHASE_LABELS[agent.phase]}</strong> (its own
          word: {agent.state}); the platform reads{' '}
          <strong className="font-medium">{PHASE_LABELS[platform.phase]}</strong> from the
          audit log and the metric. Neither is corrected against the other.
        </p>
      )}
      {!platform.metricKnown && (
        <p className="mt-2 text-xs text-gray-500">
          The platform cannot confirm recovery: the metric has no reading right now.
        </p>
      )}
    </div>
  )
}

function FaultClock({ faultAt, now }: { faultAt: string | null; now: number }) {
  if (faultAt === null) {
    return (
      <span className="text-xs text-gray-500 font-mono">no fault injected yet</span>
    )
  }
  const elapsed = Math.max(0, now - new Date(faultAt).getTime())
  return (
    <span data-testid="fault-clock" className="text-xs font-mono text-amber-300">
      T+ {formatMs(elapsed)} since the fault ({clockTime(faultAt)})
    </span>
  )
}

// ────────────────────────────────────────────────────────── the world (left)

function Sparkline({ series, threshold }: { series: Sample[]; threshold: number }) {
  const width = 240
  const height = 40
  if (series.length === 0) {
    return (
      <div className="h-10 flex items-center text-[11px] text-gray-600">
        waiting for the first reading…
      </div>
    )
  }
  const max = Math.max(threshold * 1.3, ...series.map((s) => s.v), 1)
  const first = series[0].t
  const span = Math.max(1, series[series.length - 1].t - first)
  const y = (v: number) => height - (v / max) * height
  const points = series
    .map((s) => `${((s.t - first) / span) * width},${y(s.v).toFixed(1)}`)
    .join(' ')

  return (
    <svg
      role="presentation"
      viewBox={`0 0 ${width} ${height}`}
      className="w-full h-10"
      preserveAspectRatio="none"
    >
      <line
        x1={0}
        x2={width}
        y1={y(threshold)}
        y2={y(threshold)}
        stroke="currentColor"
        className="text-amber-500/60"
        strokeWidth={1}
        strokeDasharray="4 3"
      />
      {series.length === 1 ? (
        <circle cx={0} cy={y(series[0].v)} r={2} className="fill-blue-400" />
      ) : (
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          className="text-blue-400"
          strokeWidth={1.5}
        />
      )}
    </svg>
  )
}

function MetricPanel({
  testId,
  label,
  source,
  value,
  known,
  unknownReason,
  threshold,
  rationale,
  series,
  error,
  onRetry,
}: {
  testId: string
  label: string
  source: string
  value: number | null
  known: boolean
  unknownReason: string | null
  threshold: number
  rationale: string
  series: Sample[]
  error: string | null
  onRetry: () => void
}) {
  const breaching = known && value !== null && value > threshold
  return (
    <div
      data-testid={testId}
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3"
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-gray-300">{label}</span>
        {known && value !== null ? (
          <span
            className={`text-xl font-mono ${breaching ? 'text-red-300' : 'text-green-300'}`}
          >
            {value}
          </span>
        ) : (
          // Never a zero: an absent reading is not a healthy one.
          <span className="text-sm font-mono text-gray-500">unknown</span>
        )}
      </div>
      {!known && (
        <p className="text-[11px] text-amber-300/80 mt-0.5">
          {unknownReason ?? 'the platform did not say why'}
        </p>
      )}
      {error !== null && (
        <ErrorState message={error} onRetry={onRetry} className="py-3" />
      )}
      <Sparkline series={series} threshold={threshold} />
      <p className="text-[10px] text-gray-600 font-mono mt-1">
        threshold {threshold} — {rationale}
      </p>
      <p className="text-[10px] text-gray-700 font-mono">{source}</p>
    </div>
  )
}

function JobsStrip({
  jobs,
  loading,
  error,
  onRetry,
}: {
  jobs: Job[]
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  const counts = jobs.reduce<Record<string, number>>((acc, j) => {
    acc[j.status] = (acc[j.status] ?? 0) + 1
    return acc
  }, {})

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3">
      <h3 className="text-xs text-gray-300 mb-2">Last {JOBS_STRIP_ROWS} jobs</h3>
      {error !== null ? (
        <ErrorState message={error} onRetry={onRetry} className="py-3" />
      ) : loading ? (
        <p className="text-[11px] text-gray-600">Loading…</p>
      ) : jobs.length === 0 ? (
        <p className="text-[11px] text-gray-600">No jobs submitted yet.</p>
      ) : (
        <>
          <div className="flex flex-wrap gap-1">
            {jobs.map((j) => (
              <span
                key={j.id}
                title={`${JOB_TYPE_LABELS[j.type] ?? j.type} · ${j.status} · ${formatDate(j.created_at)}`}
                className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${
                  STATUS_COLORS[j.status] ?? STATUS_COLORS.pending
                }`}
              >
                {j.id.slice(0, 4)}
              </span>
            ))}
          </div>
          <p className="text-[10px] text-gray-600 font-mono mt-2">
            {Object.entries(counts)
              .map(([status, n]) => `${status} ${n}`)
              .join(' · ')}
          </p>
        </>
      )}
    </div>
  )
}

const DECISION_STYLES: Record<DlqDecision, string> = {
  replay: 'bg-blue-500/20 text-blue-200 border-blue-500/40',
  fence: 'bg-red-500/20 text-red-200 border-red-500/40',
  leave: 'bg-gray-700/40 text-gray-400 border-gray-700',
}

function DlqMiniTable({
  rows,
  audit,
  loading,
  error,
  onRetry,
}: {
  rows: Job[]
  audit: AuditLog[]
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3">
      <h3 className="text-xs text-gray-300 mb-2">Dead-letter rows</h3>
      {error !== null ? (
        <ErrorState message={error} onRetry={onRetry} className="py-3" />
      ) : loading ? (
        <p className="text-[11px] text-gray-600">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-[11px] text-gray-600">No dead-letter rows right now.</p>
      ) : (
        <table data-testid="dlq-mini-table" className="w-full text-[11px]">
          <thead>
            <tr className="text-gray-500 text-left">
              <th className="font-medium pb-1">Row</th>
              <th className="font-medium pb-1">Error</th>
              <th className="font-medium pb-1">Hint</th>
              <th className="font-medium pb-1">Triage</th>
              <th className="font-medium pb-1">Fenced</th>
              <th className="font-medium pb-1">Agent decided</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {rows.map((job) => {
              const decision = dlqDecision(job, audit)
              return (
                <tr key={job.id} className="align-top">
                  <td className="py-1.5 pr-2 font-mono text-gray-400">
                    {job.id.slice(0, 8)}
                  </td>
                  <td className="py-1.5 pr-2 text-red-300/90 max-w-[10rem] break-words">
                    {job.error_message ?? '—'}
                  </td>
                  <td className="py-1.5 pr-2 font-mono text-gray-300">
                    {/* null is "not categorised", which is emphatically not
                        replay-safe — say so rather than printing a dash. */}
                    {job.remediation_hint ?? 'not categorised'}
                  </td>
                  <td className="py-1.5 pr-2 font-mono text-gray-400">
                    {job.triage?.root_cause_category ?? 'none'}
                  </td>
                  <td className="py-1.5 pr-2 font-mono text-gray-400">
                    {job.fenced_at ? (job.fenced_by ?? 'yes') : 'no'}
                  </td>
                  <td className="py-1.5">
                    <span
                      data-testid="dlq-decision"
                      className={`px-1.5 py-0.5 rounded border ${DECISION_STYLES[decision]}`}
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
    </div>
  )
}

// ────────────────────────────────────────────────────────── the agent (middle)

function AgentCard({
  run,
  loading,
  error,
  onRetry,
}: {
  run: AgentRun | null
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  if (error !== null) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg">
        <ErrorState message={error} onRetry={onRetry} />
        <p className="px-6 pb-4 text-[11px] text-gray-600 text-center">
          Agent runs are operator-only. A 403 here means this login may not read
          them, or the stack predates the <code>agent_runs</code> table.
        </p>
      </div>
    )
  }
  if (run === null) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-8 text-center">
        <p className="text-sm text-gray-500">
          {loading ? 'Loading…' : 'Waiting for the agent to report a run.'}
        </p>
        <p className="text-[11px] text-gray-600 mt-1">
          The commander reports itself over MCP; nothing here is readable by the
          agent's own principal.
        </p>
      </div>
    )
  }

  const hypothesis = run.current_hypothesis
  const timeline = phaseTimeline(run)

  return (
    <div
      data-testid="agent-card"
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 space-y-3"
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="px-2 py-0.5 rounded-full text-xs border bg-purple-500/20 text-purple-200 border-purple-500/40">
          {run.state}
        </span>
        <span className="text-[10px] font-mono text-gray-600">
          {run.scenario ?? 'no scenario named'}
        </span>
      </div>

      <div>
        <p className="text-[10px] uppercase tracking-wide text-gray-500">
          Current hypothesis
        </p>
        {hypothesis === null ? (
          <p className="text-[11px] text-gray-600">
            None yet — the agent has not ranked a cause.
          </p>
        ) : (
          <>
            <p className="text-sm text-gray-200">{hypothesis.name}</p>
            <p className="text-[11px] font-mono text-gray-500">{hypothesis.category}</p>
            <div className="flex items-center gap-2 mt-1">
              <div className="flex-1 h-1.5 bg-gray-800 rounded overflow-hidden">
                <div
                  className="h-full bg-purple-400"
                  style={{
                    width: `${Math.max(0, Math.min(100, hypothesis.confidence * 100))}%`,
                  }}
                />
              </div>
              <span className="text-[11px] font-mono text-gray-400 w-10 text-right">
                {Math.round(hypothesis.confidence * 100)}%
              </span>
            </div>
          </>
        )}
      </div>

      <div>
        <p className="text-[10px] uppercase tracking-wide text-gray-500">Last step</p>
        {run.last_step === null ? (
          <p className="text-[11px] text-gray-600">No step reported yet.</p>
        ) : (
          <p className="text-[11px] font-mono text-gray-300">
            {run.last_step.kind} · {run.last_step.tool} ·{' '}
            {clockTime(run.last_step.at)}
          </p>
        )}
      </div>

      <div>
        <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
          Phase history
        </p>
        {timeline.length === 0 ? (
          <p className="text-[11px] text-gray-600">No transitions recorded yet.</p>
        ) : (
          <ol className="space-y-1">
            {timeline.map((entry, i) => (
              <li key={`${entry.state}-${entry.at}-${i}`} className="flex gap-2 items-baseline">
                <span className="w-1.5 h-1.5 rounded-full bg-purple-400 shrink-0 mt-1.5" />
                <span className="text-[11px] text-gray-300 flex-1">{entry.state}</span>
                <span className="text-[11px] font-mono text-gray-500">
                  {entry.durationMs === null ? 'ongoing' : formatMs(entry.durationMs)}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────── the audit timeline (right)

type AuditLane = 'lab' | 'agent_action' | 'agent_read' | 'agent_report' | 'human'
type AuditFilter = 'all' | 'agent' | 'lab' | 'human'

/**
 * Which stream a row belongs to.
 *
 * `chaos.*` is checked first and on the ACTION, not the principal: the
 * evaluator is a service account too, so a principal-only test would file the
 * lab's own rows under "agent" and make the fault look like the agent's doing.
 */
export function auditLane(row: AuditLog): AuditLane {
  if (row.action.startsWith(LAB_ACTION_PREFIX)) return 'lab'
  if (row.action === AGENT_RUN_REPORT_ACTION) return 'agent_report'
  if (row.action === AGENT_TOOL_ACTION) {
    const call = toolCall(row)
    return call !== null && ACTION_TOOLS.includes(call.tool)
      ? 'agent_action'
      : 'agent_read'
  }
  if (row.principal_type === 'service_account') return 'agent_action'
  return 'human'
}

const LANE_OF_FILTER: Record<AuditFilter, AuditLane[] | null> = {
  all: null,
  agent: ['agent_action', 'agent_read', 'agent_report'],
  lab: ['lab'],
  human: ['human'],
}

/** What the chip asks the API for, so a filtered view does not page through rows it will drop. */
export function auditQuery(filter: AuditFilter): AuditListParams {
  switch (filter) {
    case 'lab':
      return { action_prefix: LAB_ACTION_PREFIX }
    case 'agent':
      return { principal_type: 'service_account' }
    case 'human':
      return { principal_type: 'user' }
    default:
      return {}
  }
}

const LANE_STYLES: Record<AuditLane, string> = {
  lab: 'border-amber-600/50 bg-amber-900/15',
  agent_action: 'border-blue-600/50 bg-blue-900/15',
  agent_read: 'border-gray-700 bg-gray-800/30',
  agent_report: 'border-purple-700/50 bg-purple-900/15',
  human: 'border-green-700/40 bg-green-900/15',
}

/** Consecutive rows in the same collapsible lane become one group. */
interface AuditGroup {
  lane: AuditLane
  rows: AuditLog[]
}

export function groupAuditRows(rows: AuditLog[]): AuditGroup[] {
  const groups: AuditGroup[] = []
  for (const row of rows) {
    const lane = auditLane(row)
    const last = groups[groups.length - 1]
    const collapsible = lane === 'agent_read' || lane === 'agent_report'
    if (collapsible && last && last.lane === lane) last.rows.push(row)
    else groups.push({ lane, rows: [row] })
  }
  return groups
}

function AuditRow({ row, lane }: { row: AuditLog; lane: AuditLane }) {
  const call = toolCall(row)
  const extra = row.extra_data ?? {}
  const latency = typeof extra.latency_ms === 'number' ? extra.latency_ms : null
  const outcome = typeof extra.outcome === 'string' ? extra.outcome : null
  const args = call && Object.keys(call.args).length > 0 ? call.args : null

  return (
    <div className={`rounded border px-2 py-1.5 ${LANE_STYLES[lane]}`}>
      <div className="flex items-baseline justify-between gap-2">
        <code className="text-[11px] font-mono text-gray-200 break-all">
          {call?.tool ?? row.action}
        </code>
        <span className="text-[10px] font-mono text-gray-500 shrink-0">
          {clockTime(row.created_at)}
        </span>
      </div>
      {call !== null && (
        <p className="text-[10px] font-mono text-gray-600">{row.action}</p>
      )}
      {lane === 'agent_action' && (
        <>
          {args !== null && (
            <pre className="mt-1 text-[10px] font-mono text-gray-400 whitespace-pre-wrap break-all">
              {JSON.stringify(args)}
            </pre>
          )}
          <p className="text-[10px] font-mono text-gray-500">
            {outcome !== null && <span>outcome {outcome}</span>}
            {latency !== null && <span className="ml-2">{latency.toFixed(1)} ms</span>}
          </p>
        </>
      )}
      {lane === 'lab' && args !== null && (
        <pre className="mt-1 text-[10px] font-mono text-amber-200/70 whitespace-pre-wrap break-all">
          {JSON.stringify(args)}
        </pre>
      )}
    </div>
  )
}

function CollapsedGroup({ group }: { group: AuditGroup }) {
  const [open, setOpen] = useState(false)
  const noun = group.lane === 'agent_read' ? 'reads' : 'run reports'
  return (
    <div
      data-testid={group.lane === 'agent_read' ? 'audit-reads-group' : 'audit-reports-group'}
      className={`rounded border px-2 py-1.5 ${LANE_STYLES[group.lane]}`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between text-[11px] text-gray-400 hover:text-gray-200"
      >
        <span>
          {group.rows.length} {noun}
        </span>
        <span className="font-mono text-[10px]">{open ? 'hide' : 'show'}</span>
      </button>
      {open && (
        <div className="mt-1.5 space-y-1">
          {group.rows.map((row) => (
            <AuditRow key={row.id} row={row} lane={group.lane} />
          ))}
        </div>
      )}
    </div>
  )
}

function AuditTimeline({
  rows,
  filter,
  onFilter,
  loading,
  error,
  onRetry,
}: {
  rows: AuditLog[]
  filter: AuditFilter
  onFilter: (f: AuditFilter) => void
  loading: boolean
  error: string | null
  onRetry: () => void
}) {
  const lanes = LANE_OF_FILTER[filter]
  const shown = useMemo(() => {
    const sorted = [...rows].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
    return lanes === null ? sorted : sorted.filter((r) => lanes.includes(auditLane(r)))
  }, [rows, lanes])
  const groups = useMemo(() => groupAuditRows(shown), [shown])

  return (
    <div
      data-testid="audit-timeline"
      className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3"
    >
      <div className="flex flex-wrap gap-1 mb-2">
        {(['all', 'agent', 'lab', 'human'] as const).map((f) => (
          <button
            key={f}
            onClick={() => onFilter(f)}
            className={`px-2 py-0.5 rounded text-[11px] capitalize transition-colors ${
              filter === f
                ? 'bg-blue-500/20 text-blue-300 border border-blue-500/40'
                : 'text-gray-400 border border-gray-800 hover:text-white'
            }`}
          >
            {f}
          </button>
        ))}
      </div>
      {error !== null ? (
        <ErrorState message={error} onRetry={onRetry} className="py-3" />
      ) : loading && rows.length === 0 ? (
        <p className="text-[11px] text-gray-600">Loading…</p>
      ) : groups.length === 0 ? (
        <p className="text-[11px] text-gray-600">No audit rows yet for this filter.</p>
      ) : (
        <div className="space-y-1 max-h-[32rem] overflow-y-auto pr-1">
          {groups.map((group, i) =>
            group.lane === 'agent_read' || group.lane === 'agent_report' ? (
              <CollapsedGroup key={`${group.lane}-${i}`} group={group} />
            ) : (
              <AuditRow key={group.rows[0].id} row={group.rows[0]} lane={group.lane} />
            ),
          )}
        </div>
      )}
      <p className="text-[10px] text-gray-700 font-mono mt-2">
        GET /api/v1/audit/logs — a human operator sees the lab's rows here; the
        agent's own MCP reads do not (ADR 0012).
      </p>
    </div>
  )
}

// ─────────────────────────────────────────────────────── the briefing (bottom)

function slotLine(slot: AgentBriefingSlot): string {
  return `${slot.category} / ${slot.name} (confidence ${slot.confidence.toFixed(2)}, ${
    slot.addressed ? 'addressed' : 'not addressed'
  })`
}

/** The card's own content, as Markdown, for pasting into an incident channel. */
export function briefingMarkdown(briefing: AgentBriefing): string {
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
  lines.push('')
  lines.push('## Causes')
  lines.push(
    `- Primary: ${slots?.primary ? slotLine(slots.primary) : 'none named'}`,
  )
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
  if (briefing.findings) {
    lines.push('', '## Findings', briefing.findings)
  }
  if (briefing.recommendation) {
    lines.push('', '## Recommendation', briefing.recommendation)
  }
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
              <li key={`${s.category}-${s.name}-${i}`}>
                <span className="font-mono text-[11px] text-gray-400">{s.category}</span>{' '}
                {s.name}{' '}
                <span className="font-mono text-[11px] text-gray-500">
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

function BriefingCard({ briefing }: { briefing: AgentBriefing }) {
  const toast = useToast()
  const slots = briefing.incidents
  const resolved = briefing.final_state === 'resolved'

  async function copy() {
    const ok = await copyToClipboard(briefingMarkdown(briefing))
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
        <h2 id="demo-briefing" className="text-sm font-medium text-gray-300">
          Escalation briefing
        </h2>
        <div className="flex items-center gap-2">
          <span
            data-testid="briefing-final-state"
            className={`px-2 py-0.5 rounded-full text-xs border ${
              resolved
                ? 'bg-green-500/20 text-green-300 border-green-500/40'
                : 'bg-red-500/20 text-red-300 border-red-500/40'
            }`}
          >
            {briefing.final_state}
          </span>
          <button
            onClick={() => void copy()}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500"
          >
            Copy as Markdown
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-sm">
        <div className="space-y-2">
          <div>
            <p className="text-[10px] uppercase tracking-wide text-gray-500">Alert</p>
            <p className="text-gray-200">{briefing.alert_summary}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wide text-gray-500">
              Escalation reason
            </p>
            <p className="text-gray-300">
              {briefing.escalation_reason || (
                <span className="text-gray-600">none given</span>
              )}
            </p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wide text-gray-500">
              Attempted action, and how it was judged
            </p>
            {briefing.attempted_action ? (
              <p className="text-[11px] font-mono text-gray-300 break-all">
                {briefing.attempted_action.tool}{' '}
                {JSON.stringify(briefing.attempted_action.arguments)}
              </p>
            ) : (
              <p className="text-[11px] text-gray-600">
                None — the agent escalated without acting.
              </p>
            )}
            {/* The briefing carries no separate verification field. The verdict
                IS the final state, and the reason above is the judgement behind
                it — saying so beats inventing a field the commander never
                sends. */}
            <p className="text-[11px] text-gray-500 mt-0.5">
              Verdict: {briefing.final_state} — there is no separate verification
              field; the reason above is the whole judgement.
            </p>
          </div>
        </div>

        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
            Causes (ADR 0065 slots)
          </p>
          <table className="w-full text-xs">
            <tbody>
              <SlotRow role="primary" slots={slots?.primary ? [slots.primary] : []} />
              <SlotRow role="secondary" slots={slots?.secondary ?? []} />
              <SlotRow role="unresolved extra" slots={slots?.unresolved_extra ?? []} />
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-3">
        <p className="text-[10px] uppercase tracking-wide text-gray-500">
          What the writer said
        </p>
        <p className="text-sm text-gray-300 leading-relaxed">
          {briefing.prose ?? (
            <span className="text-gray-600">
              No prose — this run was not enriched, so the deterministic template
              above is the whole briefing.
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
  const metric = MODE_METRICS[mode]
  const now = useNow(1000)
  const [auditFilter, setAuditFilter] = useState<AuditFilter>('all')

  const setMode = useCallback(
    (next: DemoMode) => {
      const params = new URLSearchParams(searchParams)
      params.set('mode', next)
      // `replace` so the demo's back button is not a list of mode toggles.
      setSearchParams(params, { replace: true })
    },
    [searchParams, setSearchParams],
  )

  // ── the four readings, all on one cadence ───────────────────────────────
  const loadRuns = useCallback(() => adminApi.listAgentRuns({ active: true }), [])
  const runs = usePolling(loadRuns, POLL_MS, {
    errorMessage: 'Could not read the agent’s run.',
  })
  // Newest active run. More than one would mean two incidents at once, which
  // the demo does not stage; the newest is the one on camera.
  const run: AgentRun | null = useMemo(() => {
    const items = runs.data ?? []
    return items.length === 0
      ? null
      : [...items].sort((a, b) => (a.started_at < b.started_at ? 1 : -1))[0]
  }, [runs.data])

  const loadLag = useCallback(() => adminApi.consumerLag(), [])
  const lag = usePolling(loadLag, POLL_MS, {
    errorMessage: 'Could not read consumer lag.',
  })
  const dispatcher = useMemo(
    () => (lag.data ?? []).find((g) => g.consumer_group === DISPATCHER_GROUP) ?? null,
    [lag.data],
  )

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
    () => adminApi.listJobs({ page: 1, page_size: 20, status: 'dead_letter' }),
    [],
  )
  const dlqRows = usePolling(loadDlqRows, POLL_MS, {
    enabled: mode === 'dlq_backlog',
    errorMessage: 'Could not read the dead-letter rows.',
  })

  // The UNFILTERED stream, always. The phase strip and the DLQ badges are
  // derived from it, so a chip the operator clicked must not be able to change
  // what the strip says — that would make the filter look like a state machine.
  const loadAudit = useCallback(
    () => adminApi.listAuditLogs({ page: 1, page_size: AUDIT_ROWS }),
    [],
  )
  const audit = usePolling(loadAudit, POLL_MS, {
    errorMessage: 'Could not read the audit log.',
  })
  // Memoized because the phase strip and the DLQ badges derive from it: a fresh
  // `[]` on every render would re-run every downstream useMemo every render.
  const auditRows = useMemo(() => audit.data?.items ?? [], [audit.data])

  // The filtered view, narrowed server-side. Only fetched when a chip is on;
  // the render filters as well, so the two can never show different rows.
  const viewParams = useMemo(() => auditQuery(auditFilter), [auditFilter])
  const loadAuditView = useCallback(
    () => adminApi.listAuditLogs({ page: 1, page_size: AUDIT_ROWS, ...viewParams }),
    [viewParams],
  )
  const auditView = usePolling(loadAuditView, POLL_MS, {
    enabled: auditFilter !== 'all',
    errorMessage: 'Could not read the audit log.',
  })
  const viewRows = auditFilter === 'all' ? auditRows : (auditView.data?.items ?? [])

  const loadAlerts = useCallback(() => adminApi.listAlerts(true), [])
  const alerts = usePolling(loadAlerts, POLL_MS * 5, {
    errorMessage: 'Could not read alerts.',
  })
  const loadBreakers = useCallback(() => adminApi.circuitBreakers(), [])
  const breakers = usePolling(loadBreakers, POLL_MS * 5, {
    errorMessage: 'Could not read circuit breakers.',
  })

  // ── the metric this mode is about ───────────────────────────────────────
  const lagValue = dispatcher?.lag_known ? (dispatcher.lag ?? null) : null
  const lagSeed = useMemo(
    () =>
      (dispatcher?.recent_samples ?? []).map((s) => ({
        t: new Date(s.measured_at).getTime(),
        v: s.lag,
      })),
    [dispatcher?.recent_samples],
  )
  const lagSeries = useSeries(lag.data, lagValue, lagSeed)
  const dlqDepth = dlq.data?.total ?? null
  const dlqSeries = useSeries(dlq.data, dlqDepth)

  const metricKnown = mode === 'consumer_outage' ? lagValue !== null : dlqDepth !== null
  const metricValue = mode === 'consumer_outage' ? lagValue : dlqDepth
  const metricInside =
    metricValue !== null ? metricValue <= metric.threshold : false
  // Recovery needs a breach to have happened FIRST. The reading is inside its
  // threshold both before the fault lands and after it is fixed, so "inside the
  // bar" on its own would flash `recovered` during the seconds between injecting
  // the fault and it becoming visible — a lie about the one moment the recording
  // exists for (docs/DEMO.md spells this out).
  //
  // Latched rather than derived from timestamps, because the samples are the
  // browser's and the fault time is the platform's. Reset whenever a NEW fault
  // row appears, so a second take in one session does not inherit the first
  // take's recovery. Mutating the ref during render is safe here: the update is
  // idempotent, schedules nothing, and the render that flips it already reads
  // the flipped value.
  const faultAt = useMemo(() => newestFaultAt(auditRows), [auditRows])
  const breach = useRef<{ faultAt: string | null; seen: boolean }>({
    faultAt: null,
    seen: false,
  })
  if (breach.current.faultAt !== faultAt) {
    breach.current = { faultAt, seen: false }
  }
  if (faultAt !== null && metricKnown && metricValue !== null && !metricInside) {
    breach.current.seen = true
  }

  const phase = derivePhase({
    run,
    audit: auditRows,
    metricKnown,
    metricInsideThreshold: metricInside,
    metricBreachedSinceFault: breach.current.seen,
  })

  const briefing = run?.briefing ?? null
  const activeAlert = (alerts.data ?? [])[0] ?? null
  const openBreakers = (breakers.data ?? []).filter((b) => b.state !== 'closed')

  return (
    <Layout>
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div>
          <h1 className="text-lg font-semibold text-white">Live demo</h1>
          <p className="text-sm text-gray-500">
            One screen: the world, the agent, and every row either of them wrote.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <FaultClock faultAt={faultAt} now={now} />
          <div className="flex gap-1 bg-gray-800/60 rounded-lg p-1">
            <button
              onClick={() => setMode('consumer_outage')}
              className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
                mode === 'consumer_outage'
                  ? 'bg-gray-700 text-white'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              Consumer outage
            </button>
            <button
              onClick={() => setMode('dlq_backlog')}
              className={`px-3 py-1 rounded text-sm font-medium transition-colors ${
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

      <PhaseStrip reading={phase} />

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 mt-4">
        <section aria-labelledby="demo-world" className="space-y-3">
          <h2 id="demo-world" className="text-sm font-medium text-gray-300">
            The world
          </h2>
          <MetricPanel
            testId="metric-lag"
            label="worker-dispatcher lag"
            source={MODE_METRICS.consumer_outage.source}
            value={lagValue}
            known={dispatcher !== null && dispatcher.lag_known}
            unknownReason={
              dispatcher === null
                ? 'no reading for this group yet'
                : (dispatcher.unknown_reason ?? null)
            }
            threshold={MODE_METRICS.consumer_outage.threshold}
            rationale={MODE_METRICS.consumer_outage.rationale}
            series={lagSeries}
            error={lag.error}
            onRetry={lag.reload}
          />
          <MetricPanel
            testId="metric-dlq"
            label="DLQ depth"
            source={MODE_METRICS.dlq_backlog.source}
            value={dlqDepth}
            known={dlqDepth !== null}
            unknownReason="the dead-letter stats endpoint has not answered yet"
            threshold={MODE_METRICS.dlq_backlog.threshold}
            rationale={MODE_METRICS.dlq_backlog.rationale}
            series={dlqSeries}
            error={dlq.error}
            onRetry={dlq.reload}
          />
          <JobsStrip
            jobs={jobs.data?.items ?? []}
            loading={jobs.loading && jobs.data === null}
            error={jobs.error}
            onRetry={jobs.reload}
          />
          {mode === 'dlq_backlog' && (
            <DlqMiniTable
              rows={dlqRows.data?.items ?? []}
              audit={auditRows}
              loading={dlqRows.loading && dlqRows.data === null}
              error={dlqRows.error}
              onRetry={dlqRows.reload}
            />
          )}
          <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 text-[11px] space-y-1">
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
            ) : (
              <p className="text-gray-400">
                {/* A breaker with no published record is ABSENT from this list,
                    never reported closed (ADR 0030). */}
                {openBreakers.length === 0
                  ? 'No breaker open among those publishing state.'
                  : openBreakers.map((b) => `${b.name}: ${b.state}`).join(' · ')}
              </p>
            )}
          </div>
        </section>

        <section aria-labelledby="demo-agent" className="space-y-3">
          <h2 id="demo-agent" className="text-sm font-medium text-gray-300">
            The agent
          </h2>
          <AgentCard
            run={run}
            loading={runs.loading && runs.data === null}
            error={runs.error}
            onRetry={runs.reload}
          />
        </section>

        <section aria-labelledby="demo-audit" className="space-y-3">
          <h2 id="demo-audit" className="text-sm font-medium text-gray-300">
            Audit timeline
          </h2>
          <AuditTimeline
            rows={viewRows}
            filter={auditFilter}
            onFilter={setAuditFilter}
            loading={
              auditFilter === 'all'
                ? audit.loading && audit.data === null
                : auditView.loading && auditView.data === null
            }
            error={auditFilter === 'all' ? audit.error : auditView.error}
            onRetry={auditFilter === 'all' ? audit.reload : auditView.reload}
          />
        </section>
      </div>

      {briefing !== null && <BriefingCard briefing={briefing} />}
    </Layout>
  )
}
