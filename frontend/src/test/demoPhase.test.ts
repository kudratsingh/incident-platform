/**
 * The /demo page's phase derivation (WO-R3-313).
 *
 * The phase strip has two independent sources and the rule is that neither one
 * is allowed to overwrite the other:
 *
 *  - the agent's own word — the active `agent_run`'s `state` (WO-R3-312), which
 *    is the only source that can distinguish investigating from planning from
 *    remediating, because none of those three leaves a distinct mark on the
 *    platform;
 *  - the platform's own record — the audit log (a `chaos.*` row is the lab
 *    injecting the fault; `agent.tool_invoked` rows are the agent's footprint)
 *    plus the metric the scenario is about.
 *
 * Two things this file pins hardest. A fault that has not yet shown up in the
 * metric must NOT read as recovered — the metric is inside its threshold both
 * before the fault lands and after it is fixed, so "inside the threshold" only
 * means recovered once the breach has actually been observed. And when the two
 * sources disagree the reading carries both, because merging them invents a
 * state neither source asserted.
 */

import { describe, it, expect } from 'vitest'
import {
  agentPhase,
  derivePhase,
  dlqDecision,
  phaseTimeline,
  platformPhase,
} from '../utils/demoPhase'
import type { AgentRun, AgentRunState, AuditLog, Job } from '../types'

function auditRow(overrides: Partial<AuditLog>): AuditLog {
  return {
    id: crypto.randomUUID(),
    user_id: null,
    principal_type: 'service_account',
    principal_id: '33333333-3333-3333-3333-333333333333',
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

function toolRow(
  action: string,
  tool: string,
  at: string,
  args: Record<string, unknown> = {},
): AuditLog {
  return auditRow({
    action,
    created_at: at,
    extra_data: { tool_name: tool, arguments: args, outcome: 'success' },
  })
}

const FAULT = toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:00:00Z', {
  consumer_group: 'worker-dispatcher',
  ttl_seconds: 300,
})

function run(state: AgentRunState, history: AgentRunState[] = []): AgentRun {
  return {
    id: 'run-1',
    tenant_id: 'tenant-1',
    alert_id: 'alert-1',
    service_account_id: 'sa-1',
    scenario: 'remediate_consumer_lag_success',
    state,
    phase_history: [...history, state].map((s, i) => ({
      state: s,
      at: `2026-09-19T10:0${i + 1}:00Z`,
    })),
    current_hypothesis: null,
    last_step: null,
    briefing: null,
    started_at: '2026-09-19T10:01:00Z',
    updated_at: '2026-09-19T10:05:00Z',
    finished_at: null,
  }
}

describe('agentPhase — the agent’s own word', () => {
  it('has nothing to say when no run exists', () => {
    expect(agentPhase(null)).toBeNull()
  })

  it.each([
    ['triaging', 'agent_investigating'],
    ['investigating', 'agent_investigating'],
    ['planning', 'agent_planning'],
    ['remediating', 'agent_remediating'],
    ['verifying', 'verifying'],
    ['resolved', 'recovered'],
    ['escalated', 'escalated'],
    // `failed` is not a station of its own: the strip's terminal pair is
    // recovered | escalated, so a failed run lands on the not-recovered one
    // and the agent card still shows the run's real word.
    ['failed', 'escalated'],
  ] as const)('maps run state %s onto station %s', (state, phase) => {
    const reading = agentPhase(run(state))
    expect(reading).not.toBeNull()
    expect(reading!.phase).toBe(phase)
    expect(reading!.state).toBe(state)
  })

  it('carries the run’s own state word even where the station is coarser', () => {
    expect(agentPhase(run('triaging'))!.state).toBe('triaging')
    expect(agentPhase(run('failed'))!.state).toBe('failed')
  })
})

describe('platformPhase — what the platform itself can see', () => {
  const healthyMetric = {
    metricKnown: true,
    metricInsideThreshold: true,
    metricBreachedSinceFault: false,
  }

  it('is healthy with no lab row, however the metric reads', () => {
    expect(platformPhase({ audit: [], ...healthyMetric }).phase).toBe('healthy')
    expect(
      platformPhase({
        audit: [toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T09:00:00Z')],
        ...healthyMetric,
      }).phase,
    ).toBe('healthy')
  })

  it('reads fault injected off a chaos row, before the metric moves', () => {
    const reading = platformPhase({ audit: [FAULT], ...healthyMetric })
    expect(reading.phase).toBe('fault_injected')
    // The wall clock the header counts from is the platform's own row.
    expect(reading.faultAt).toBe('2026-09-19T10:00:00Z')
  })

  it('does NOT call an un-breached metric recovered', () => {
    // The trap: lag is 0 both before the kill lands and after the restart. A
    // rule that only looked at "inside the threshold" would flash recovered
    // during the seconds between injecting the fault and it becoming visible.
    expect(
      platformPhase({
        audit: [FAULT],
        metricKnown: true,
        metricInsideThreshold: true,
        metricBreachedSinceFault: false,
      }).phase,
    ).toBe('fault_injected')
  })

  it('reads the agent investigating from its reads after the fault', () => {
    const reading = platformPhase({
      audit: [
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
        FAULT,
      ],
      metricKnown: true,
      metricInsideThreshold: false,
      metricBreachedSinceFault: true,
    })
    expect(reading.phase).toBe('agent_investigating')
  })

  it('ignores agent reads that predate the fault', () => {
    const reading = platformPhase({
      audit: [
        FAULT,
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T09:59:00Z'),
      ],
      metricKnown: true,
      metricInsideThreshold: false,
      metricBreachedSinceFault: true,
    })
    expect(reading.phase).toBe('fault_injected')
  })

  it('reads remediating from a Tier-1 action, not from a read', () => {
    const reading = platformPhase({
      audit: [
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z', {
          consumer_group: 'worker-dispatcher',
        }),
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
        FAULT,
      ],
      metricKnown: true,
      metricInsideThreshold: false,
      metricBreachedSinceFault: true,
    })
    expect(reading.phase).toBe('agent_remediating')
  })

  it('reads recovered once a breached metric is back inside its threshold', () => {
    const reading = platformPhase({
      audit: [
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z'),
        FAULT,
      ],
      metricKnown: true,
      metricInsideThreshold: true,
      metricBreachedSinceFault: true,
    })
    expect(reading.phase).toBe('recovered')
  })

  it('will not claim recovery from an unknown metric', () => {
    // lag_known false — the reading is absent, with a reason. An absent
    // reading is not a healthy one (ADR 0030's rule, applied in the UI).
    const reading = platformPhase({
      audit: [FAULT],
      metricKnown: false,
      metricInsideThreshold: true,
      metricBreachedSinceFault: true,
    })
    expect(reading.phase).toBe('fault_injected')
    expect(reading.metricKnown).toBe(false)
  })
})

describe('derivePhase — the two sources side by side', () => {
  const agreeing = {
    run: run('remediating'),
    audit: [
      toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z'),
      FAULT,
    ],
    metricKnown: true,
    metricInsideThreshold: false,
    metricBreachedSinceFault: true,
  }

  it('reports agreement when both name the same station', () => {
    const reading = derivePhase(agreeing)
    expect(reading.agent!.phase).toBe('agent_remediating')
    expect(reading.platform.phase).toBe('agent_remediating')
    expect(reading.disagree).toBe(false)
  })

  it('keeps both readings when they disagree, and merges neither', () => {
    // The agent says it is verifying; the platform still reads a breached
    // metric with only a Tier-1 action to go on. Both are true statements
    // about what their own source knows.
    const reading = derivePhase({ ...agreeing, run: run('verifying') })
    expect(reading.disagree).toBe(true)
    expect(reading.agent!.phase).toBe('verifying')
    expect(reading.platform.phase).toBe('agent_remediating')
  })

  it('falls back to the platform alone before the run exists', () => {
    const reading = derivePhase({ ...agreeing, run: null })
    expect(reading.agent).toBeNull()
    expect(reading.platform.phase).toBe('agent_remediating')
    expect(reading.disagree).toBe(false)
  })

  it('walks the whole strip as a run progresses', () => {
    const order: AgentRunState[] = [
      'triaging',
      'investigating',
      'planning',
      'remediating',
      'verifying',
      'resolved',
    ]
    const seen = order.map((s) => derivePhase({ ...agreeing, run: run(s) }).agent!.phase)
    expect(seen).toEqual([
      'agent_investigating',
      'agent_investigating',
      'agent_planning',
      'agent_remediating',
      'verifying',
      'recovered',
    ])
  })
})

describe('phaseTimeline — durations from the append-only history', () => {
  it('measures each phase against the next one’s start', () => {
    const r: AgentRun = {
      ...run('planning'),
      phase_history: [
        { state: 'triaging', at: '2026-09-19T10:00:00Z' },
        { state: 'investigating', at: '2026-09-19T10:00:30Z' },
        { state: 'planning', at: '2026-09-19T10:01:30Z' },
      ],
      finished_at: null,
    }
    const rows = phaseTimeline(r)
    expect(rows.map((row) => row.durationMs)).toEqual([30_000, 60_000, null])
  })

  it('closes the last phase at finished_at when the run has ended', () => {
    const r: AgentRun = {
      ...run('resolved'),
      phase_history: [
        { state: 'verifying', at: '2026-09-19T10:00:00Z' },
        { state: 'resolved', at: '2026-09-19T10:00:10Z' },
      ],
      finished_at: '2026-09-19T10:00:15Z',
    }
    expect(phaseTimeline(r).map((row) => row.durationMs)).toEqual([10_000, 5_000])
  })

  it('is empty for a run with no history yet', () => {
    expect(phaseTimeline({ ...run('triaging'), phase_history: [] })).toEqual([])
  })
})

describe('dlqDecision — what the agent decided about one row', () => {
  const job = {
    id: '44444444-4444-4444-4444-444444444444',
    type: 'bulk_api_sync',
    remediation_hint: 'replay_safe',
  } as unknown as Job

  it('is leave when the agent has touched nothing', () => {
    expect(dlqDecision(job, [])).toBe('leave')
  })

  it('is replay when a by-id replay names this row', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'replay_dlq_by_ids', '2026-09-19T10:04:00Z', {
        job_ids: [job.id],
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('replay')
  })

  it('is leave when a by-id replay names a different row', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'replay_dlq_by_ids', '2026-09-19T10:04:00Z', {
        job_ids: ['55555555-5555-5555-5555-555555555555'],
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('leave')
  })

  it('is replay when a by-category replay covers this row’s hint', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'replay_dlq_by_category', '2026-09-19T10:04:00Z', {
        category: 'replay_safe',
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('replay')
  })

  it('is leave when a by-category replay covers a different hint', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'replay_dlq_by_category', '2026-09-19T10:04:00Z', {
        category: 'human_required',
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('leave')
  })

  it('is leave when a by-category replay is narrowed to another job type', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'replay_dlq_by_category', '2026-09-19T10:04:00Z', {
        category: 'replay_safe',
        job_type: 'csv_upload',
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('leave')
  })

  it('is fence when the agent marked this row permanent', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'mark_dlq_permanent', '2026-09-19T10:04:00Z', {
        job_id: job.id,
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('fence')
  })

  it('takes the newest decision when the agent changed its mind', () => {
    const rows = [
      toolRow('agent.tool_invoked', 'mark_dlq_permanent', '2026-09-19T10:06:00Z', {
        job_id: job.id,
      }),
      toolRow('agent.tool_invoked', 'replay_dlq_by_ids', '2026-09-19T10:04:00Z', {
        job_ids: [job.id],
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('fence')
  })

  it('ignores a lab row that names the same id', () => {
    // `chaos.tool_invoked` is the evaluator, not the agent. Attributing the
    // lab's own seeding to the agent would badge every seeded row.
    const rows = [
      toolRow('chaos.tool_invoked', 'mark_dlq_permanent', '2026-09-19T09:59:00Z', {
        job_id: job.id,
      }),
    ]
    expect(dlqDecision(job, rows)).toBe('leave')
  })
})
