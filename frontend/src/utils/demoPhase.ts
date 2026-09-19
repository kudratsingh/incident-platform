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
 * Everything here is a pure function of its inputs so the whole strip can be
 * driven from a fixture through every transition.
 */

import type { AgentRun, AgentRunState, AuditLog, Job } from '../types'

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

/** The audit action prefix the lab writes under. Withheld from the agent's own MCP reads (ADR 0012); a human operator reads it over REST. */
export const LAB_ACTION_PREFIX = 'chaos.'
/** Every MCP call the agent makes, read or action alike. */
export const AGENT_TOOL_ACTION = 'agent.tool_invoked'
/** What the commander reports about its own run (ADR 0035). */
export const AGENT_RUN_REPORT_ACTION = 'agent.run_reported'

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

function newest(rows: AuditLog[]): AuditLog | null {
  let best: AuditLog | null = null
  for (const row of rows) {
    if (best === null || row.created_at > best.created_at) best = row
  }
  return best
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

// ── the platform's own record ─────────────────────────────────────────────────

export interface PlatformPhaseInput {
  /** The audit rows the page has, newest-first or not — order is derived. */
  audit: AuditLog[]
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
  metricKnown: boolean
}

/**
 * When the lab last injected a fault, by the platform's own clock.
 *
 * Exported because the page needs it BEFORE it can decide whether a breach has
 * been observed — the breach latch is per-fault, so a second take in one session
 * starts clean rather than inheriting the first take's recovery.
 */
export function newestFaultAt(audit: AuditLog[]): string | null {
  return newest(audit.filter(isLabRow))?.created_at ?? null
}

export function platformPhase(input: PlatformPhaseInput): PlatformPhaseReading {
  const { audit, metricKnown, metricInsideThreshold, metricBreachedSinceFault } = input
  const fault = newest(audit.filter(isLabRow))

  if (fault === null) {
    // No lab row: nothing was injected, whatever else is happening. A healthy
    // world with an agent poking at it is still a healthy world.
    return { phase: 'healthy', faultAt: null, metricKnown }
  }
  const faultAt = fault.created_at

  // Recovery is the one thing the platform can assert over the agent, and it
  // needs all three: a reading, a breach that really happened, and the reading
  // back inside the bar.
  if (metricKnown && metricBreachedSinceFault && metricInsideThreshold) {
    return { phase: 'recovered', faultAt, metricKnown }
  }

  const sinceFault = audit.filter((r) => isAgentToolRow(r) && r.created_at >= faultAt)
  const acted = sinceFault.some((row) => {
    const call = toolCall(row)
    return call !== null && ACTION_TOOLS.includes(call.tool)
  })
  if (acted) return { phase: 'agent_remediating', faultAt, metricKnown }
  if (sinceFault.length > 0) {
    return { phase: 'agent_investigating', faultAt, metricKnown }
  }
  return { phase: 'fault_injected', faultAt, metricKnown }
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
 * `leave` is the honest default. A row nothing touched has not been judged, and
 * for the `dlq_backlog` scenario leaving four of five rows alone is the correct
 * answer — so `leave` is a result, not a blank.
 */
export function dlqDecision(job: Job, audit: AuditLog[]): DlqDecision {
  const calls = audit
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
