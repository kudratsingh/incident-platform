/** The pure readers behind the v6 ledger and briefing card, on the take-7 record (WO-R3-354). */

import { describe, it, expect } from 'vitest'
import {
  PHASE_MEANING,
  actionTaken,
  buildLedger,
  countsSentence,
  ledgerCounts,
  ledgerDisplayOrder,
  phaseMeaning,
  stepAnswersForRun,
  verifyJudgedOn,
} from '../utils/demoRun'
import take7Json from './fixtures/take7-run.json'
import priorJson from './fixtures/take7-prior-run.json'
import type { AgentRun, AgentRunState, AgentRunStepsResponse } from '../types'

const TAKE7 = take7Json as unknown as AgentRun
const PRIOR = priorJson as unknown as AgentRun
const STEPS = TAKE7.steps ?? []

const ALL_STATES: AgentRunState[] = [
  'triage',
  'investigating',
  'planning',
  'awaiting_approval',
  'remediating',
  'verifying',
  'resolved',
  'escalated',
  'failed',
]

describe('phase meanings', () => {
  it('has a sentence for every state, and a present-tense exit only for the live ones', () => {
    for (const state of ALL_STATES) {
      const meaning = PHASE_MEANING[state]
      expect(meaning.does.length).toBeGreaterThan(20)
      const terminal = state === 'resolved' || state === 'escalated' || state === 'failed'
      expect(meaning.now === null).toBe(terminal)
    }
    expect(PHASE_MEANING.verifying.now).toBe(
      'waiting for a reading taken after the action that is inside the threshold',
    )
  })

  it('names an unknown state rather than inventing a meaning for it', () => {
    expect(phaseMeaning('paused').does).toMatch(/not a state this page knows/)
    expect(phaseMeaning('paused').now).toBeNull()
  })
})

describe('the ledger with its transitions', () => {
  it('puts each step under the state it ran in, newest section first', () => {
    const entries = buildLedger({ steps: STEPS, audit: [], phaseHistory: TAKE7.phase_history })
    const order = ledgerDisplayOrder(entries).map((e) =>
      e.kind === 'phase' ? `== ${e.phase?.state ?? ''}` : `${String(e.step?.seq)}`,
    )
    expect(order).toEqual([
      '== resolved',
      '== verifying',
      '6',
      '5',
      '== remediating',
      '4',
      '== planning',
      '== investigating',
      '3',
      '2',
      '1',
      '== triage',
    ])
  })

  it('keeps the flat order where there is no phase history', () => {
    const entries = buildLedger({ steps: STEPS, audit: [] })
    expect(ledgerDisplayOrder(entries).map((e) => e.step?.seq)).toEqual([6, 5, 4, 3, 2, 1])
  })
})

describe('the counts line', () => {
  it('says the tool calls and the planner/judge steps plainly', () => {
    const counts = ledgerCounts({ steps: STEPS, audit: [], runPrincipalId: TAKE7.service_account_id })
    expect(countsSentence(counts, 3)).toBe(
      '3 tool calls (reads + actions) · 3 planner/judge steps · the platform recorded 0 tool calls',
    )
    expect(countsSentence({ ...counts, auditCalls: 3 }, 3)).toBe(
      '3 tool calls (reads + actions) · 3 planner/judge steps',
    )
  })
})

describe('what a verify judge was given', () => {
  it('reads the take-7 judge’s reading as taken before the action', () => {
    const judge = STEPS.find((s) => s.seq === 6)
    expect(judge).toBeDefined()
    expect(verifyJudgedOn(judge!, STEPS)).toBe('judged on lag 55 measured 3 s BEFORE the action')
  })

  it('says after where the reading is newer than the action', () => {
    const steps = STEPS.map((s) =>
      s.seq === 5
        ? { ...s, result_excerpt: '{"lag":0,"lag_known":true,"measured_at":"2026-09-27T08:52:16.1Z"}' }
        : s,
    )
    expect(verifyJudgedOn(steps.find((s) => s.seq === 6)!, steps)).toBe(
      'judged on lag 0 measured 5 s after the action',
    )
  })

  it('is null for a step that is not a verify judge', () => {
    expect(verifyJudgedOn(STEPS[0], STEPS)).toBeNull()
  })
})

describe('the action a run took', () => {
  it('reads tool, arguments and acceptance off the action step', () => {
    expect(actionTaken(TAKE7, STEPS)).toEqual({
      tool: 'restart_consumer_group',
      argumentsSummary: 'consumer_group=worker-dispatcher',
      accepted: true,
      at: '2026-09-27T08:52:11.050154Z',
    })
  })

  it('falls back to the plan, with acceptance unknown, before the action step is in', () => {
    expect(actionTaken(TAKE7, STEPS.filter((s) => s.seq < 4))).toEqual({
      tool: 'restart_consumer_group',
      argumentsSummary: 'consumer_group=worker-dispatcher',
      accepted: null,
      at: null,
    })
  })

  it('is null for a run that never planned or acted', () => {
    expect(actionTaken({ ...TAKE7, plan: null }, STEPS.filter((s) => s.kind !== 'action'))).toBeNull()
  })
})

describe('the step answers the page may merge', () => {
  const tail = (run: AgentRun): AgentRunStepsResponse => ({
    run_id: run.id,
    state: run.state,
    finished_at: run.finished_at,
    steps: run.steps ?? [],
    returned: 0,
    total: 0,
    steps_dropped: 0,
    after_seq: null,
    next_after_seq: 8,
  })

  it('drops a parked answer that belongs to the previous run', () => {
    const got = stepAnswersForRun(TAKE7.id, PRIOR, tail(PRIOR))
    expect(got.steps).toEqual([])
    expect(got.cursor).toBeNull()
  })

  it('keeps both answers when they are the selected run’s', () => {
    const got = stepAnswersForRun(TAKE7.id, TAKE7, { ...tail(TAKE7), next_after_seq: 6 })
    expect(got.steps.length).toBe(12)
    expect(got.cursor).toBe(6)
  })
})
