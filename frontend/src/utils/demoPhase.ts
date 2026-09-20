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
 * **A take is the span between two boundaries** (WO-R3-334). The rule above was
 * "everything after the newest boundary", and the third live take proved that is not
 * the same thing: the page was reloaded after the wind-down, so the newest boundary
 * was AFTER the run, and the platform row read a freshly wiped world beside an agent
 * row that read `escalated`. The page now picks the newest run with a fault in its
 * own take and reads that take end to end — and measures every `agent.tool_invoked`
 * row against that run's own `service_account_id`, because the demo runner and the
 * evaluator's guard probes both make calls under the agent's token.
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
 * The action a read the LAB took under the agent's own token is written as
 * (platform WO-R3-333).
 *
 * The evaluator's principal-guard probes and its world audit deliberately wear the
 * agent's token — proving what that token can and cannot do is the point of them —
 * so they cannot be told apart by principal. They are labelled at the source
 * instead, and this page drops them: in the third take seven of them landed AFTER
 * the reset boundary and the page read them as a new run's work.
 *
 * Matched as "a `lab.` row that is not the boundary" rather than by the exact name,
 * so a second lab label cannot arrive and be read as the agent's doing.
 */
export const LAB_PROBE_ACTION = 'lab.probe'

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

/** A read the lab took while wearing the agent's token (WO-R3-333). */
export function isLabProbeRow(row: AuditLog): boolean {
  return row.action.startsWith('lab.') && !isResetRow(row)
}

/**
 * An `agent.tool_invoked` row this run actually made.
 *
 * `principalId` is the run's own `service_account_id`. The third take's ledger was
 * flooded with `get_consumer_lag` every three seconds because the demo runner built
 * two of its clients with the AGENT's token, and the page could not tell those reads
 * from the agent's (F3). With a run selected there is a principal to compare
 * against, so it does: a row by anyone else is somebody else's read. Without a run
 * there is nothing to compare against and every row counts, which is the honest
 * reading rather than a guess.
 */
function isRunToolRow(row: AuditLog, principalId: string | null | undefined): boolean {
  if (!isAgentToolRow(row)) return false
  if (principalId === null || principalId === undefined) return true
  return row.principal_id === principalId
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
  return rowsInTake(audit, { startAt: resetAt, endAt: null })
}

// ── a take is the span between two boundaries ────────────────────────────────
//
// WO-R3-334. The third take was recorded, wound down, and the page was reloaded
// two minutes later — so the newest boundary was AFTER the run, and everything
// that read "since the newest boundary" read an empty world while the agent panel
// read `escalated`. The header said "no run in this take yet" beside a run that
// had happened, and the PLATFORM row said "healthy, no fault yet" beside an AGENT
// row that said the run had given up.
//
// "Newest boundary onwards" was the wrong unit. A take is the span BETWEEN two
// boundaries, a run belongs to exactly one of them, and the page reads that run's
// take — the boundary before it and the boundary after it — for everything: the
// platform row, the clock, the chart's markers, the ledger and the DLQ badges.

export interface Take {
  /**
   * The boundary that opened this take, or null when none is in view.
   *
   * Null is "no boundary in what I can see", which is also what a stack older than
   * WO-R3-327 looks like — the same honest fallback the boundary rule has always
   * had. It is never inferred.
   */
  startAt: string | null
  /** The boundary that closed it, or null while it is the take now running. */
  endAt: string | null
}

/** Every boundary in view, oldest first, de-duplicated. */
export function takeBoundaries(audit: AuditLog[]): string[] {
  const seen = new Set(audit.filter(isResetRow).map((r) => r.created_at))
  return [...seen].sort()
}

/** The take now running: after the newest boundary, with no end yet. */
export function currentTake(audit: AuditLog[]): Take {
  const bounds = takeBoundaries(audit)
  return { startAt: bounds[bounds.length - 1] ?? null, endAt: null }
}

/**
 * The take an instant falls in.
 *
 * A row or a run sharing a boundary's exact timestamp belongs to the take being
 * CLOSED — the reset writes its row last, after every restoring step, so anything
 * simultaneous with it is the world it just wound up. Hence `b < at` for the start
 * and `b >= at` for the end.
 */
export function takeAt(audit: AuditLog[], at: string | null): Take {
  if (at === null) return currentTake(audit)
  const bounds = takeBoundaries(audit)
  const startAt = [...bounds].reverse().find((b) => b < at) ?? null
  const endAt = bounds.find((b) => b >= at) ?? null
  return { startAt, endAt }
}

/** The take a run belongs to, by the run's own start. */
export function takeOfRun(audit: AuditLog[], run: AgentRun | null): Take {
  return takeAt(audit, run?.started_at ?? null)
}

/** One string per take, so a latch or a memo can be keyed on it. */
export function takeKey(take: Take): string {
  return `${take.startAt ?? 'open'}→${take.endAt ?? 'now'}`
}

/** True once a boundary has closed this take. */
export function takeHasEnded(take: Take): boolean {
  return take.endAt !== null
}

/** The rows of one take: after its opening boundary, up to and including its closing one. */
export function rowsInTake(audit: AuditLog[], take: Take): AuditLog[] {
  return audit.filter(
    (row) =>
      (take.startAt === null || row.created_at > take.startAt) &&
      (take.endAt === null || row.created_at <= take.endAt),
  )
}

/**
 * The take's rows plus the boundary row that OPENED it.
 *
 * That row belongs to the take it closed — the reset writes it last, after every
 * restoring step — but the ledger has to be able to draw both edges of the take it is
 * showing, so it asks for them explicitly. Nothing that counts calls uses this: a
 * boundary is not a call.
 */
export function rowsInTakeWithEdges(audit: AuditLog[], take: Take): AuditLog[] {
  const startAt = take.startAt
  const opening =
    startAt === null ? [] : audit.filter((r) => isResetRow(r) && r.created_at === startAt)
  return [...opening, ...rowsInTake(audit, take)]
}

/** When the lab injected this take's fault, by the platform's own clock. */
export function faultInTake(audit: AuditLog[], take: Take): string | null {
  return newest(rowsInTake(audit, take).filter(isLabRow))?.created_at ?? null
}

/** The runs that started inside one take, newest first. */
export function runsInTake(runs: AgentRun[], take: Take): AgentRun[] {
  return runs
    .filter(
      (r) =>
        (take.startAt === null || r.started_at > take.startAt) &&
        (take.endAt === null || r.started_at <= take.endAt),
    )
    .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
}

/** Why the page is showing the take it is showing. */
export type TakeChoice =
  /** `?run=` named a run the page has. */
  | 'requested'
  /** The newest run whose own take carries a fault row — the default rule. */
  | 'fault'
  /** No run's take has a fault row in view, so the newest run's take. */
  | 'newest_run'
  /** No run at all, so the take now running. */
  | 'current'

export interface TakeSelection {
  take: Take
  run: AgentRun | null
  /** Every run in view, newest first — what the selector offers. */
  runs: AgentRun[]
  /** The runs of the selected take, newest first. */
  takeRuns: AgentRun[]
  why: TakeChoice
}

/**
 * Which run, and therefore which take, the page reads.
 *
 * The default is **the newest run with a fault row in its own take**, not the newest
 * run and not the newest boundary. That is the rule the third take needed: after the
 * wind-down the newest boundary held an empty world, and the only coherent thing on
 * the screen was the take that had just happened.
 *
 * `?run=` wins over the default and selects that run's take the same way, so a deep
 * link from `make demo-live` opens the take it belongs to however many resets have
 * happened since. A `?run=` naming a run the page does not have falls through to the
 * default rather than emptying the screen.
 */
export function selectTake(input: {
  runs: AgentRun[]
  audit: AuditLog[]
  wanted?: string | null
}): TakeSelection {
  const runs = [...input.runs].sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
  const choose = (run: AgentRun | null, why: TakeChoice, take?: Take): TakeSelection => {
    const chosen = take ?? takeOfRun(input.audit, run)
    return { take: chosen, run, runs, takeRuns: runsInTake(runs, chosen), why }
  }

  const wanted = input.wanted ?? ''
  if (wanted !== '') {
    const found = runs.find((r) => r.id === wanted)
    if (found) return choose(found, 'requested')
  }

  const withFault = runs.find(
    (r) => faultInTake(input.audit, takeOfRun(input.audit, r)) !== null,
  )
  if (withFault) return choose(withFault, 'fault')
  if (runs.length > 0) return choose(runs[0], 'newest_run')
  return choose(null, 'current', currentTake(input.audit))
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
   * The take this reading is about (WO-R3-334).
   *
   * Absent means "the newest boundary onwards", which is what every caller meant
   * before the third take proved it insufficient: the page was reloaded after the
   * wind-down, so the newest boundary was AFTER the run and this row read an empty
   * world beside an agent row that said `escalated`.
   */
  take?: Take | null
  /**
   * The run's own `service_account_id`, so a read somebody else took under the
   * agent's token is not counted as the agent acting (F3/F4). Null = count them all,
   * which is the only honest reading when no run is selected.
   */
  runPrincipalId?: string | null
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
 * The fault of the take being read: the latch and the rows, whichever is newer,
 * and neither one if it falls outside the take.
 *
 * Both ends matter since WO-R3-334. A latch from the previous take was always
 * dropped; a latch from a LATER one has to be dropped too, or a page showing a
 * closed take would count its clock from the next take's kill.
 */
function latchedFaultAt(
  latched: string | null | undefined,
  derived: string | null,
  take: Take,
): string | null {
  const candidates = [latched ?? null, derived].filter(
    (v): v is string =>
      v !== null &&
      (take.startAt === null || v > take.startAt) &&
      (take.endAt === null || v <= take.endAt),
  )
  if (candidates.length === 0) return null
  return candidates.reduce((a, b) => (a > b ? a : b))
}

export function platformPhase(input: PlatformPhaseInput): PlatformPhaseReading {
  const { metricKnown, metricInsideThreshold, metricBreachedSinceFault } = input
  // Everything below reads ONE take. A boundary discards the previous take's fault,
  // its investigation AND its remediation together — crediting one take's
  // `restart_consumer_group` to the next one's fault would be the same lie in a
  // different station.
  const take = input.take ?? currentTake(input.audit)
  const resetAt = take.startAt
  const audit = rowsInTake(input.audit, take)
  const faultAt = latchedFaultAt(
    input.faultAt,
    newest(audit.filter(isLabRow))?.created_at ?? null,
    take,
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

  const sinceFault = audit.filter(
    (r) => isRunToolRow(r, input.runPrincipalId) && r.created_at >= faultAt,
  )
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
  /**
   * When the report that carried this station ARRIVED, set only when it arrived
   * more than `lateAfterMs` after the event itself (WO-R3-334, F2).
   *
   * The third take's three agent stations were all stamped 08:17:59 with durations
   * of 76 ms / 9 ms / 0 ms, and the whole burst reached the platform at 08:18:40
   * carrying its original timestamps — so a run that took 41 seconds rendered as
   * one that took 85 milliseconds. The event's time is the truth about the run and
   * stays the station's stamp; this is the truth about the reporting, and a late
   * burst has to read as late rather than as instantaneous.
   */
  reportedAt: string | null
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
      // The platform's own stations are read off rows the platform wrote itself, so
      // there is no reporting delay to show: the row IS the arrival.
      reportedAt: null,
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
  const audit = rowsInTake(input.audit, input.take ?? currentTake(input.audit))
  const faultAt = reading.faultAt

  const callsSinceFault =
    faultAt === null
      ? []
      : audit.filter(
          (r) => isRunToolRow(r, input.runPrincipalId) && r.created_at >= faultAt,
        )
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

/** How late a report has to be before the station says when it actually arrived. */
export const LATE_REPORT_MS = 5000

export interface AgentRowOptions {
  /**
   * When each reported state ARRIVED at the platform, from `reportArrivals`.
   *
   * The run's own `phase_history` timestamps are the responder's; the arrival is the
   * platform's. The two were 41 seconds apart in the third take and the page had no
   * way to say so (F2).
   */
  arrivals?: Map<string, string> | null
  /**
   * How many reached stations to show, for the sequential reveal — absent shows all.
   *
   * A queued burst of reports arrives in one poll, and four stations lighting in one
   * frame is not a run advancing, it is a page catching up. Revealing them one at a
   * time is the difference between the two, on camera.
   */
  reveal?: number | null
  /** Overridable for tests; the default is `LATE_REPORT_MS`. */
  lateAfterMs?: number
}

/**
 * When each state the run reported reached the platform.
 *
 * Every report writes an `agent.run_reported` audit row whose `extra_data.arguments`
 * carries the run id and the state (ADR 0035), and whose `created_at` is the
 * platform's own clock — so the audit log is the record of *when the page could have
 * known*. The earliest row per state is the arrival: a state reported again later is
 * the same station, not a second one.
 */
export function reportArrivals(
  audit: AuditLog[],
  runId: string | null,
): Map<string, string> {
  const arrivals = new Map<string, string>()
  if (runId === null) return arrivals
  for (const row of audit) {
    if (row.action !== AGENT_RUN_REPORT_ACTION) continue
    const args = row.extra_data?.arguments
    if (!args || typeof args !== 'object') continue
    const fields = args as Record<string, unknown>
    if (fields.run_id !== runId) continue
    const state = fields.state
    if (typeof state !== 'string') continue
    const seen = arrivals.get(state)
    if (seen === undefined || row.created_at < seen) arrivals.set(state, row.created_at)
  }
  return arrivals
}

/**
 * The same row with only its first `reveal` reached stations shown.
 *
 * The station that is last of the revealed ones becomes the current one, so a
 * partially revealed row reads as a run that has got that far — never as one that
 * skipped a station. Held back stations are pending with no timestamp, because a
 * station showing a time while reading `pending` is a contradiction on screen.
 */
export function revealStations<K extends string>(
  stations: Station<K>[],
  reveal: number | null | undefined,
): Station<K>[] {
  if (reveal === null || reveal === undefined) return stations
  const reached = stations.flatMap((s, i) => (s.state === 'pending' ? [] : [i]))
  if (reveal >= reached.length) return stations
  const shown = reached.slice(0, Math.max(0, reveal))
  const last = shown[shown.length - 1] ?? -1
  const keep = new Set(shown)
  return stations.map((station, i) => {
    if (!keep.has(i)) {
      return { ...station, state: 'pending', at: null, durationMs: null, reportedAt: null }
    }
    return i === last
      ? { ...station, state: 'current', durationMs: null }
      : { ...station, state: 'passed' }
  })
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
export function agentRow(
  run: AgentRun | null,
  options: AgentRowOptions = {},
): AgentStation[] {
  const arrivals = options.arrivals ?? null
  const lateAfterMs = options.lateAfterMs ?? LATE_REPORT_MS
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

  const stations = AGENT_ROW.map((key) => {
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
    const first = visits[0]?.entry ?? null
    // The arrival of the report that carried this station, kept only when it is
    // late enough to change what the row means.
    const arrival = first === null ? null : (arrivals?.get(first.state) ?? null)
    const lateBy =
      first === null || arrival === null
        ? null
        : new Date(arrival).getTime() - new Date(first.at).getTime()
    return {
      key,
      label: key === 'terminal' ? terminalLabel : AGENT_STATION_LABELS[key],
      at: first?.at ?? null,
      // A station the run is still in has no duration: `ongoing` is the honest
      // reading, and the closed visits before it are what the sum is of.
      durationMs: closed.length > 0 ? closed.reduce((a, b) => a + b, 0) : null,
      state: isCurrent ? 'current' : reached ? 'passed' : 'pending',
      note: visits.length > 1 ? `entered ${String(visits.length)} times` : null,
      visits: visits.length,
      reportedAt: lateBy !== null && lateBy > lateAfterMs ? arrival : null,
    } satisfies AgentStation
  })
  return revealStations(stations, options.reveal)
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
  /** One boundary, kept for callers that read a single take's opening. */
  resetAt?: string | null
  /** Every boundary to draw — the take's own start and end (WO-R3-334). */
  resetAts?: (string | null)[]
  /** The selected run's steps; the authority on what the agent did, and when. */
  steps: AgentRunStepRecord[]
  /** Used for the actions only where no step was reported. */
  audit: AuditLog[]
  /**
   * The run's own `service_account_id`. Without it the third take drew three blue
   * `A` markers for `mark_dlq_permanent` and friends that the EVALUATOR's guard
   * probes fired under the agent's token (F4) — the page was marking somebody
   * else's writes as the agent acting.
   */
  runPrincipalId?: string | null
  windowStart: number
  windowEnd: number
}

/**
 * The vertical markers, and the set is closed: the boundaries, the lab's fault,
 * the run's own Tier-1 actions, the recovery. Nothing else.
 *
 * Reads are not marked — fifteen ticks on a fifteen-minute chart is a comb, and the
 * ledger is where every call belongs. Neither are the runner's or the evaluator's
 * calls: a marker here says "the agent did this", so a row that is not the run's own
 * principal, and every `lab.probe` row, is not a candidate (F3/F4).
 */
export function chartMarkers(input: ChartMarkerInput): ChartMarker[] {
  const marks: ChartMarker[] = []
  const add = (at: string | null, kind: ChartMarker['kind'], label: string, detail: string | null = null) => {
    if (at === null) return
    const t = new Date(at).getTime()
    if (Number.isNaN(t) || t < input.windowStart || t > input.windowEnd) return
    marks.push({ at, t, kind, label, detail })
  }

  const boundaries = input.resetAts ?? [input.resetAt ?? null]
  for (const at of boundaries) {
    add(at, 'reset', 'world reset', 'a take begins and ends here')
  }

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
    // cannot say what came back. Marking it is still right — as long as it was
    // this run that made the call.
    for (const row of input.audit.filter((r) => isRunToolRow(r, input.runPrincipalId))) {
      const call = toolCall(row)
      if (call !== null && ACTION_TOOLS.includes(call.tool)) {
        add(row.created_at, 'action', call.tool, 'from the audit log')
      }
    }
  }

  add(input.recoveredAt, 'recovery', 'recovered', 'the metric came back inside its bar')

  return marks.sort((a, b) => a.t - b.t)
}

// ── the chart's own axes ─────────────────────────────────────────────────────

/** The span the chart draws, and what its left edge should be labelled. */
export interface ChartWindow {
  start: number
  end: number
  /** True while the span is the take's rather than the platform's whole history. */
  zoomed: boolean
}

/** Two minutes of quiet before the fault, so the climb starts from a flat line. */
export const PRE_FAULT_MS = 2 * 60 * 1000
/** No span narrower than this, or a take one tick old draws a chart of one point. */
export const MIN_SPAN_MS = 5 * 60 * 1000

/**
 * The x-axis span: the take, not a fixed fifteen minutes (WO-R3-334).
 *
 * A fifteen-minute axis put the third take's whole incident — a climb of 0 → 10 → 30
 * over two minutes — into the last eighth of the plot, drawn from two samples. The
 * span is the take now: two minutes before the fault to now, or to the boundary that
 * closed it. `full` zooms back out to everything the platform still holds, which is
 * the answer to "what did the rest of the window look like".
 *
 * Both clamps matter. Never narrower than `MIN_SPAN_MS`, so a take that started ten
 * seconds ago is not a chart of one pixel; never wider than the platform's own
 * history, because the samples beyond it do not exist and an empty stretch of axis
 * reads as a flat line.
 */
export function chartWindow(input: {
  faultAt: string | null
  takeStartAt: string | null
  takeEndAt: string | null
  now: number
  windowSeconds: number
  full?: boolean
}): ChartWindow {
  const historyMs = Math.max(MIN_SPAN_MS, input.windowSeconds * 1000)
  const endAt = input.takeEndAt === null ? input.now : new Date(input.takeEndAt).getTime()
  const end = Number.isNaN(endAt) ? input.now : endAt
  if (input.full === true) return { start: end - historyMs, end, zoomed: false }

  const faultT = input.faultAt === null ? null : new Date(input.faultAt).getTime()
  const takeT = input.takeStartAt === null ? null : new Date(input.takeStartAt).getTime()
  const wanted =
    faultT !== null && !Number.isNaN(faultT)
      ? faultT - PRE_FAULT_MS
      : takeT !== null && !Number.isNaN(takeT)
        ? takeT - 30_000
        : end - historyMs
  const start = Math.max(end - historyMs, Math.min(wanted, end - MIN_SPAN_MS))
  return { start, end, zoomed: start > end - historyMs }
}

/**
 * A clean upper bound just above the data.
 *
 * The ladder is finer than 1/2/5 on purpose: a peak of 53 on a 1/2/5 ladder becomes
 * an axis to 100, which pushes the whole incident into the bottom half of the plot
 * and makes a lag of 42 look like nothing.
 */
export function niceMax(value: number): number {
  if (value <= 1) return 1
  const exp = Math.floor(Math.log10(value))
  const base = Math.pow(10, exp)
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (value <= step * base) return step * base
  }
  return 10 * base
}

/**
 * The y ticks: four gaps, round numbers, and zero always drawn.
 *
 * Zero is the line a viewer measures a recovery against, so it is a tick rather than
 * wherever the axis happens to end; a fractional tick (7.5 messages behind) is
 * rounded away rather than printed, because the metric is a count.
 */
export function yAxisTicks(max: number): number[] {
  const top = niceMax(max)
  const raw = [0, top / 4, top / 2, (top * 3) / 4, top]
  const ticks = raw.map((t) => (top >= 4 ? Math.round(t) : t))
  return [...new Set(ticks)].sort((a, b) => a - b)
}
