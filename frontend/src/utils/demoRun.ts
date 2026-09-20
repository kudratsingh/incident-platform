/**
 * What the /demo page reads off one run record (WO-R3-330, against WO-R3-328).
 *
 * `demoPhase.ts` next door answers "where is this incident" from the two
 * witnesses. This file answers the other half, which the first live take could
 * not answer at all: **what the agent thought, decided, saw and spent.**
 *
 * That take's console showed an empty agent panel for the whole run, and it was
 * not a rendering bug. `report_agent_run` carried one `current_hypothesis` and one
 * `last_step`, both filled only at the end, so there was nothing on the wire to
 * draw. And the timeline beside it was 43 job events out of 50 rows, with the
 * agent's three rows lost in them and no tool RESULT on any of them, because
 * `agent.tool_invoked` records that a call happened and with what arguments and
 * never what came back.
 *
 * WO-R3-328 puts all of it on the record — a ranked hypothesis list with reasoning
 * excerpts, the plan, every verify verdict, the budget, and an append-only `steps`
 * list with a result excerpt per call. Everything here derives a panel from those,
 * under two rules:
 *
 *  1. **An absence is named, never filled.** A run with no `hypotheses` is not a
 *     run that ranked nothing: `hypothesesSource` tells the two apart so the panel
 *     can say which it is looking at. A stack older than v0.6.16 and a commander
 *     older than WO-R3-329 are different absences, and both are possible on the
 *     day this ships.
 *  2. **`steps` is the authority where it exists.** The audit log's
 *     `agent.tool_invoked` rows are the same calls minus their results, so the
 *     ledger uses them only as a fallback — and `ledgerCounts` reports both
 *     witnesses' totals, because a reporter that stopped reporting mid-run is
 *     exactly what a demo must not hide.
 */

import type {
  AgentRun,
  AgentRunBudget,
  AgentRunRankedHypothesis,
  AgentRunStepRecord,
  AgentRunVerification,
  AuditLog,
  Job,
} from '../types'
import {
  ACTION_TOOLS,
  AGENT_RUN_REPORT_ACTION,
  AGENT_TOOL_ACTION,
  LAB_ACTION_PREFIX,
  isResetRow,
  toolCall,
} from './demoPhase'
import type { DlqDecision } from './demoPhase'

/** The prefix the job lifecycle audits under — the 43 rows in 50 (WO-R3-328). */
export const JOB_EVENT_PREFIX = 'event.'

// ── the hypotheses ───────────────────────────────────────────────────────────

/** Where the hypothesis panel's content came from, which decides what it says. */
export type HypothesesSource = 'ranked' | 'current_only' | 'none'

export function hypothesesSource(run: AgentRun | null): HypothesesSource {
  if (run === null) return 'none'
  if ((run.hypotheses ?? []).length > 0) return 'ranked'
  return run.current_hypothesis !== null ? 'current_only' : 'none'
}

/**
 * The ranked list, highest confidence first.
 *
 * Sorted here rather than trusted: the platform stores what the reporter sent, and
 * a panel whose bars are not in order reads as a bug even when the data is right.
 * Where only `current_hypothesis` exists it becomes a one-entry list with a null
 * excerpt — the top hypothesis IS the list's head, so the panel shape does not
 * have to change for a commander that sends one.
 */
export function rankedHypotheses(run: AgentRun | null): AgentRunRankedHypothesis[] {
  if (run === null) return []
  const ranked = run.hypotheses ?? []
  if (ranked.length > 0) {
    return [...ranked].sort((a, b) => b.confidence - a.confidence)
  }
  const top = run.current_hypothesis
  if (top === null) return []
  return [
    {
      name: top.name,
      category: top.category,
      confidence: top.confidence,
      reasoning_excerpt: null,
    },
  ]
}

// ── the verify verdicts ──────────────────────────────────────────────────────

/**
 * Every verify poll, oldest first — the list when the record has one, the latest
 * verdict alone when it does not.
 *
 * The list matters more than the latest: `not_verified` then `verified` is a run
 * that had to try twice, and only the latest field survives on a record written
 * before WO-R3-328.
 */
export function runVerifications(run: AgentRun | null): AgentRunVerification[] {
  if (run === null) return []
  const all = run.verifications ?? []
  if (all.length > 0) return all
  return run.verification ? [run.verification] : []
}

// ── the budget ───────────────────────────────────────────────────────────────

export interface BudgetMeter {
  used: number | null
  max: number | null
  /** Null without a cap: a bar with no cap is a bar with an invented denominator. */
  percent: number | null
  /** True when the run used more than its cap — the bar is full and the number is not. */
  over: boolean
  tokens: number | null
  usd: number | null
  wallSeconds: number | null
}

export function budgetMeter(
  budget: AgentRunBudget | null | undefined,
): BudgetMeter {
  const used = budget?.tool_calls_used ?? null
  const max = budget?.tool_calls_max ?? null
  const ratio = used !== null && max !== null && max > 0 ? (used / max) * 100 : null
  return {
    used,
    max,
    percent: ratio === null ? null : Math.min(100, ratio),
    over: used !== null && max !== null && used > max,
    tokens: budget?.tokens_used ?? null,
    usd: budget?.usd_used ?? null,
    wallSeconds: budget?.wall_seconds ?? null,
  }
}

// ── the steps ────────────────────────────────────────────────────────────────

/**
 * One step per `seq`, in order, from however many answers carried it.
 *
 * The page reads the steps from two places — the run detail, which carries the
 * whole list, and `/steps?after_seq=`, which carries the tail — because the list
 * is append-only and monotonic in `seq`, so re-downloading two hundred rows with a
 * four-hundred-character excerpt each every two seconds is waste. A later answer
 * wins for the same `seq`: the platform appends, and the only way one step changes
 * is a correction, which is the newer of the two.
 */
export function mergeSteps(
  existing: AgentRunStepRecord[],
  incoming: AgentRunStepRecord[],
): AgentRunStepRecord[] {
  const bySeq = new Map<number, AgentRunStepRecord>()
  for (const step of existing) bySeq.set(step.seq, step)
  for (const step of incoming) bySeq.set(step.seq, step)
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
}

// ── the ledger ───────────────────────────────────────────────────────────────

export type LedgerKind =
  | 'step'
  | 'agent_audit'
  | 'agent_report'
  | 'lab'
  | 'reset'
  | 'job_event'
  | 'human'

export interface LedgerEntry {
  id: string
  at: string
  kind: LedgerKind
  /** Set on a `step` entry. */
  step?: AgentRunStepRecord
  /** Set on every entry derived from an audit row. */
  row?: AuditLog
}

export interface LedgerInput {
  /** The selected run's steps. */
  steps: AgentRunStepRecord[]
  /** The audit rows the page holds — the operator streams, unfiltered by chip. */
  audit: AuditLog[]
  /**
   * Off by default, and this is the setting that made the first take unwatchable:
   * with the traffic loop running, `event.job.*` was 43 of the 50 rows on screen
   * and the four rows the demo is about were somewhere under them.
   */
  showJobEvents?: boolean
}

/**
 * The run's own actions, newest first, with the lab's rows and the boundary in
 * their true places in time.
 *
 * One entry per step where steps exist. The agent's `agent.tool_invoked` and
 * `agent.run_reported` audit rows are then dropped, because they are the same
 * events without their results and drawing both would double every row — the
 * fallback below is the only case that needs them.
 *
 * Every reset row in the window is an entry, not just the newest: two takes'
 * worth of rows in one window is a real state on a demo stack, and a divider per
 * boundary says which rows belong to which take instead of silently mixing them.
 */
export function buildLedger(input: LedgerInput): LedgerEntry[] {
  const haveSteps = input.steps.length > 0
  const entries: LedgerEntry[] = input.steps.map((step) => ({
    id: `step-${String(step.seq)}`,
    at: step.at,
    kind: 'step' as const,
    step,
  }))

  for (const row of input.audit) {
    const kind = auditLedgerKind(row, haveSteps, input.showJobEvents === true)
    if (kind === null) continue
    entries.push({ id: `row-${row.id}`, at: row.created_at, kind, row })
  }

  return entries.sort((a, b) => {
    if (a.at !== b.at) return a.at < b.at ? 1 : -1
    // Same instant: the step order is the run's own order, and a step is more
    // specific than the row that recorded it.
    return (b.step?.seq ?? 0) - (a.step?.seq ?? 0)
  })
}

function auditLedgerKind(
  row: AuditLog,
  haveSteps: boolean,
  showJobEvents: boolean,
): LedgerKind | null {
  if (isResetRow(row)) return 'reset'
  // Matched on the ACTION, not the principal: the evaluator is a service account
  // too, so a principal-only test files the lab's own rows under the agent and
  // makes the fault look like something the agent did.
  if (row.action.startsWith(LAB_ACTION_PREFIX)) return 'lab'
  if (row.action.startsWith(JOB_EVENT_PREFIX)) return showJobEvents ? 'job_event' : null
  if (row.action === AGENT_RUN_REPORT_ACTION) return haveSteps ? null : 'agent_report'
  if (row.action === AGENT_TOOL_ACTION) return haveSteps ? null : 'agent_audit'
  if (row.principal_type === 'user') return 'human'
  return null
}

export interface LedgerCounts {
  /** Every step the responder reported, reports included. */
  steps: number
  /** The steps that were MCP calls — a `read` or an `action`. */
  calls: number
  /** The `agent.tool_invoked` rows the platform recorded. */
  auditCalls: number
  labRows: number
  /** False when the two witnesses do not agree on how many calls there were. */
  agreed: boolean
}

/**
 * What each witness counted.
 *
 * The reporter is fail-open by design (commander invariant 5), so it can stop
 * reporting without the run noticing — and then the ledger quietly stops growing
 * while the agent keeps working. The platform's own `agent.tool_invoked` rows are
 * the second count, and showing both is the only way that failure is visible
 * while it is happening rather than afterwards in a trace.
 */
export function ledgerCounts(input: {
  steps: AgentRunStepRecord[]
  audit: AuditLog[]
}): LedgerCounts {
  const auditCalls = input.audit.filter((r) => r.action === AGENT_TOOL_ACTION).length
  // Report steps have no `agent.tool_invoked` row — they audit as
  // `agent.run_reported` — so they are not part of the comparison.
  const calls = input.steps.filter((s) => s.kind !== 'report').length
  return {
    steps: input.steps.length,
    calls,
    auditCalls,
    labRows: input.audit.filter((r) => r.action.startsWith(LAB_ACTION_PREFIX)).length,
    agreed: calls === auditCalls,
  }
}

// ── what the run decided about one dead-letter row ───────────────────────────

/**
 * The DLQ badge, read off the run's own steps rather than off the audit log.
 *
 * Same rules as `dlqDecision` next door and a better source: a step carries the
 * arguments AND the outcome of the call, it is scoped to the run the page is
 * showing, and `seq` orders two decisions about one row without comparing
 * timestamps from different clocks. Reads are ignored — looking at a row is not
 * deciding about it — and `leave` is a result rather than a blank, because
 * replaying one row of five and leaving four is the correct answer to the
 * `dlq_backlog` scenario.
 */
export function dlqDecisionFromSteps(
  job: Job,
  steps: AgentRunStepRecord[],
): DlqDecision {
  const actions = steps
    .filter((s) => s.kind === 'action' && ACTION_TOOLS.includes(s.tool))
    // Newest first: the last thing the run decided about this row wins.
    .sort((a, b) => b.seq - a.seq)

  for (const step of actions) {
    const args = step.arguments ?? {}
    if (step.tool === 'mark_dlq_permanent' && args.job_id === job.id) return 'fence'
    if (step.tool === 'replay_dlq_by_ids' || step.tool === 'replay_dlq_messages') {
      const ids = args.job_ids
      if (Array.isArray(ids) && ids.includes(job.id)) return 'replay'
    }
    if (step.tool === 'replay_dlq_by_category') {
      const sameCategory =
        job.remediation_hint != null && args.category === job.remediation_hint
      const typeOk = args.job_type == null || args.job_type === job.type
      if (sameCategory && typeOk) return 'replay'
    }
  }
  return 'leave'
}

/** True where the step list is the badge's source, rather than the audit log. */
export function dlqDecisionSource(steps: AgentRunStepRecord[]): 'steps' | 'audit' {
  return steps.length > 0 ? 'steps' : 'audit'
}

/** The lab's own tool name from a `chaos.*` row, for a caption that names it. */
export function labToolName(row: AuditLog): string {
  return toolCall(row)?.tool ?? row.action
}
