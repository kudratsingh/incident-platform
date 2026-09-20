/**
 * Where the /demo page's phase strip gets its reading, and how it keeps two
 * sources apart (WO-R3-313, owner decision C).
 *
 * The strip has seven stations and two witnesses:
 *
 *   healthy → fault injected → agent investigating → agent planning
 *           → agent remediating → verifying → recovered | escalated
 *
 *  - **the agent's own word**: the active `agent_run`'s `state` (WO-R3-312).
 *    This is the only source that can tell investigating from planning from
 *    remediating, because a plan leaves no mark on the platform at all — the
 *    agent thinking is invisible from the outside.
 *  - **the platform's own record**: the audit log (a `chaos.*` row is the lab
 *    injecting the fault, `agent.tool_invoked` rows are the agent's footprint)
 *    plus the metric the scenario is about. This source can be trusted about
 *    two things the agent cannot assert: that a fault was really injected, and
 *    that the world really recovered.
 *
 * The rule is that neither overwrites the other. When they name different
 * stations the page shows both, labelled, and merges nothing — a merged
 * reading would put a state on screen that neither witness ever asserted, and
 * the disagreement is the interesting part: it is the shape of every incident
 * where the agent believes it fixed something it did not (INC-001..003).
 *
 * **A reset is a boundary, and both witnesses are read only after it**
 * (WO-R3-327). The audit log is append-only, so the rows of a previous take
 * never go away: after `make eval-reset` the newest `chaos.*` row was still the
 * last take's kill, and a freshly wiped world opened this page at `agent
 * remediating` with a clock counting from an incident that no longer existed.
 * The reset now appends one `lab.world_reset` row, and everything here reads
 * rows and runs newer than the newest one. Nothing infers the boundary: it is
 * stated, on the platform's own clock, in the same append-only place as the rows
 * it bounds.
 *
 * Everything here is a pure function of its inputs so the whole strip can be
 * driven from a fixture through every transition.
 */

import type {
  AgentRun,
  AgentRunState,
  AgentRunStepRecord,
  AuditLog,
  Job,
} from '../types'

export type DemoMode = 'consumer_outage' | 'dlq_backlog'

export const DEMO_MODES: readonly DemoMode[] = ['consumer_outage', 'dlq_backlog']

export function isDemoMode(value: string | null): value is DemoMode {
  return value === 'consumer_outage' || value === 'dlq_backlog'
}

export type DemoPhase =
  | 'healthy'
  | 'fault_injected'
  | 'agent_investigating'
  | 'agent_planning'
  | 'awaiting_approval'
  | 'agent_remediating'
  | 'verifying'
  | 'recovered'
  | 'escalated'

/**
 * The stations, in render order.
 *
 * `awaiting_approval` gets a station of its own even though the approvals
 * subsystem is unbuilt and no run reaches it today. The nine reportable states
 * are the commander's own `IncidentState` values with no mapping layer, so this
 * one WILL arrive the moment Tier-2 approvals ship — and a state with nowhere to
 * land is a state that silently reads as something else. An empty station on
 * screen is the cheaper mistake.
 *
 * `escalated` is deliberately absent: it shares the last slot with `recovered`
 * (they are the two terminals, not two steps), and the strip labels that slot
 * with whichever one was actually reached.
 */
export const PHASE_STRIP: readonly DemoPhase[] = [
  'healthy',
  'fault_injected',
  'agent_investigating',
  'agent_planning',
  'awaiting_approval',
  'agent_remediating',
  'verifying',
  'recovered',
]

export const PHASE_LABELS: Record<DemoPhase, string> = {
  healthy: 'healthy',
  fault_injected: 'fault injected',
  agent_investigating: 'agent investigating',
  agent_planning: 'agent planning',
  awaiting_approval: 'awaiting approval',
  agent_remediating: 'agent remediating',
  verifying: 'verifying',
  recovered: 'recovered',
  escalated: 'escalated',
}

/** The audit action prefix the lab injects faults under. Withheld from the agent's own MCP reads (ADR 0012); a human operator reads it over REST. */
export const LAB_ACTION_PREFIX = 'chaos.'
/** Every MCP call the agent makes, read or action alike. */
export const AGENT_TOOL_ACTION = 'agent.tool_invoked'
/** What the commander reports about its own run (ADR 0035). */
export const AGENT_RUN_REPORT_ACTION = 'agent.run_reported'
/**
 * The boundary `make eval-reset` appends — one row per reset, payload = its counters
 * (platform WO-R3-327, ADR 0012's 2026-09-20 amendment).
 *
 * Its own prefix rather than `chaos.` for the reason this page is the reason: the
 * newest `chaos.*` row IS the fault here, so a reset filed under that prefix would
 * be read as one. Withheld from the agent beside `chaos.`; this page reads REST as
 * a human operator, so it sees it.
 */
export const WORLD_RESET_ACTION = 'lab.world_reset'

/**
 * The Tier-1 action tools.
 *
 * Membership is what separates "the agent is looking" from "the agent has
 * acted" on the platform's side, and an action is the only one of the two that
 * can be inferred without the agent's word. Kept as a closed list rather than
 * a name pattern because `list_dlq_messages` and `replay_dlq_by_ids` differ by
 * intent, not by spelling.
 */
export const ACTION_TOOLS: readonly string[] = [
  'restart_consumer_group',
  'replay_dlq_by_ids',
  'replay_dlq_by_category',
  'replay_dlq_messages',
  'mark_dlq_permanent',
  'pause_dag',
  'invalidate_cache_key',
]

interface ToolCall {
  tool: string
  args: Record<string, unknown>
  at: string
}

/** The `{tool_name, arguments}` an `*.tool_invoked` row carries in `extra_data`. */
export function toolCall(row: AuditLog): ToolCall | null {
  const extra = row.extra_data
  if (!extra) return null
  const tool = extra.tool_name
  if (typeof tool !== 'string') return null
  const args = extra.arguments
  return {
    tool,
    args: args && typeof args === 'object' ? (args as Record<string, unknown>) : {},
    at: row.created_at,
  }
}

function isLabRow(row: AuditLog): boolean {
  return row.action.startsWith(LAB_ACTION_PREFIX)
}

function isAgentToolRow(row: AuditLog): boolean {
  return row.action === AGENT_TOOL_ACTION
}

/** The one row that says a take is over. */
export function isResetRow(row: AuditLog): boolean {
  return row.action === WORLD_RESET_ACTION
}

function newest(rows: AuditLog[]): AuditLog | null {
  let best: AuditLog | null = null
  for (const row of rows) {
    if (best === null || row.created_at > best.created_at) best = row
  }
  return best
}

/**
 * When the world was last reset, by the platform's own clock — or null on a stack
 * that has not reset since the page's window of rows began.
 *
 * Null is "no boundary in what I can see", which is also what a pre-WO-R3-327 stack
 * looks like: the page then behaves exactly as it did before, reading the whole
 * history. That is the honest fallback — an inferred boundary would be a guess about
 * which rows to discard.
 */
export function newestResetAt(audit: AuditLog[]): string | null {
  return newest(audit.filter(isResetRow))?.created_at ?? null
}

/**
 * The rows of the current take: strictly newer than the boundary.
 *
 * Strictly, not at-or-after, so a row sharing the boundary's timestamp belongs to the
 * take being closed. The reset writes its row last, after every restoring step, so
 * anything simultaneous with it is the world it just wound up.
 */
function sinceReset(audit: AuditLog[], resetAt: string | null): AuditLog[] {
  if (resetAt === null) return audit
  return audit.filter((row) => row.created_at > resetAt)
}

// ── the agent's own word ──────────────────────────────────────────────────────

export interface AgentPhaseReading {
  /** The station the strip highlights. */
  phase: DemoPhase
  /** The run's own state word, which can be finer than the station. */
  state: AgentRunState
}

/**
 * The nine reportable states onto eight stations.
 *
 * Two states share a station, and both keep their own word in the agent card:
 *
 *  - `triage` shares `agent investigating` — triage IS the first read, and a
 *    station for it would be lit for a second or two at most;
 *  - `failed` shares `escalated` — the strip's terminal pair is recovered |
 *    escalated, and a failed run is the not-recovered one. "The run broke" and
 *    "the agent handed over" are different things to a human watching, which is
 *    why the card shows `failed` while the strip lights the terminal.
 */
const STATE_TO_PHASE: Record<AgentRunState, DemoPhase> = {
  triage: 'agent_investigating',
  investigating: 'agent_investigating',
  planning: 'agent_planning',
  awaiting_approval: 'awaiting_approval',
  remediating: 'agent_remediating',
  verifying: 'verifying',
  resolved: 'recovered',
  escalated: 'escalated',
  failed: 'escalated',
}

export function agentPhase(run: AgentRun | null): AgentPhaseReading | null {
  if (run === null) return null
  // An unrecognised state (a platform enum that grew) parks on the first
  // agent station rather than throwing; the card shows the real word.
  const phase = STATE_TO_PHASE[run.state] ?? 'agent_investigating'
  return { phase, state: run.state }
}

/**
 * The run this take is about: the newest one the boundary has not already closed out.
 *
 * `make eval-reset` closes every open run as `failed` with a `closed_by: reset` marker
 * (platform WO-R3-315), so the previous take's runs are finished at or before the
 * boundary and are dropped here. A run that is still OPEN and older than the boundary
 * is deliberately KEPT: it is either a race with the reset's own sweep or a responder
 * the reset could not reach, and on camera that is a disagreement worth seeing rather
 * than a row to hide.
 *
 * Newest by `started_at`. More than one live run would mean two incidents at once,
 * which the demo does not stage — but picking arbitrarily would be worse than picking
 * the latest.
 */
export function runsSinceReset(runs: AgentRun[], resetAt: string | null): AgentRun[] {
  return runs
    .filter((r) => resetAt === null || r.finished_at === null || r.finished_at > resetAt)
    .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
}

export function runSinceReset(
  runs: AgentRun[],
  resetAt: string | null,
): AgentRun | null {
  return runsSinceReset(runs, resetAt)[0] ?? null
}

/**
 * Which run the page is showing — `?run=<id>` when it names one of this take's
 * runs, the newest otherwise.
 *
 * A `?run=` the list does not carry falls back rather than emptying the page: a
 * link pasted from a previous session, or one whose run the boundary has closed
 * out, should show the current run and not a blank screen. `runs` is expected in
 * the order `runsSinceReset` returns — newest first.
 */
export function selectRun(runs: AgentRun[], wanted: string | null): AgentRun | null {
  if (runs.length === 0) return null
  if (wanted !== null && wanted !== '') {
    const found = runs.find((r) => r.id === wanted)
    if (found) return found
  }
  return runs[0]
}

// ── the platform's own record ─────────────────────────────────────────────────

export interface PlatformPhaseInput {
  /** The audit rows the page has, newest-first or not — order is derived. */
  audit: AuditLog[]
  /**
   * The fault this take is about, latched by the page (WO-R3-330).
   *
   * Omit it and the fault is derived from the rows, which is what every caller
   * did before the first live take showed why that is not enough: the traffic
   * loop writes `event.job.*` rows faster than anything else on the stack, so the
   * one `chaos.*` row falls off the page of rows within a minute and the reading
   * fell back to `healthy` in the middle of the incident. A latched value is
   * never allowed to outlive its take — a fault at or before the boundary is
   * dropped here, and a newer row in the rows themselves always wins.
   */
  faultAt?: string | null
  /** False when the metric has no reading (e.g. `lag_known: false`). */
  metricKnown: boolean
  /** True when the reading is inside the scenario's threshold right now. */
  metricInsideThreshold: boolean
  /**
   * True once the metric has been observed OUTSIDE its threshold at or after
   * the fault. Without it, "inside the threshold" is ambiguous: lag is 0 both
   * before the kill lands and after the restart works, so a fault that has not
   * yet become visible would read as a recovery.
   */
  metricBreachedSinceFault: boolean
}

export interface PlatformPhaseReading {
  phase: DemoPhase
  /** When the lab injected the fault, by the platform's own clock. */
  faultAt: string | null
  /** When the world was last reset — the boundary this reading was taken after. */
  resetAt: string | null
  metricKnown: boolean
}

/**
 * When the lab last injected a fault *in the current take*, by the platform's own
 * clock.
 *
 * Rows older than the newest `lab.world_reset` are a previous take and are not
 * candidates, which is the whole of WO-R3-327: without that, a reset world still
 * reported the last take's kill as its fault, and the header counted a clock from it.
 *
 * Exported because the page needs it BEFORE it can decide whether a breach has
 * been observed — the breach latch is per-fault, so a second take in one session
 * starts clean rather than inheriting the first take's recovery.
 */
export function newestFaultAt(audit: AuditLog[]): string | null {
  const resetAt = newestResetAt(audit)
  return newest(sinceReset(audit, resetAt).filter(isLabRow))?.created_at ?? null
}

/**
 * The fault of the current take: the latch and the rows, whichever is newer,
 * and neither one if it belongs to a take the boundary has closed.
 */
function latchedFaultAt(
  latched: string | null | undefined,
  derived: string | null,
  resetAt: string | null,
): string | null {
  const candidates = [latched ?? null, derived].filter(
    (v): v is string => v !== null && (resetAt === null || v > resetAt),
  )
  if (candidates.length === 0) return null
  return candidates.reduce((a, b) => (a > b ? a : b))
}

export function platformPhase(input: PlatformPhaseInput): PlatformPhaseReading {
  const { metricKnown, metricInsideThreshold, metricBreachedSinceFault } = input
  // Everything below reads the current take only. A boundary discards the previous
  // take's fault, its investigation AND its remediation together — crediting one
  // take's `restart_consumer_group` to the next one's fault would be the same lie in
  // a different station.
  const resetAt = newestResetAt(input.audit)
  const audit = sinceReset(input.audit, resetAt)
  const faultAt = latchedFaultAt(
    input.faultAt,
    newest(audit.filter(isLabRow))?.created_at ?? null,
    resetAt,
  )

  if (faultAt === null) {
    // No lab row: nothing was injected, whatever else is happening. A healthy
    // world with an agent poking at it is still a healthy world.
    return { phase: 'healthy', faultAt: null, resetAt, metricKnown }
  }

  // Recovery is the one thing the platform can assert over the agent, and it
  // needs all three: a reading, a breach that really happened, and the reading
  // back inside the bar.
  if (metricKnown && metricBreachedSinceFault && metricInsideThreshold) {
    return { phase: 'recovered', faultAt, resetAt, metricKnown }
  }

  const sinceFault = audit.filter((r) => isAgentToolRow(r) && r.created_at >= faultAt)
  const acted = sinceFault.some((row) => {
    const call = toolCall(row)
    return call !== null && ACTION_TOOLS.includes(call.tool)
  })
  if (acted) return { phase: 'agent_remediating', faultAt, resetAt, metricKnown }
  if (sinceFault.length > 0) {
    return { phase: 'agent_investigating', faultAt, resetAt, metricKnown }
  }
  return { phase: 'fault_injected', faultAt, resetAt, metricKnown }
}

// ── the two together ─────────────────────────────────────────────────────────

export interface PhaseReading {
  /** Null until the agent has reported a run. */
  agent: AgentPhaseReading | null
  platform: PlatformPhaseReading
  /** True when the two witnesses name different stations. */
  disagree: boolean
}

export function derivePhase(
  input: PlatformPhaseInput & { run: AgentRun | null },
): PhaseReading {
  const agent = agentPhase(input.run)
  const platform = platformPhase(input)
  return {
    agent,
    platform,
    disagree: agent !== null && agent.phase !== platform.phase,
  }
}

// ── the agent's phase history as a timeline ──────────────────────────────────

export interface TimelineEntry {
  state: AgentRunState
  at: string
  /** How long the run spent here; null while it is still there. */
  durationMs: number | null
}

export function phaseTimeline(run: AgentRun): TimelineEntry[] {
  const history = run.phase_history ?? []
  return history.map((entry, i) => {
    const next = history[i + 1]
    const closesAt = next ? next.at : run.finished_at
    return {
      state: entry.state,
      at: entry.at,
      durationMs: closesAt
        ? new Date(closesAt).getTime() - new Date(entry.at).getTime()
        : null,
    }
  })
}

// ── what the agent decided about one dead-letter row ─────────────────────────

export type DlqDecision = 'replay' | 'fence' | 'leave'

/**
 * Read off the agent's OWN audit rows — `agent.tool_invoked` only.
 *
 * `chaos.tool_invoked` rows are excluded deliberately: the evaluator seeds this
 * world, and attributing its seeding to the agent would badge every row the lab
 * planted as something the agent decided.
 *
 * Rows older than the newest boundary are excluded for a sharper version of the same
 * reason (WO-R3-327). The seeded dead-letter rows come back with *stable* ids, so a
 * replay from the previous take names the same job id as the row this take just
 * planted — and the badge would say the agent had already decided about a row it has
 * not seen.
 *
 * `leave` is the honest default. A row nothing touched has not been judged, and
 * for the `dlq_backlog` scenario leaving four of five rows alone is the correct
 * answer — so `leave` is a result, not a blank.
 */
export function dlqDecision(job: Job, audit: AuditLog[]): DlqDecision {
  const calls = sinceReset(audit, newestResetAt(audit))
    .filter(isAgentToolRow)
    .map(toolCall)
    .filter((c): c is ToolCall => c !== null)
    // Newest first: the last thing the agent decided about this row wins.
    .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0))

  for (const call of calls) {
    if (call.tool === 'mark_dlq_permanent' && call.args.job_id === job.id) {
      return 'fence'
    }
    if (call.tool === 'replay_dlq_by_ids' || call.tool === 'replay_dlq_messages') {
      const ids = call.args.job_ids
      if (Array.isArray(ids) && ids.includes(job.id)) return 'replay'
    }
    if (call.tool === 'replay_dlq_by_category') {
      // A by-category replay names a hint, not rows, so attribution is by the
      // row's own hint — narrowed by `job_type` when the call narrowed it.
      const sameCategory =
        job.remediation_hint != null && call.args.category === job.remediation_hint
      const typeOk = call.args.job_type == null || call.args.job_type === job.type
      if (sameCategory && typeOk) return 'replay'
    }
  }
  return 'leave'
}

// ── the metric thresholds each mode is about ─────────────────────────────────

export interface ModeMetric {
  /** What the sparkline and the recovery bar are measured against. */
  threshold: number
  label: string
  /** Where the number comes from, for the panel's own caption. */
  source: string
  /** Why this number, said in the UI so nobody has to guess. */
  rationale: string
}

/**
 * The bars the two demo modes recover against.
 *
 * Both come from the scenarios themselves rather than from a platform setting,
 * and both are stated here once so the sparkline's dashed line, the recovery
 * rule and DEMO.md cannot drift apart:
 *
 *  - `consumer_outage`: `remediate_consumer_lag_success` polls until
 *    `worker-dispatcher` lag is **≥ 20** before it lets the agent start, so
 *    below 20 is the world back inside its bar.
 *  - `dlq_backlog`: `remediate_dlq_backlog_success` seeds **5** dead letters of
 *    which exactly **one** is replay-safe, and grades on `replayed == 1`. So
 *    4 rows left is the fixed world; the other four are supposed to stay.
 */
export const MODE_METRICS: Record<DemoMode, ModeMetric> = {
  consumer_outage: {
    threshold: 20,
    label: 'worker-dispatcher lag',
    source: 'GET /api/v1/admin/consumer-lag',
    rationale: 'the scenario waits for lag ≥ 20 before the agent starts',
  },
  dlq_backlog: {
    threshold: 4,
    label: 'DLQ depth',
    source: 'GET /api/v1/admin/dlq/stats',
    rationale: 'the seeded backlog is 5 rows, one of them replay-safe',
  },
}

// ─────────────────────────────────────────────────────────────────────────────
// WO-R3-330 — two rows, never merged
//
// The strip above proved the wrong shape on camera. It had ONE row of stations
// with two markers, so the moment a run existed the agent's word was the only
// thing lighting a station and `fault injected` — which only the platform can
// assert — was never shown at all. The disagreement the design was for was
// invisible because both witnesses were competing for one row.
//
// So there are two rows now, always both, and each has only the stations its own
// witness is competent to assert:
//
//   PLATFORM  healthy → fault injected → agent acting → recovered
//   AGENT     triage → investigating → planning → awaiting approval
//             → remediating → verifying → resolved | escalated | failed
//
// Neither row is derived from the other, neither is hidden when it has nothing
// to say, and disagreement is therefore visible by construction rather than by a
// rule that has to notice it.
// ─────────────────────────────────────────────────────────────────────────────

export type StationState = 'passed' | 'current' | 'pending'

export interface Station<K extends string> {
  key: K
  label: string
  /** When this station was reached, by its own witness's clock. Null = not reached. */
  at: string | null
  /** How long it lasted. Null while it is the current station, or before it. */
  durationMs: number | null
  state: StationState
  /** One line of detail the station itself can carry. */
  note: string | null
  /** How many times this station was entered — 2 after a hand-back. */
  visits: number
}

export type PlatformStationKey =
  | 'healthy'
  | 'fault_injected'
  | 'agent_acting'
  | 'recovered'

export const PLATFORM_ROW: readonly PlatformStationKey[] = [
  'healthy',
  'fault_injected',
  'agent_acting',
  'recovered',
]

export const PLATFORM_STATION_LABELS: Record<PlatformStationKey, string> = {
  healthy: 'healthy',
  fault_injected: 'fault injected',
  agent_acting: 'agent acting',
  recovered: 'recovered',
}

export type PlatformStation = Station<PlatformStationKey>

export interface PlatformRowInput extends PlatformPhaseInput {
  /**
   * When the metric came back inside its bar, by the platform's own sample clock
   * (`metricRecovery`). Null means the page cannot put a time on it, which is a
   * different thing from the recovery not having happened — the station is still
   * reached when the reading says `recovered`, it just has no timestamp.
   */
  recoveredAt?: string | null
}

/** Oldest of a set of rows, where `newest` above takes the other end. */
function oldest(rows: AuditLog[]): AuditLog | null {
  let best: AuditLog | null = null
  for (const row of rows) {
    if (best === null || row.created_at < best.created_at) best = row
  }
  return best
}

function stationRow<K extends string>(
  cells: { key: K; label: string; at: string | null; reached: boolean; note?: string | null }[],
): Station<K>[] {
  const lastReached = cells.reduce((acc, cell, i) => (cell.reached ? i : acc), -1)
  return cells.map((cell, i) => {
    const nextAt = cells.slice(i + 1).find((c) => c.reached && c.at !== null)?.at ?? null
    return {
      key: cell.key,
      label: cell.label,
      at: cell.reached ? cell.at : null,
      durationMs:
        cell.reached && cell.at !== null && nextAt !== null
          ? new Date(nextAt).getTime() - new Date(cell.at).getTime()
          : null,
      state: i === lastReached ? 'current' : cell.reached ? 'passed' : 'pending',
      note: cell.note ?? null,
      visits: cell.reached ? 1 : 0,
    }
  })
}

/**
 * The platform's row: the four things the platform can say about the world
 * without taking the agent's word for any of it.
 *
 * `healthy` is stamped with the boundary, because that is when this world began
 * and it is the only start the page has that is not the browser's own idea of
 * when someone opened a tab.
 */
export function platformRow(input: PlatformRowInput): PlatformStation[] {
  const reading = platformPhase(input)
  const audit = sinceReset(input.audit, reading.resetAt)
  const faultAt = reading.faultAt

  const callsSinceFault =
    faultAt === null
      ? []
      : audit.filter((r) => isAgentToolRow(r) && r.created_at >= faultAt)
  const actions = callsSinceFault.filter((row) => {
    const call = toolCall(row)
    return call !== null && ACTION_TOOLS.includes(call.tool)
  })
  const actingAt = oldest(callsSinceFault)?.created_at ?? null
  const firstAction = oldest(actions)
  const reads = callsSinceFault.length - actions.length
  const note =
    firstAction !== null
      ? `${toolCall(firstAction)?.tool ?? 'a Tier-1 action'} fired after ${String(reads)} read${reads === 1 ? '' : 's'}`
      : callsSinceFault.length > 0
        ? `${String(reads)} read${reads === 1 ? '' : 's'}, no action yet`
        : null

  const recoveredReached = reading.phase === 'recovered'

  return stationRow<PlatformStationKey>([
    {
      key: 'healthy',
      label: PLATFORM_STATION_LABELS.healthy,
      at: reading.resetAt,
      reached: true,
      note: reading.resetAt === null ? 'no reset boundary in view' : 'since the reset',
    },
    {
      key: 'fault_injected',
      label: PLATFORM_STATION_LABELS.fault_injected,
      at: faultAt,
      reached: faultAt !== null,
      note: faultAt === null ? null : 'the lab’s own audit row',
    },
    {
      key: 'agent_acting',
      label: PLATFORM_STATION_LABELS.agent_acting,
      at: actingAt,
      reached: actingAt !== null,
      note,
    },
    {
      key: 'recovered',
      label: PLATFORM_STATION_LABELS.recovered,
      at: recoveredReached ? (input.recoveredAt ?? null) : null,
      reached: recoveredReached,
      note: reading.metricKnown ? null : 'the metric has no reading right now',
    },
  ])
}

export type AgentStationKey =
  | 'triage'
  | 'investigating'
  | 'planning'
  | 'awaiting_approval'
  | 'remediating'
  | 'verifying'
  | 'terminal'

export const AGENT_ROW: readonly AgentStationKey[] = [
  'triage',
  'investigating',
  'planning',
  'awaiting_approval',
  'remediating',
  'verifying',
  'terminal',
]

/** The nine states onto seven stations; the three terminals share the last one. */
const STATE_TO_STATION: Record<AgentRunState, AgentStationKey> = {
  triage: 'triage',
  investigating: 'investigating',
  planning: 'planning',
  awaiting_approval: 'awaiting_approval',
  remediating: 'remediating',
  verifying: 'verifying',
  resolved: 'terminal',
  escalated: 'terminal',
  failed: 'terminal',
}

const TERMINAL_STATES: readonly AgentRunState[] = ['resolved', 'escalated', 'failed']

export const AGENT_STATION_LABELS: Record<AgentStationKey, string> = {
  triage: 'triage',
  investigating: 'investigating',
  planning: 'planning',
  awaiting_approval: 'awaiting approval',
  remediating: 'remediating',
  verifying: 'verifying',
  terminal: 'resolved | escalated | failed',
}

export type AgentStation = Station<AgentStationKey>

/** The run's own word for where it is, verbatim — including one this build does not know. */
export function agentStateLabel(run: AgentRun | null): string {
  return run === null ? 'no run reported' : run.state
}

/**
 * The agent's row, from `phase_history` alone.
 *
 * A station can be entered twice — `verifying` may hand back to `investigating`
 * (commander ADR 0056) — so each one carries its visit count and the SUM of its
 * closed visits. Hiding a second visit would make a run that went round again
 * look like one that walked straight through, which is the opposite of what a
 * viewer needs to see.
 */
export function agentRow(run: AgentRun | null): AgentStation[] {
  const history = [...(run?.phase_history ?? [])].sort((a, b) =>
    a.at < b.at ? -1 : a.at > b.at ? 1 : 0,
  )
  const currentStation =
    run === null ? null : (STATE_TO_STATION[run.state] as AgentStationKey | undefined) ?? null

  const terminalEntry = history.find((e) => TERMINAL_STATES.includes(e.state))
  const terminalLabel =
    terminalEntry?.state ??
    (run !== null && TERMINAL_STATES.includes(run.state) ? run.state : null) ??
    AGENT_STATION_LABELS.terminal

  return AGENT_ROW.map((key) => {
    const visits = history
      .map((entry, i) => ({ entry, i }))
      .filter(({ entry }) => STATE_TO_STATION[entry.state] === key)
    const durations = visits.map(({ entry, i }) => {
      const closesAt = history[i + 1]?.at ?? run?.finished_at ?? null
      return closesAt === null
        ? null
        : new Date(closesAt).getTime() - new Date(entry.at).getTime()
    })
    const closed = durations.filter((d): d is number => d !== null)
    const reached = visits.length > 0 || currentStation === key
    const isCurrent = currentStation === key
    return {
      key,
      label: key === 'terminal' ? terminalLabel : AGENT_STATION_LABELS[key],
      at: visits[0]?.entry.at ?? null,
      // A station the run is still in has no duration: `ongoing` is the honest
      // reading, and the closed visits before it are what the sum is of.
      durationMs: closed.length > 0 ? closed.reduce((a, b) => a + b, 0) : null,
      state: isCurrent ? 'current' : reached ? 'passed' : 'pending',
      note: visits.length > 1 ? `entered ${String(visits.length)} times` : null,
      visits: visits.length,
    }
  })
}

// ── the metric, and when it really came back ─────────────────────────────────

/** One reading of the mode's metric, at the platform's own measurement time. */
export interface MetricSample {
  /** Epoch ms of `at`, so samples from two sources can be compared safely. */
  t: number
  v: number
  at: string
}

export interface RecoveryReading {
  /** The first sample outside the bar at or after the fault. */
  breachedAt: string | null
  /** The first sample of the run of inside-the-bar samples that counted. */
  recoveredAt: string | null
  /**
   * The start of an inside-the-bar run that is not long enough yet, so the page
   * can say "1 of 2 samples back inside" instead of either lying or saying nothing.
   */
  insideSince: string | null
  sustained: boolean
  /** How many consecutive inside samples a recovery takes. */
  required: number
}

/**
 * Whether the world really came back, read off the platform's own samples.
 *
 * The first take's rule was "the reading is inside the bar and a breach was seen",
 * evaluated on whatever single number the last poll returned — and the cached lag
 * value reads `42 → 0 → 42` as the sample ages, so the strip announced a recovery
 * in the middle of the incident and then took it back. Two things fix it, and both
 * are about which numbers are being read rather than about the threshold: the
 * samples come from the platform's 15-minute window (one per metrics tick, the
 * same reading the agent gets) rather than from the page's own polling, and a
 * recovery takes `required` consecutive samples inside the bar.
 */
export function metricRecovery(
  samples: MetricSample[],
  threshold: number,
  faultAt: string | null,
  required = 2,
): RecoveryReading {
  const faultT = faultAt === null ? null : new Date(faultAt).getTime()
  const relevant = [...samples]
    .filter((s) => faultT === null || s.t >= faultT)
    .sort((a, b) => a.t - b.t)

  const breach = relevant.find((s) => s.v > threshold) ?? null
  if (breach === null) {
    return {
      breachedAt: null,
      recoveredAt: null,
      insideSince: null,
      sustained: false,
      required,
    }
  }

  let runStart: string | null = null
  let runLength = 0
  let recoveredAt: string | null = null
  for (const sample of relevant.filter((s) => s.t >= breach.t)) {
    if (sample.v <= threshold) {
      runLength += 1
      if (runStart === null) runStart = sample.at
      if (runLength >= required) {
        recoveredAt = runStart
        break
      }
    } else {
      runLength = 0
      runStart = null
    }
  }

  return {
    breachedAt: breach.at,
    recoveredAt,
    insideSince: recoveredAt === null ? runStart : null,
    sustained: recoveredAt !== null,
    required,
  }
}

// ── what the chart draws on top of the line ──────────────────────────────────

export interface ChartMarker {
  at: string
  t: number
  kind: 'reset' | 'fault' | 'action' | 'recovery'
  label: string
  detail: string | null
}

export interface ChartMarkerInput {
  faultAt: string | null
  recoveredAt: string | null
  resetAt: string | null
  /** The selected run's steps; the authority on what the agent did, and when. */
  steps: AgentRunStepRecord[]
  /** Used for the actions only where no step was reported. */
  audit: AuditLog[]
  windowStart: number
  windowEnd: number
}

/**
 * The vertical markers: the boundary, the fault, every Tier-1 action, the recovery.
 *
 * Reads only — the bulk of any run — are deliberately not marked. Fifteen ticks
 * on a fifteen-minute chart is a comb, and the ledger is where the reads belong;
 * what the chart is for is the three or four moments that changed the line.
 */
export function chartMarkers(input: ChartMarkerInput): ChartMarker[] {
  const marks: ChartMarker[] = []
  const add = (at: string | null, kind: ChartMarker['kind'], label: string, detail: string | null = null) => {
    if (at === null) return
    const t = new Date(at).getTime()
    if (Number.isNaN(t) || t < input.windowStart || t > input.windowEnd) return
    marks.push({ at, t, kind, label, detail })
  }

  add(input.resetAt, 'reset', 'world reset', 'the take begins here')

  const labRow = newest(input.audit.filter(isLabRow))
  add(
    input.faultAt,
    'fault',
    toolCall(labRow ?? ({} as AuditLog))?.tool ?? 'fault injected',
    'the lab injected the fault',
  )

  const stepActions = input.steps.filter((s) => s.kind === 'action')
  if (stepActions.length > 0) {
    for (const step of stepActions) {
      // A step with no tool name is still a Tier-1 action the run reported; the
      // marker names it by its sequence rather than inventing a tool.
      add(step.at, 'action', step.tool ?? `step ${String(step.seq)}`, `step ${String(step.seq)}`)
    }
  } else {
    // No steps reported: the audit log still knows an action happened, it just
    // cannot say what came back. Marking it is still right.
    for (const row of input.audit.filter(isAgentToolRow)) {
      const call = toolCall(row)
      if (call !== null && ACTION_TOOLS.includes(call.tool)) {
        add(row.created_at, 'action', call.tool, 'from the audit log')
      }
    }
  }

  add(input.recoveredAt, 'recovery', 'recovered', 'the metric came back inside its bar')

  return marks.sort((a, b) => a.t - b.t)
}
