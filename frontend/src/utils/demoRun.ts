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
 *  3. **A call that is not this run's is not this run's** (WO-R3-334). The third
 *     live take's ledger counted 89 calls against a four-call run: the demo runner
 *     read lag every three seconds under the AGENT's token and the evaluator's
 *     guard probes fired seven more. Rows are matched against the run's own
 *     `service_account_id`, `lab.probe` rows are the lab's own (WO-R3-333), and
 *     what is excluded is counted and shown behind a toggle rather than dropped.
 *     The rows themselves are one line each, with the answer summarised on them.
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
  isLabProbeRow,
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
 * The ranked list, **in the order the responder sent it**.
 *
 * Not re-sorted by confidence, deliberately: the platform stores the list best
 * first and states that the order IS the ranking (WO-R3-328), so `confidence` is
 * a number the responder attached rather than the key the list is in. A reader
 * that re-sorted would silently disagree with the run about what it thought most
 * likely whenever the two ever differed — and it is the run's opinion the panel
 * exists to show.
 *
 * Where only `current_hypothesis` exists it becomes a one-entry list with a null
 * excerpt — the top hypothesis IS the list's head, so the panel shape does not
 * have to change for a commander that sends one.
 */
export function rankedHypotheses(run: AgentRun | null): AgentRunRankedHypothesis[] {
  if (run === null) return []
  const ranked = run.hypotheses ?? []
  if (ranked.length > 0) return [...ranked]
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
  /** A read the lab took under the agent's token, labelled by the lab (WO-R3-333). */
  | 'lab_probe'
  /** An `agent.tool_invoked` row by a principal that is not this run's (F3). */
  | 'other_principal'
  | 'reset'
  | 'job_event'
  | 'human'

/** The two kinds the ledger hides behind the "reads hidden" toggle. */
export const HIDDEN_LEDGER_KINDS: readonly LedgerKind[] = ['lab_probe', 'other_principal']

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
  /**
   * The run's own `service_account_id` (WO-R3-334).
   *
   * The third take's ledger was a wall of `get_consumer_lag` every three seconds
   * under the AGENT principal, and none of it was the agent's: the demo runner built
   * two clients with the agent's token (F3) and the evaluator's guard probes fired
   * seven more calls after the reset boundary (F4). A row by another principal is
   * somebody else's read, and the page now says so instead of showing it as the
   * agent's.
   */
  runPrincipalId?: string | null
  /** Show the excluded reads — the toggle behind the "reads hidden" count. */
  showHiddenReads?: boolean
  /**
   * Oldest first, which is how the page draws it: newest at the BOTTOM, where the
   * eye already is while a run is live, so the ledger reads like a transcript.
   * Default stays newest-first.
   */
  oldestFirst?: boolean
}

export interface LedgerExclusions {
  /** `lab.probe` rows — the evaluator's own reads, labelled at the source. */
  labProbe: number
  /** `agent.tool_invoked` rows by a principal that is not the run's. */
  otherPrincipal: number
  total: number
}

/**
 * What the ledger is leaving out, counted so the page can say how much.
 *
 * "N evaluator/traffic reads hidden" with a toggle, rather than a silently shorter
 * list: the reads are real calls the platform really served, and an operator who
 * cannot see them cannot tell a quiet run from a filtered one.
 */
export function ledgerExclusions(input: {
  audit: AuditLog[]
  runPrincipalId?: string | null
}): LedgerExclusions {
  let labProbe = 0
  let otherPrincipal = 0
  for (const row of input.audit) {
    if (isLabProbeRow(row)) labProbe += 1
    else if (isForeignToolRow(row, input.runPrincipalId)) otherPrincipal += 1
  }
  return { labProbe, otherPrincipal, total: labProbe + otherPrincipal }
}

/** An `agent.tool_invoked` row that some other principal made. */
function isForeignToolRow(row: AuditLog, runPrincipalId: string | null | undefined): boolean {
  if (row.action !== AGENT_TOOL_ACTION) return false
  if (runPrincipalId === null || runPrincipalId === undefined) return false
  return row.principal_id !== runPrincipalId
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
  // A step's `at` is the responder's own and can be null — every field but `seq`
  // and `kind` can be. One with no time is placed by its neighbours rather than
  // dropped: `seq` is the order it happened in, which is what a ledger is for.
  const stepTimes = new Map<number, string>()
  let carried: string | null = null
  for (const step of [...input.steps].sort((a, b) => a.seq - b.seq)) {
    if (step.at !== null && step.at !== undefined) carried = step.at
    if (carried !== null) stepTimes.set(step.seq, carried)
  }
  const entries: LedgerEntry[] = input.steps.map((step) => ({
    id: `step-${String(step.seq)}`,
    at: step.at ?? stepTimes.get(step.seq) ?? '',
    kind: 'step' as const,
    step,
  }))

  for (const row of input.audit) {
    const kind = auditLedgerKind(row, {
      haveSteps,
      showJobEvents: input.showJobEvents === true,
      showHiddenReads: input.showHiddenReads === true,
      runPrincipalId: input.runPrincipalId,
    })
    if (kind === null) continue
    entries.push({ id: `row-${row.id}`, at: row.created_at, kind, row })
  }

  const newestFirst = entries.sort((a, b) => {
    if (a.at !== b.at) return a.at < b.at ? 1 : -1
    // Same instant: the step order is the run's own order, and a step is more
    // specific than the row that recorded it.
    return (b.step?.seq ?? 0) - (a.step?.seq ?? 0)
  })
  return input.oldestFirst === true ? newestFirst.reverse() : newestFirst
}

function auditLedgerKind(
  row: AuditLog,
  options: {
    haveSteps: boolean
    showJobEvents: boolean
    showHiddenReads: boolean
    runPrincipalId?: string | null
  },
): LedgerKind | null {
  if (isResetRow(row)) return 'reset'
  // Matched on the ACTION, not the principal: the evaluator is a service account
  // too, so a principal-only test files the lab's own rows under the agent and
  // makes the fault look like something the agent did.
  if (row.action.startsWith(LAB_ACTION_PREFIX)) return 'lab'
  // A lab row that is not the boundary is a read the lab took under the agent's own
  // token (WO-R3-333) — hidden with the other principals' reads, never drawn as the
  // agent's own.
  if (isLabProbeRow(row)) return options.showHiddenReads ? 'lab_probe' : null
  if (row.action.startsWith(JOB_EVENT_PREFIX)) return options.showJobEvents ? 'job_event' : null
  if (row.action === AGENT_RUN_REPORT_ACTION) return options.haveSteps ? null : 'agent_report'
  if (row.action === AGENT_TOOL_ACTION) {
    if (isForeignToolRow(row, options.runPrincipalId)) {
      return options.showHiddenReads ? 'other_principal' : null
    }
    // With steps, the steps ARE the ledger: these are the same calls without their
    // results, and drawing both would show every call twice.
    return options.haveSteps ? null : 'agent_audit'
  }
  if (row.principal_type === 'user') return 'human'
  return null
}

export interface LedgerCounts {
  /** Every step the responder reported, reports included. */
  steps: number
  /** The steps that were MCP calls — a `read` or an `action`. */
  calls: number
  /**
   * The `agent.tool_invoked` rows the platform recorded **for this run's own
   * principal**.
   *
   * The third take's page said "0 steps reported · 89 calls the platform recorded —
   * the two do not agree", and the 89 was mostly the runner reading lag every three
   * seconds under the agent's token. A count that includes somebody else's calls
   * cannot be compared with the run's own steps, so this one does not.
   */
  auditCalls: number
  labRows: number
  /** The rows the exclusions hid — the evaluator's and the runner's reads. */
  hiddenReads: number
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
  runPrincipalId?: string | null
}): LedgerCounts {
  const auditCalls = input.audit.filter(
    (r) => r.action === AGENT_TOOL_ACTION && !isForeignToolRow(r, input.runPrincipalId),
  ).length
  // Report steps have no `agent.tool_invoked` row — they audit as
  // `agent.run_reported` — so they are not part of the comparison.
  const calls = input.steps.filter((s) => s.kind !== 'report').length
  return {
    steps: input.steps.length,
    calls,
    auditCalls,
    labRows: input.audit.filter((r) => r.action.startsWith(LAB_ACTION_PREFIX)).length,
    hiddenReads: ledgerExclusions(input).total,
    agreed: calls === auditCalls,
  }
}

// ── one row, one line ────────────────────────────────────────────────────────
//
// The third take's ledger showed raw audit rows — tool name, arguments, a latency —
// and the one thing a viewer wants from a read is what it ANSWERED. A step carries a
// 400-character excerpt of that answer (ADR 0037), which is too much for a row and
// exactly right behind a click. So every row is one line by default:
//
//   08:19:44  READ  get_consumer_lag → lag 30, known
//
// and the summary on the right of the arrow comes from the table below: a named
// reading per tool, because "lag 30, known" is a sentence and the first sixty
// characters of a JSON blob is not.

/** How much of an unrecognised excerpt fits on one line. */
const GENERIC_SUMMARY_CHARS = 64

function collapse(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

/** The excerpt as an object, when it is one — the reporter sends JSON where a tool returns it. */
function excerptObject(excerpt: string): Record<string, unknown> | null {
  const text = excerpt.trim()
  if (!text.startsWith('{')) return null
  try {
    const parsed: unknown = JSON.parse(text)
    return parsed !== null && typeof parsed === 'object'
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    // A 400-character excerpt of a longer body is usually truncated JSON, which is
    // not parseable and not a defect: the regexes below read it as text.
    return null
  }
}

function numberField(o: Record<string, unknown> | null, key: string): number | null {
  const value = o?.[key]
  return typeof value === 'number' ? value : null
}

function boolField(o: Record<string, unknown> | null, key: string): boolean | null {
  const value = o?.[key]
  return typeof value === 'boolean' ? value : null
}

/** The first captured number of a pattern, from a prose or truncated-JSON excerpt. */
function firstNumber(text: string, pattern: RegExp): number | null {
  const found = pattern.exec(text)
  if (found === null) return null
  const value = Number(found[1])
  return Number.isFinite(value) ? value : null
}

type Summariser = (json: Record<string, unknown> | null, text: string) => string | null

/**
 * One summary per tool the demo's two scenarios touch, plus the readings a run
 * reaches for when it is guessing.
 *
 * Each one reads the parsed excerpt where it can and the text where it cannot, and
 * returns null to fall through to the generic excerpt rather than inventing a
 * reading — an excerpt that does not carry the field is not a field worth guessing.
 */
const SUMMARISERS: Record<string, Summariser> = {
  get_consumer_lag: (json, text) => {
    const known = boolField(json, 'lag_known')
    const lag = numberField(json, 'lag') ?? firstNumber(text, /lag\D{0,12}(-?\d+)/i)
    if (known === false || (lag === null && /unknown/i.test(text))) return 'lag unknown'
    if (lag === null) return null
    return `lag ${String(lag)}, known`
  },
  list_dlq_messages: (json, text) => {
    const total = numberField(json, 'total') ?? firstNumber(text, /total\D{0,8}(\d+)/i)
    return total === null ? null : `DLQ total ${String(total)}`
  },
  get_circuit_breakers: (json, text) => {
    const breakers = json?.breakers
    if (Array.isArray(breakers)) {
      const open = breakers.filter(
        (b): b is Record<string, unknown> =>
          b !== null && typeof b === 'object' && (b as Record<string, unknown>).state !== 'closed',
      )
      if (open.length === 0) {
        return `${String(breakers.length)} breaker${breakers.length === 1 ? '' : 's'}, none open`
      }
      return open
        .map((b) => `${String(b.name ?? 'breaker')} ${String(b.state ?? 'open')}`)
        .join(', ')
    }
    const open = /([\w-]+)\s*[:=]?\s*(half_open|open)\b/i.exec(text)
    if (open !== null) return `${open[1]} ${open[2].toLowerCase()}`
    return /none open|all closed/i.test(text) ? 'none open' : null
  },
  get_cache_key_info: (json, text) => {
    const exists = boolField(json, 'exists') ?? (/"?exists"?\s*[:=]\s*true/i.test(text) ? true : null)
    if (exists === false || /"?exists"?\s*[:=]\s*false/i.test(text)) return 'absent'
    if (exists !== true) return null
    const size = numberField(json, 'size_bytes') ?? firstNumber(text, /size_bytes\D{0,6}(\d+)/i)
    return size === null ? 'exists' : `exists, ${String(size)} bytes`
  },
  restart_consumer_group: (json, text) => {
    const accepted = boolField(json, 'accepted')
    const cleared = boolField(json, 'kill_key_cleared')
    if (accepted === null && !/accepted/i.test(text)) return null
    const parts = [accepted === false ? 'not accepted' : 'accepted']
    if (cleared !== null) parts.push(cleared ? 'kill key cleared' : 'kill key still set')
    return parts.join(', ')
  },
  mark_dlq_permanent: (json, text) =>
    json?.fenced_at !== undefined || /fenced/i.test(text) ? 'fenced' : null,
  get_dag_state: (json, text) => {
    const paused = boolField(json, 'paused')
    if (paused !== null) return paused ? 'paused' : 'not paused'
    return /"?paused"?\s*[:=]\s*true/i.test(text) ? 'paused' : null
  },
}

const REPLAY_TOOLS = ['replay_dlq_by_ids', 'replay_dlq_messages', 'replay_dlq_by_category']

function replaySummary(json: Record<string, unknown> | null, text: string): string | null {
  const replayed = numberField(json, 'replayed') ?? firstNumber(text, /replayed\D{0,6}(\d+)/i)
  if (replayed === null) return null
  const scheduled = numberField(json, 'scheduled') ?? firstNumber(text, /scheduled\D{0,6}(\d+)/i)
  return scheduled === null
    ? `replayed ${String(replayed)}`
    : `replayed ${String(replayed)}, scheduled ${String(scheduled)}`
}

/**
 * The one-line summary of what a call answered, or null where there is nothing to
 * summarise.
 *
 * The generic fallback is the excerpt itself, collapsed to one line and cut — a
 * reading nobody has written a summariser for is still better read than hidden, and
 * the full excerpt is one click away.
 */
export function summariseResult(
  tool: string | null | undefined,
  excerpt: string | null | undefined,
): string | null {
  if (excerpt === null || excerpt === undefined || excerpt.trim() === '') return null
  const text = collapse(excerpt)
  const json = excerptObject(excerpt)
  const named =
    tool === null || tool === undefined
      ? null
      : REPLAY_TOOLS.includes(tool)
        ? replaySummary(json, text)
        : (SUMMARISERS[tool]?.(json, text) ?? null)
  if (named !== null) return named
  return text.length > GENERIC_SUMMARY_CHARS
    ? `${text.slice(0, GENERIC_SUMMARY_CHARS)}…`
    : text
}

/** The same, for a step: the excerpt it carries, summarised by its own tool. */
export function summariseStep(step: AgentRunStepRecord): string | null {
  return summariseResult(step.tool, step.result_excerpt)
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
    .filter((s) => s.kind === 'action' && s.tool !== null && ACTION_TOOLS.includes(s.tool))
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
