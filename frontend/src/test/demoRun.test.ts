/**
 * The run record's own derivations (WO-R3-330).
 *
 * The first live take had an empty agent panel and an unreadable timeline, and
 * both failures were about shape rather than layout: the run record carried a
 * single `current_hypothesis` (null for the whole run), the audit log carried no
 * tool *results*, and 43 of the 50 rows on screen were the traffic loop's job
 * events. WO-R3-328 gives the record a ranked hypothesis list, a plan, the verify
 * verdicts, a budget and an append-only `steps` list; everything here derives the
 * panels from those and degrades honestly where a stack or a commander is older
 * than that.
 *
 * Two rules worth stating because they are what the tests hold:
 *
 *  - **An absence is named, not filled.** A run with no `hypotheses` is not a run
 *    with no hypotheses: `hypothesesSource` distinguishes "ranked list" from "only
 *    a top hypothesis" from "nothing", so the panel can say which it is looking at.
 *  - **`steps` is the ledger's authority when it exists.** The audit log's
 *    `agent.tool_invoked` rows are the same calls without their results, so mixing
 *    both would double every row; the ledger uses the audit rows only as a fallback
 *    and counts the difference so a reporter that stopped reporting is visible.
 */

import { describe, it, expect } from 'vitest'
import {
  budgetMeter,
  buildLedger,
  dlqDecisionFromSteps,
  hypothesesSource,
  ledgerCounts,
  mergeSteps,
  rankedHypotheses,
  runVerifications,
} from '../utils/demoRun'
import { WORLD_RESET_ACTION } from '../utils/demoPhase'
import type {
  AgentRun,
  AgentRunStepRecord,
  AuditLog,
  Job,
} from '../types'

function step(
  seq: number,
  kind: AgentRunStepRecord['kind'],
  tool: string,
  at: string,
  overrides: Partial<AgentRunStepRecord> = {},
): AgentRunStepRecord {
  return {
    seq,
    kind,
    tool,
    at,
    arguments: {},
    result_excerpt: null,
    outcome: 'success',
    latency_ms: 12.5,
    ...overrides,
  }
}

function auditRow(overrides: Partial<AuditLog>): AuditLog {
  return {
    id: crypto.randomUUID(),
    user_id: null,
    principal_type: 'service_account',
    principal_id: 'sa-1',
    job_id: null,
    action: 'agent.tool_invoked',
    resource_type: 'mcp_tool',
    resource_id: null,
    request_id: null,
    ip_address: null,
    extra_data: null,
    created_at: '2026-09-19T10:00:00Z',
    ...overrides,
  }
}

function toolRow(action: string, tool: string, at: string, args: Record<string, unknown> = {}) {
  return auditRow({
    action,
    created_at: at,
    extra_data: { tool_name: tool, arguments: args, outcome: 'success' },
  })
}

function baseRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'run-1',
    tenant_id: 't-1',
    alert_id: 'alert-1',
    service_account_id: 'sa-1',
    scenario: 'remediate_consumer_lag_success',
    state: 'investigating',
    phase_history: [{ state: 'triage', at: '2026-09-19T10:01:00Z' }],
    current_hypothesis: null,
    last_step: null,
    briefing: null,
    started_at: '2026-09-19T10:01:00Z',
    updated_at: '2026-09-19T10:02:00Z',
    finished_at: null,
    active: true,
    ...overrides,
  }
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-aaaa',
    user_id: 'u-1',
    type: 'bulk_api_sync',
    status: 'dead_letter',
    idempotency_key: null,
    payload: null,
    result: null,
    error_message: 'boom',
    retry_count: 3,
    max_attempts: 3,
    dead_lettered_by: null,
    priority: 0,
    trace_id: null,
    created_at: '2026-09-19T09:00:00Z',
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

describe('rankedHypotheses — the panel the first take could not fill', () => {
  it('has nothing to show without a run', () => {
    expect(rankedHypotheses(null)).toEqual([])
    expect(hypothesesSource(null)).toBe('none')
  })

  it('carries the ranked list, highest confidence first', () => {
    const run = baseRun({
      hypotheses: [
        { name: 'b', category: 'cache', confidence: 0.2, reasoning_excerpt: 'weak' },
        { name: 'a', category: 'consumer', confidence: 0.7, reasoning_excerpt: 'lag is 42' },
      ],
    })
    expect(rankedHypotheses(run).map((h) => h.name)).toEqual(['a', 'b'])
    expect(rankedHypotheses(run)[0].reasoning_excerpt).toBe('lag is 42')
    expect(hypothesesSource(run)).toBe('ranked')
  })

  it('falls back to the single current hypothesis, and says that is all it got', () => {
    const run = baseRun({
      current_hypothesis: { name: 'consumer down', category: 'consumer', confidence: 0.8 },
    })
    expect(rankedHypotheses(run)).toEqual([
      { name: 'consumer down', category: 'consumer', confidence: 0.8, reasoning_excerpt: null },
    ])
    // The distinction the panel has to draw: a commander older than WO-R3-329
    // reports one hypothesis and no reasoning, which is not the same finding as
    // a run that ranked nothing.
    expect(hypothesesSource(run)).toBe('current_only')
  })

  it('reports nothing where the run reported nothing', () => {
    const run = baseRun({ hypotheses: [], current_hypothesis: null })
    expect(rankedHypotheses(run)).toEqual([])
    expect(hypothesesSource(run)).toBe('none')
  })
})

describe('runVerifications — every verify poll, oldest first', () => {
  it('prefers the list over the latest-only field', () => {
    const run = baseRun({
      verification: { verdict: 'verified', attempt: 2, of: 2 },
      verifications: [
        { verdict: 'not_verified', attempt: 1, of: 2, reasoning_excerpt: 'lag still 42' },
        { verdict: 'verified', attempt: 2, of: 2, reasoning_excerpt: 'lag 0' },
      ],
    })
    expect(runVerifications(run).map((v) => v.verdict)).toEqual(['not_verified', 'verified'])
  })

  it('uses the latest-only field when the list is absent', () => {
    const run = baseRun({ verification: { verdict: 'verified_stabilizer' } })
    expect(runVerifications(run)).toEqual([{ verdict: 'verified_stabilizer' }])
  })

  it('is empty for a run that has not verified anything', () => {
    expect(runVerifications(baseRun())).toEqual([])
    expect(runVerifications(null)).toEqual([])
  })
})

describe('budgetMeter — used against a cap, never a bare number', () => {
  it('computes the percentage of the cap', () => {
    const meter = budgetMeter({ tool_calls_used: 7, tool_calls_max: 13, usd_used: 0.42 })
    expect(meter.used).toBe(7)
    expect(meter.max).toBe(13)
    expect(meter.percent).toBeCloseTo((7 / 13) * 100, 5)
    expect(meter.usd).toBe(0.42)
  })

  it('has no percentage without a cap, rather than assuming one', () => {
    expect(budgetMeter({ tool_calls_used: 7 }).percent).toBeNull()
    expect(budgetMeter(null).used).toBeNull()
    expect(budgetMeter(undefined).percent).toBeNull()
  })

  it('clamps a run that went over its cap to a full bar but keeps the true numbers', () => {
    const meter = budgetMeter({ tool_calls_used: 15, tool_calls_max: 13 })
    expect(meter.percent).toBe(100)
    expect(meter.used).toBe(15)
    expect(meter.over).toBe(true)
  })
})

describe('mergeSteps — an append-only list polled incrementally', () => {
  const a = step(1, 'read', 'get_consumer_lag', '2026-09-19T10:01:00Z')
  const b = step(2, 'read', 'list_dlq_messages', '2026-09-19T10:01:10Z')
  const c = step(3, 'action', 'restart_consumer_group', '2026-09-19T10:02:00Z')

  it('keeps one entry per seq, in order, whatever order they arrive in', () => {
    expect(mergeSteps([c, a], [b]).map((s) => s.seq)).toEqual([1, 2, 3])
  })

  it('lets a later answer replace an earlier one for the same seq', () => {
    const revised = step(2, 'read', 'list_dlq_messages', '2026-09-19T10:01:10Z', {
      result_excerpt: 'five rows',
    })
    const merged = mergeSteps([a, b], [revised])
    expect(merged).toHaveLength(2)
    expect(merged[1].result_excerpt).toBe('five rows')
  })

  it('is a no-op when the incremental poll has nothing new', () => {
    expect(mergeSteps([a, b], [])).toHaveLength(2)
  })
})

describe('buildLedger — the run’s own actions, with the lab interleaved', () => {
  const steps = [
    step(1, 'read', 'get_consumer_lag', '2026-09-19T10:01:00Z', {
      result_excerpt: 'lag 42, known',
    }),
    step(2, 'action', 'restart_consumer_group', '2026-09-19T10:02:00Z'),
    step(3, 'report', 'report_agent_run', '2026-09-19T10:02:01Z'),
  ]
  const fault = toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:00:30Z')
  const reset = auditRow({ action: WORLD_RESET_ACTION, created_at: '2026-09-19T10:00:00Z' })
  const jobEvent = auditRow({ action: 'event.job.completed', created_at: '2026-09-19T10:01:30Z' })
  const human = auditRow({
    action: 'job.replayed',
    principal_type: 'user',
    created_at: '2026-09-19T10:01:40Z',
  })

  it('is newest first, one entry per step, with the lab row in its place', () => {
    const ledger = buildLedger({ steps, audit: [fault, reset] })
    expect(ledger.map((e) => e.kind)).toEqual([
      'step',
      'step',
      'step',
      'lab',
      'reset',
    ])
    expect(ledger[0].step?.seq).toBe(3)
    expect(ledger[3].row?.action).toBe('chaos.tool_invoked')
  })

  it('leaves job events out by default and adds them on the toggle', () => {
    const off = buildLedger({ steps, audit: [fault, reset, jobEvent] })
    expect(off.some((e) => e.kind === 'job_event')).toBe(false)

    const on = buildLedger({ steps, audit: [fault, reset, jobEvent], showJobEvents: true })
    const events = on.filter((e) => e.kind === 'job_event')
    expect(events).toHaveLength(1)
    expect(events[0].row?.action).toBe('event.job.completed')
  })

  it('does not draw the agent’s own rows twice when the steps carry them', () => {
    const duplicate = toolRow(
      'agent.tool_invoked',
      'restart_consumer_group',
      '2026-09-19T10:02:00Z',
    )
    const reported = auditRow({
      action: 'agent.run_reported',
      created_at: '2026-09-19T10:02:01Z',
    })
    const ledger = buildLedger({ steps, audit: [fault, duplicate, reported] })
    expect(ledger.filter((e) => e.kind === 'step')).toHaveLength(3)
    expect(ledger.some((e) => e.kind === 'agent_audit')).toBe(false)
    expect(ledger.some((e) => e.kind === 'agent_report')).toBe(false)
  })

  it('falls back to the audit rows when no step was reported, results and all missing', () => {
    const invoked = toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z')
    const reported = auditRow({
      action: 'agent.run_reported',
      created_at: '2026-09-19T10:01:05Z',
    })
    const ledger = buildLedger({ steps: [], audit: [invoked, reported, fault] })
    expect(ledger.map((e) => e.kind)).toEqual(['agent_report', 'agent_audit', 'lab'])
  })

  it('keeps a human’s own row, which is neither the lab nor the agent', () => {
    const ledger = buildLedger({ steps, audit: [human] })
    expect(ledger.some((e) => e.kind === 'human')).toBe(true)
  })

  it('counts what the two witnesses each saw, so a stopped reporter shows', () => {
    // Two of the three steps are MCP calls; the report step audits as
    // `agent.run_reported`, so it is not part of the comparison.
    const invoked = [
      toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
      toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:02:00Z'),
    ]
    expect(ledgerCounts({ steps, audit: [...invoked, fault] })).toEqual({
      steps: 3,
      calls: 2,
      auditCalls: 2,
      labRows: 1,
      agreed: true,
    })
    // One witness ahead of the other is the interesting case: the platform saw
    // both calls and the reporter filed one, which is what a reporter that died
    // mid-run looks like.
    const behind = ledgerCounts({ steps: steps.slice(0, 1), audit: [...invoked, fault] })
    expect(behind.calls).toBe(1)
    expect(behind.auditCalls).toBe(2)
    expect(behind.agreed).toBe(false)
  })
})

describe('dlqDecisionFromSteps — the badge, from the steps rather than the audit', () => {
  it('is leave when the run acted on nothing', () => {
    expect(dlqDecisionFromSteps(job(), [])).toBe('leave')
  })

  it('is replay when a by-id replay names this row', () => {
    const steps = [
      step(4, 'action', 'replay_dlq_by_ids', '2026-09-19T10:02:00Z', {
        arguments: { job_ids: ['job-aaaa'] },
      }),
    ]
    expect(dlqDecisionFromSteps(job(), steps)).toBe('replay')
  })

  it('is fence when the run marked this row permanent', () => {
    const steps = [
      step(4, 'action', 'mark_dlq_permanent', '2026-09-19T10:02:00Z', {
        arguments: { job_id: 'job-aaaa' },
      }),
    ]
    expect(dlqDecisionFromSteps(job(), steps)).toBe('fence')
  })

  it('is replay when a by-category replay covers this row’s hint', () => {
    const steps = [
      step(4, 'action', 'replay_dlq_by_category', '2026-09-19T10:02:00Z', {
        arguments: { category: 'replay_safe' },
      }),
    ]
    expect(dlqDecisionFromSteps(job({ remediation_hint: 'replay_safe' }), steps)).toBe('replay')
    expect(dlqDecisionFromSteps(job({ remediation_hint: null }), steps)).toBe('leave')
  })

  it('takes the newest decision by seq when the run changed its mind', () => {
    const steps = [
      step(4, 'action', 'replay_dlq_by_ids', '2026-09-19T10:02:00Z', {
        arguments: { job_ids: ['job-aaaa'] },
      }),
      step(9, 'action', 'mark_dlq_permanent', '2026-09-19T10:04:00Z', {
        arguments: { job_id: 'job-aaaa' },
      }),
    ]
    expect(dlqDecisionFromSteps(job(), steps)).toBe('fence')
  })

  it('ignores a read that merely looked at the row', () => {
    const steps = [
      step(4, 'read', 'list_dlq_messages', '2026-09-19T10:02:00Z', {
        arguments: { job_ids: ['job-aaaa'] },
      }),
    ]
    expect(dlqDecisionFromSteps(job(), steps)).toBe('leave')
  })
})
