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
 * Three things this file pins hardest. A fault that has not yet shown up in the
 * metric must NOT read as recovered — the metric is inside its threshold both
 * before the fault lands and after it is fixed, so "inside the threshold" only
 * means recovered once the breach has actually been observed. When the two
 * sources disagree the reading carries both, because merging them invents a
 * state neither source asserted. And since WO-R3-327, both sources are read only
 * after the newest `lab.world_reset` row: the audit log is append-only, so without
 * that boundary a freshly reset world still reported the previous take's fault,
 * its remediation and its DLQ decisions as the current ones.
 */

import { describe, it, expect } from 'vitest'
import {
  MIN_SPAN_MS,
  WORLD_RESET_ACTION,
  agentPhase,
  agentRow,
  agentStateLabel,
  chartMarkers,
  chartWindow,
  currentTake,
  derivePhase,
  dlqDecision,
  faultInTake,
  isResetRow,
  metricRecovery,
  newestFaultAt,
  newestResetAt,
  phaseTimeline,
  platformPhase,
  platformRow,
  reportArrivals,
  revealStations,
  rowsInTake,
  rowsInTakeWithEdges,
  runSinceReset,
  runsInTake,
  runsSinceReset,
  selectRun,
  selectTake,
  takeAt,
  takeBoundaries,
  takeHasEnded,
  takeKey,
  takeOfRun,
  yAxisTicks,
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

/** The boundary `make eval-reset` appends (WO-R3-327); its payload is the reset's counters. */
function resetRow(at: string): AuditLog {
  return auditRow({
    action: WORLD_RESET_ACTION,
    created_at: at,
    resource_type: 'world',
    extra_data: { chaos_keys_cleared: 4, hot_set_reseeded: 1 },
  })
}

/** A boundary before the fault, so `healthy` has a start to be measured from. */
const RESET_FIRST = resetRow('2026-09-19T09:59:00Z')

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
    active: true,
  }
}

describe('agentPhase — the agent’s own word', () => {
  it('has nothing to say when no run exists', () => {
    expect(agentPhase(null)).toBeNull()
  })

  it.each([
    ['triage', 'agent_investigating'],
    ['investigating', 'agent_investigating'],
    ['planning', 'agent_planning'],
    ['awaiting_approval', 'awaiting_approval'],
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
    expect(agentPhase(run('triage'))!.state).toBe('triage')
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

describe('the reset is a boundary — WO-R3-327', () => {
  const healthyMetric = {
    metricKnown: true,
    metricInsideThreshold: true,
    metricBreachedSinceFault: false,
  }
  /** The previous take, in full: fault, investigation, remediation, recovery. */
  const previousTake: AuditLog[] = [
    FAULT,
    toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
    toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z', {
      consumer_group: 'worker-dispatcher',
    }),
  ]
  const RESET = resetRow('2026-09-19T10:05:00Z')

  it('recognises the reset row, and does not mistake it for a fault', () => {
    expect(isResetRow(RESET)).toBe(true)
    expect(isResetRow(FAULT)).toBe(false)
    // The bug this fixes in one line: a reset filed under `chaos.` would have BEEN
    // the newest fault.
    expect(newestFaultAt([...previousTake, RESET])).toBeNull()
  })

  it('finds the newest boundary when a session has several takes', () => {
    const older = resetRow('2026-09-19T09:00:00Z')
    expect(newestResetAt([older, RESET])).toBe('2026-09-19T10:05:00Z')
    expect(newestResetAt(previousTake)).toBeNull()
  })

  it('reads healthy after a reset, however complete the previous take was', () => {
    // THE assertion. Before this, the newest `chaos.*` row was still the last
    // take's kill, so a freshly wiped world opened the strip at `agent
    // remediating` with a clock counting from an incident that no longer existed.
    const reading = platformPhase({ audit: [...previousTake, RESET], ...healthyMetric })
    expect(reading.phase).toBe('healthy')
    expect(reading.faultAt).toBeNull()
    expect(reading.resetAt).toBe('2026-09-19T10:05:00Z')
  })

  it('reads the next take’s fault, and only its rows', () => {
    const audit = [
      ...previousTake,
      RESET,
      toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:06:00Z', {
        consumer_group: 'worker-dispatcher',
      }),
    ]
    const reading = platformPhase({ audit, ...healthyMetric })
    expect(reading.phase).toBe('fault_injected')
    expect(reading.faultAt).toBe('2026-09-19T10:06:00Z')
  })

  it('does not credit the previous take’s remediation to the new one', () => {
    const audit = [
      ...previousTake,
      RESET,
      toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:06:00Z'),
    ]
    const reading = platformPhase({
      audit,
      metricKnown: true,
      metricInsideThreshold: false,
      metricBreachedSinceFault: true,
    })
    // The `restart_consumer_group` row is older than the boundary, so the strip
    // must not read `agent remediating` on a fault nobody has touched yet.
    expect(reading.phase).toBe('fault_injected')
  })

  it('leaves today’s behaviour alone when no reset row exists', () => {
    const reading = platformPhase({
      audit: previousTake,
      metricKnown: true,
      metricInsideThreshold: false,
      metricBreachedSinceFault: true,
    })
    expect(reading.phase).toBe('agent_remediating')
    expect(reading.faultAt).toBe('2026-09-19T10:00:00Z')
    expect(reading.resetAt).toBeNull()
  })

  it('does not badge a fresh DLQ row with the previous take’s decision', () => {
    // The seeded rows come back under STABLE ids, so a replay from the last take names
    // the same job id as the row this take just planted.
    const row: Job = { id: 'job-1' } as Job
    const audit = [
      toolRow('agent.tool_invoked', 'replay_dlq_by_ids', '2026-09-19T10:03:00Z', {
        job_ids: ['job-1'],
      }),
      resetRow('2026-09-19T10:05:00Z'),
    ]
    expect(dlqDecision(row, audit)).toBe('leave')
    // And the same call after the boundary still counts.
    expect(
      dlqDecision(row, [
        ...audit,
        toolRow('agent.tool_invoked', 'replay_dlq_by_ids', '2026-09-19T10:06:00Z', {
          job_ids: ['job-1'],
        }),
      ]),
    ).toBe('replay')
  })

  it('a row exactly at the boundary belongs to the take being closed', () => {
    const simultaneous = toolRow(
      'chaos.tool_invoked',
      'kill_consumer',
      '2026-09-19T10:05:00Z',
    )
    expect(newestFaultAt([simultaneous, RESET])).toBeNull()
  })
})

describe('runSinceReset — the agent card ignores a closed-out take', () => {
  const RESET_AT = '2026-09-19T10:05:00Z'

  function finished(id: string, at: string): AgentRun {
    return { ...run('failed'), id, finished_at: at, active: false }
  }

  it('picks the newest run when there is no boundary', () => {
    const older = { ...run('investigating'), id: 'a', started_at: '2026-09-19T10:00:00Z' }
    const newer = { ...run('planning'), id: 'b', started_at: '2026-09-19T10:02:00Z' }
    expect(runSinceReset([older, newer], null)?.id).toBe('b')
  })

  it('has nothing to show when every run was closed before the boundary', () => {
    // What `make eval-reset` leaves behind: it closes open runs as `failed` with a
    // `closed_by: reset` marker, and those are the previous take's.
    const runs = [finished('a', '2026-09-19T10:04:00Z'), finished('b', RESET_AT)]
    expect(runSinceReset(runs, RESET_AT)).toBeNull()
  })

  it('shows a run that started after the boundary', () => {
    const runs = [
      finished('old', '2026-09-19T10:04:00Z'),
      { ...run('investigating'), id: 'new', started_at: '2026-09-19T10:06:00Z' },
    ]
    expect(runSinceReset(runs, RESET_AT)?.id).toBe('new')
  })

  it('keeps a still-open run the reset did not manage to close', () => {
    // Deliberately kept rather than hidden: an open run older than the boundary is
    // either a race with the reset's own sweep or a responder it could not reach,
    // and on camera that disagreement is worth seeing.
    const stillOpen = {
      ...run('remediating'),
      id: 'open',
      started_at: '2026-09-19T10:01:00Z',
    }
    expect(runSinceReset([stillOpen], RESET_AT)?.id).toBe('open')
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
    // All nine reportable states, in the order a run walks them. These are the
    // commander's own IncidentState values character for character — there is
    // no mapping layer on either side of the wire, which is what makes an
    // unhandled member a bug rather than a fallback.
    const order: AgentRunState[] = [
      'triage',
      'investigating',
      'planning',
      'awaiting_approval',
      'remediating',
      'verifying',
      'resolved',
    ]
    const seen = order.map((s) => derivePhase({ ...agreeing, run: run(s) }).agent!.phase)
    expect(seen).toEqual([
      'agent_investigating',
      'agent_investigating',
      'agent_planning',
      'awaiting_approval',
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
        { state: 'triage', at: '2026-09-19T10:00:00Z' },
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
    expect(phaseTimeline({ ...run('triage'), phase_history: [] })).toEqual([])
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

// ───────────────────────────────────────────────────────────────────────────────
// WO-R3-330 — the two rows, the latched fault, and recovery read off the
// platform's own samples rather than off one poll.
//
// The first live take failed here in two ways. The strip switched from the
// platform's reading to the agent's the second a run existed, so `fault injected`
// — a station only the platform can assert — was never on screen. And the
// recovery rule fired on a single sub-threshold reading: the cached lag sample
// reads 42 → 0 → 42 as it ages, so the strip called the world recovered in the
// middle of the incident. Both rows are now always rendered, the fault is latched
// for the whole take, and recovery needs two consecutive inside-the-bar samples
// from the platform's own 15-minute window.
// ───────────────────────────────────────────────────────────────────────────────

describe('platformRow — four stations the platform can assert for itself', () => {
  const FAULT_AT = '2026-09-19T10:00:00Z'
  const metric = {
    metricKnown: true,
    metricInsideThreshold: false,
    metricBreachedSinceFault: true,
  }

  it('is four stations, always, whatever the agent is doing', () => {
    const row = platformRow({ audit: [], faultAt: null, ...metric })
    expect(row.map((s) => s.key)).toEqual([
      'healthy',
      'fault_injected',
      'agent_acting',
      'recovered',
    ])
  })

  it('sits on healthy before any fault, with nothing else reached', () => {
    const row = platformRow({
      audit: [],
      faultAt: null,
      metricKnown: true,
      metricInsideThreshold: true,
      metricBreachedSinceFault: false,
    })
    expect(row[0].state).toBe('current')
    expect(row.slice(1).every((s) => s.state === 'pending')).toBe(true)
  })

  it('lights fault injected from the lab’s own row, with its timestamp', () => {
    const row = platformRow({ audit: [FAULT], faultAt: FAULT_AT, ...metric })
    expect(row[1].state).toBe('current')
    expect(row[1].at).toBe(FAULT_AT)
    expect(row[2].state).toBe('pending')
  })

  it('keeps the fault latched when the row has scrolled out of the window', () => {
    // The whole of finding 1: with the traffic loop running, the `chaos.*` row is
    // pushed off the page of audit rows within a minute and the station fell back
    // to `healthy` mid-run. The latch is the page's, and this row honours it.
    const row = platformRow({ audit: [], faultAt: FAULT_AT, ...metric })
    expect(row[1].state).toBe('current')
    expect(row[1].at).toBe(FAULT_AT)
  })

  it('moves to agent acting on the agent’s first call after the fault, and names it', () => {
    const read = toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z')
    const acted = toolRow(
      'agent.tool_invoked',
      'restart_consumer_group',
      '2026-09-19T10:02:00Z',
    )
    const reads = platformRow({ audit: [FAULT, read], faultAt: FAULT_AT, ...metric })
    expect(reads[2].state).toBe('current')
    expect(reads[2].at).toBe('2026-09-19T10:01:00Z')
    expect(reads[2].note).toMatch(/read/i)

    const actions = platformRow({
      audit: [FAULT, read, acted],
      faultAt: FAULT_AT,
      ...metric,
    })
    expect(actions[2].note).toContain('restart_consumer_group')
  })

  it('reaches recovered with the sample’s own time, and keeps the fault passed', () => {
    const row = platformRow({
      audit: [FAULT, toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:02:00Z')],
      faultAt: FAULT_AT,
      recoveredAt: '2026-09-19T10:03:00Z',
      metricKnown: true,
      metricInsideThreshold: true,
      metricBreachedSinceFault: true,
    })
    expect(row[3].state).toBe('current')
    expect(row[3].at).toBe('2026-09-19T10:03:00Z')
    expect(row[1].state).toBe('passed')
    expect(row[2].state).toBe('passed')
  })

  it('measures each passed station against the next one’s clock', () => {
    const acting = toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z')
    const row = platformRow({
      audit: [RESET_FIRST, FAULT, acting],
      faultAt: FAULT_AT,
      ...metric,
    })
    // The boundary is when this world began, so `healthy` has a start to measure from.
    expect(row[0].at).toBe('2026-09-19T09:59:00Z')
    expect(row[0].durationMs).toBe(60_000)
    expect(row[1].durationMs).toBe(60_000)
    expect(row[2].durationMs).toBeNull()
  })

  it('will not claim recovery the metric cannot confirm', () => {
    const row = platformRow({
      audit: [FAULT],
      faultAt: FAULT_AT,
      recoveredAt: null,
      metricKnown: false,
      metricInsideThreshold: true,
      metricBreachedSinceFault: true,
    })
    expect(row[3].state).toBe('pending')
    expect(row[3].at).toBeNull()
  })
})

describe('agentRow — seven stations from the append-only history', () => {
  it('is rendered in full with no run at all, every station pending', () => {
    const row = agentRow(null)
    expect(row.map((s) => s.key)).toEqual([
      'triage',
      'investigating',
      'planning',
      'awaiting_approval',
      'remediating',
      'verifying',
      'terminal',
    ])
    expect(row.every((s) => s.state === 'pending')).toBe(true)
    expect(row[6].label).toBe('resolved | escalated | failed')
  })

  it('stamps each station with its own time and its duration', () => {
    const r: AgentRun = {
      ...run('planning', ['triage', 'investigating']),
      phase_history: [
        { state: 'triage', at: '2026-09-19T10:01:00Z' },
        { state: 'investigating', at: '2026-09-19T10:01:30Z' },
        { state: 'planning', at: '2026-09-19T10:02:30Z' },
      ],
      state: 'planning',
    }
    const row = agentRow(r)
    expect(row[0].at).toBe('2026-09-19T10:01:00Z')
    expect(row[0].durationMs).toBe(30_000)
    expect(row[1].durationMs).toBe(60_000)
    expect(row[1].state).toBe('passed')
    expect(row[2].state).toBe('current')
    expect(row[2].durationMs).toBeNull()
    expect(row[3].state).toBe('pending')
  })

  it('names the terminal it actually reached, and closes it at finished_at', () => {
    const r: AgentRun = {
      ...run('resolved'),
      phase_history: [
        { state: 'verifying', at: '2026-09-19T10:03:00Z' },
        { state: 'resolved', at: '2026-09-19T10:04:00Z' },
      ],
      state: 'resolved',
      finished_at: '2026-09-19T10:04:00Z',
      active: false,
    }
    const row = agentRow(r)
    expect(row[5].durationMs).toBe(60_000)
    expect(row[6].state).toBe('current')
    expect(row[6].label).toBe('resolved')
  })

  it('counts a hand-back rather than hiding it', () => {
    // VERIFYING may hand back to INVESTIGATING, so a station can be entered twice.
    // The duration is the sum of the visits and the station says how many.
    const r: AgentRun = {
      ...run('investigating'),
      phase_history: [
        { state: 'investigating', at: '2026-09-19T10:01:00Z' },
        { state: 'remediating', at: '2026-09-19T10:02:00Z' },
        { state: 'verifying', at: '2026-09-19T10:03:00Z' },
        { state: 'investigating', at: '2026-09-19T10:04:00Z' },
      ],
      state: 'investigating',
    }
    const row = agentRow(r)
    expect(row[1].visits).toBe(2)
    expect(row[1].state).toBe('current')
    // The first visit is closed (60s); the second is still open.
    expect(row[1].durationMs).toBe(60_000)
    expect(row[4].state).toBe('passed')
  })

  it('keeps an unrecognised state visible instead of guessing a station', () => {
    const r = { ...run('investigating'), state: 'meditating' as AgentRunState }
    const row = agentRow(r)
    expect(row.some((s) => s.state === 'current')).toBe(false)
    expect(agentStateLabel(r)).toBe('meditating')
  })
})

describe('metricRecovery — recovery off the platform’s 15-minute samples', () => {
  const T = (min: number, sec = 0) =>
    `2026-09-19T10:${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}Z`

  function samples(...pairs: [number, number][]) {
    return pairs.map(([min, v]) => ({ t: Date.parse(T(min)), v, at: T(min) }))
  }

  it('sees no breach in a world that never broke', () => {
    const reading = metricRecovery(samples([1, 0], [2, 0]), 20, T(0))
    expect(reading.breachedAt).toBeNull()
    expect(reading.recoveredAt).toBeNull()
  })

  it('records the first sample outside the bar as the breach', () => {
    const reading = metricRecovery(samples([1, 0], [2, 42], [3, 44]), 20, T(0))
    expect(reading.breachedAt).toBe(T(2))
    expect(reading.recoveredAt).toBeNull()
  })

  it('does NOT call one sub-threshold sample a recovery', () => {
    // 42 → 0 → 42, which is what an ageing cached sample reads. One zero is not
    // a recovery, and the first take's strip said it was.
    const reading = metricRecovery(samples([1, 42], [2, 0], [3, 42]), 20, T(0))
    expect(reading.recoveredAt).toBeNull()
    expect(reading.insideSince).toBeNull()
  })

  it('calls it recovered on two consecutive samples inside the bar', () => {
    const reading = metricRecovery(samples([1, 42], [2, 0], [3, 0]), 20, T(0))
    expect(reading.recoveredAt).toBe(T(2))
    expect(reading.sustained).toBe(true)
  })

  it('says a recovery is pending while only one sample is back inside', () => {
    const reading = metricRecovery(samples([1, 42], [2, 0]), 20, T(0))
    expect(reading.recoveredAt).toBeNull()
    expect(reading.insideSince).toBe(T(2))
    expect(reading.sustained).toBe(false)
  })

  it('ignores samples older than the fault', () => {
    const reading = metricRecovery(samples([1, 0], [2, 0], [5, 42]), 20, T(4))
    expect(reading.breachedAt).toBe(T(5))
    expect(reading.recoveredAt).toBeNull()
  })
})

describe('chartMarkers — what the chart draws on top of the line', () => {
  const windowStart = Date.parse('2026-09-19T10:00:00Z')
  const windowEnd = Date.parse('2026-09-19T10:15:00Z')

  it('marks the fault, each action and the recovery, oldest first', () => {
    const markers = chartMarkers({
      faultAt: '2026-09-19T10:01:00Z',
      recoveredAt: '2026-09-19T10:06:00Z',
      resetAt: null,
      steps: [
        {
          seq: 1,
          kind: 'read',
          tool: 'get_consumer_lag',
          at: '2026-09-19T10:02:00Z',
        },
        {
          seq: 2,
          kind: 'action',
          tool: 'restart_consumer_group',
          at: '2026-09-19T10:05:00Z',
        },
      ],
      audit: [],
      windowStart,
      windowEnd,
    })
    expect(markers.map((m) => m.kind)).toEqual(['fault', 'action', 'recovery'])
    expect(markers[1].label).toBe('restart_consumer_group')
  })

  it('takes the actions from the audit log when no step was reported', () => {
    const markers = chartMarkers({
      faultAt: '2026-09-19T10:01:00Z',
      recoveredAt: null,
      resetAt: null,
      steps: [],
      audit: [
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:02:00Z'),
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:05:00Z'),
      ],
      windowStart,
      windowEnd,
    })
    expect(markers.filter((m) => m.kind === 'action')).toHaveLength(1)
  })

  it('drops anything outside the window rather than pinning it to the edge', () => {
    const markers = chartMarkers({
      faultAt: '2026-09-19T09:30:00Z',
      recoveredAt: null,
      resetAt: '2026-09-19T09:29:00Z',
      steps: [],
      audit: [],
      windowStart,
      windowEnd,
    })
    expect(markers).toEqual([])
  })
})

describe('runsSinceReset — what the run selector may offer', () => {
  const RESET_AT = '2026-09-19T10:05:00Z'

  it('offers every run of the current take, newest first', () => {
    const a = { ...run('resolved'), id: 'a', started_at: '2026-09-19T10:06:00Z' }
    const b = { ...run('investigating'), id: 'b', started_at: '2026-09-19T10:08:00Z' }
    expect(runsSinceReset([a, b], RESET_AT).map((r) => r.id)).toEqual(['b', 'a'])
  })

  it('keeps a finished run of this take, which the old active-only query dropped', () => {
    // The first take's console asked for `active=true`, so the run disappeared the
    // instant it resolved — taking the briefing card with it.
    const finished = {
      ...run('resolved'),
      id: 'done',
      started_at: '2026-09-19T10:06:00Z',
      finished_at: '2026-09-19T10:09:00Z',
      active: false,
    }
    expect(runsSinceReset([finished], RESET_AT).map((r) => r.id)).toEqual(['done'])
    expect(runSinceReset([finished], RESET_AT)?.id).toBe('done')
  })

  it('drops the previous take’s closed runs', () => {
    const old = {
      ...run('failed'),
      id: 'old',
      started_at: '2026-09-19T10:01:00Z',
      finished_at: '2026-09-19T10:04:00Z',
      active: false,
    }
    expect(runsSinceReset([old], RESET_AT)).toEqual([])
  })
})

describe('selectRun — the run selector’s choice', () => {
  const runs = [
    { ...run('investigating'), id: 'newest', started_at: '2026-09-19T10:08:00Z' },
    { ...run('resolved'), id: 'older', started_at: '2026-09-19T10:06:00Z' },
  ]

  it('defaults to the newest run of the take', () => {
    expect(selectRun(runs, null)?.id).toBe('newest')
  })

  it('honours an explicit ?run= that names a run in the list', () => {
    expect(selectRun(runs, 'older')?.id).toBe('older')
  })

  it('falls back to the newest when ?run= names nothing it has', () => {
    expect(selectRun(runs, 'ghost')?.id).toBe('newest')
  })

  it('has nothing to select from an empty take', () => {
    expect(selectRun([], 'older')).toBeNull()
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// WO-R3-334 — the page reads ONE take, and a take is the span between two
// boundaries.
//
// The third live take was recorded, wound down and the page reloaded two minutes
// later, so the newest `lab.world_reset` row was AFTER the run. Everything that read
// "since the newest boundary" then read a freshly wiped world: the PLATFORM row said
// `healthy · no fault yet` and the header said "no run in this take yet", beside an
// AGENT row that said `escalated`. Both readings were true of different moments.
//
// THE SHAPE OF THAT SCREENSHOT, which most of these tests are built on:
//
//   09:59:00  boundary          ← the take opens
//   10:00:00  chaos.tool_invoked (kill_consumer)
//   10:01:00  the run starts, escalates, finishes
//   10:04:00  boundary          ← the wind-down closes the take
//   10:05:00  lab.probe + another principal's reads (the evaluator's guards)
// ─────────────────────────────────────────────────────────────────────────────

const B1 = '2026-09-19T09:59:00Z'
const B2 = '2026-09-19T10:04:00Z'

/** The third take's rows, as the page sees them on a reload after the wind-down. */
function windDownRows(): AuditLog[] {
  return [
    resetRow(B1),
    toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:00:00Z'),
    toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:30Z'),
    resetRow(B2),
    // After the boundary: the evaluator's principal-guard probes and the world
    // audit, both under the agent's own token on purpose (WO-R3-333).
    auditRow({ action: 'lab.probe', created_at: '2026-09-19T10:05:00Z' }),
    toolRow('agent.tool_invoked', 'mark_dlq_permanent', '2026-09-19T10:05:10Z'),
  ]
}

/** The run of that take: started inside it, finished inside it. */
function windDownRun(): AgentRun {
  return {
    ...run('escalated', ['triage', 'investigating']),
    id: 'run-take-3',
    started_at: '2026-09-19T10:01:00Z',
    finished_at: '2026-09-19T10:03:00Z',
    active: false,
  }
}

describe('takes — the span between two boundaries', () => {
  const rows = windDownRows()

  it('lists every boundary in view, oldest first', () => {
    expect(takeBoundaries(rows)).toEqual([B1, B2])
  })

  it('puts an instant in the take the boundaries around it define', () => {
    expect(takeAt(rows, '2026-09-19T10:01:00Z')).toEqual({ startAt: B1, endAt: B2 })
  })

  it('opens a take with no boundary before it rather than inventing one', () => {
    expect(takeAt(rows, '2026-09-19T09:58:00Z')).toEqual({ startAt: null, endAt: B1 })
  })

  it('reads the take now running as the one after the newest boundary', () => {
    expect(currentTake(rows)).toEqual({ startAt: B2, endAt: null })
    expect(takeAt(rows, '2026-09-19T10:06:00Z')).toEqual({ startAt: B2, endAt: null })
  })

  it('gives a row on a boundary to the take being closed — the reset writes last', () => {
    expect(takeAt(rows, B2)).toEqual({ startAt: B1, endAt: B2 })
  })

  it('keeps the closing boundary row in the take and the opening one out of it', () => {
    const take = { startAt: B1, endAt: B2 }
    const inside = rowsInTake(rows, take).map((r) => r.created_at)
    expect(inside).toContain(B2)
    expect(inside).not.toContain(B1)
    // The ledger asks for both edges, because it draws a divider at each one.
    expect(rowsInTakeWithEdges(rows, take).map((r) => r.created_at)).toContain(B1)
  })

  it('leaves the next take’s rows out — including the lab’s own probes', () => {
    const inside = rowsInTake(rows, { startAt: B1, endAt: B2 })
    expect(inside.some((r) => r.action === 'lab.probe')).toBe(false)
    expect(inside.some((r) => r.created_at === '2026-09-19T10:05:10Z')).toBe(false)
  })

  it('finds the fault of the take, and no fault in the take after it', () => {
    expect(faultInTake(rows, { startAt: B1, endAt: B2 })).toBe('2026-09-19T10:00:00Z')
    expect(faultInTake(rows, { startAt: B2, endAt: null })).toBeNull()
  })

  it('gives every take one key, so a latch cannot cross a boundary', () => {
    expect(takeKey({ startAt: B1, endAt: B2 })).not.toBe(takeKey({ startAt: B2, endAt: null }))
    expect(takeHasEnded({ startAt: B1, endAt: B2 })).toBe(true)
    expect(takeHasEnded({ startAt: B2, endAt: null })).toBe(false)
  })

  it('assigns each run to the take it started in', () => {
    const inside = windDownRun()
    const after = { ...run('investigating'), id: 'next', started_at: '2026-09-19T10:06:00Z' }
    expect(runsInTake([inside, after], { startAt: B1, endAt: B2 }).map((r) => r.id)).toEqual([
      'run-take-3',
    ])
    expect(runsInTake([inside, after], { startAt: B2, endAt: null }).map((r) => r.id)).toEqual([
      'next',
    ])
    expect(takeOfRun(rows, inside)).toEqual({ startAt: B1, endAt: B2 })
  })
})

describe('selectTake — the newest run with a fault in its OWN take', () => {
  const rows = windDownRows()

  it('shows the closed take the run belongs to, not the empty one after it', () => {
    const chosen = selectTake({ runs: [windDownRun()], audit: rows, wanted: null })
    expect(chosen.run?.id).toBe('run-take-3')
    expect(chosen.take).toEqual({ startAt: B1, endAt: B2 })
    expect(chosen.why).toBe('fault')
  })

  it('prefers a run whose take has a fault over a newer run whose take has none', () => {
    const newer = { ...run('investigating'), id: 'quiet', started_at: '2026-09-19T10:06:00Z' }
    const chosen = selectTake({ runs: [windDownRun(), newer], audit: rows, wanted: null })
    expect(chosen.run?.id).toBe('run-take-3')
  })

  it('honours ?run= and reads that run’s take the same way', () => {
    const newer = { ...run('investigating'), id: 'quiet', started_at: '2026-09-19T10:06:00Z' }
    const chosen = selectTake({ runs: [windDownRun(), newer], audit: rows, wanted: 'quiet' })
    expect(chosen.run?.id).toBe('quiet')
    expect(chosen.take).toEqual({ startAt: B2, endAt: null })
    expect(chosen.why).toBe('requested')
  })

  it('falls back to the newest run when no take in view has a fault', () => {
    const quiet = [resetRow(B1), resetRow(B2)]
    const newer = { ...run('investigating'), id: 'quiet', started_at: '2026-09-19T10:06:00Z' }
    const chosen = selectTake({ runs: [windDownRun(), newer], audit: quiet, wanted: null })
    expect(chosen.run?.id).toBe('quiet')
    expect(chosen.why).toBe('newest_run')
  })

  it('shows the take now running when no run has been reported at all', () => {
    const chosen = selectTake({ runs: [], audit: rows, wanted: null })
    expect(chosen.run).toBeNull()
    expect(chosen.take).toEqual({ startAt: B2, endAt: null })
    expect(chosen.why).toBe('current')
  })

  it('offers every run it can see, newest first, whatever take they are in', () => {
    const newer = { ...run('investigating'), id: 'quiet', started_at: '2026-09-19T10:06:00Z' }
    const chosen = selectTake({ runs: [windDownRun(), newer], audit: rows, wanted: null })
    expect(chosen.runs.map((r) => r.id)).toEqual(['quiet', 'run-take-3'])
    expect(chosen.takeRuns.map((r) => r.id)).toEqual(['run-take-3'])
  })
})

describe('platformRow reads the take it is given, not the newest boundary', () => {
  const rows = windDownRows()
  const metricQuiet = {
    metricKnown: true,
    metricInsideThreshold: true,
    metricBreachedSinceFault: false,
  }

  it('lights fault injected for a closed take that a later boundary followed', () => {
    // The screenshot's bug, in one assertion: read "since the newest boundary" this
    // row says `healthy`, and the agent row beside it says `escalated`.
    const stations = platformRow({
      audit: rows,
      take: { startAt: B1, endAt: B2 },
      ...metricQuiet,
    })
    const byKey = Object.fromEntries(stations.map((s) => [s.key, s]))
    expect(byKey.fault_injected.at).toBe('2026-09-19T10:00:00Z')
    expect(byKey.healthy.at).toBe(B1)
    expect(byKey.fault_injected.state).not.toBe('pending')
  })

  it('reads the take after the wind-down as healthy with no fault', () => {
    const stations = platformRow({
      audit: rows,
      take: { startAt: B2, endAt: null },
      ...metricQuiet,
    })
    const byKey = Object.fromEntries(stations.map((s) => [s.key, s]))
    expect(byKey.healthy.state).toBe('current')
    expect(byKey.fault_injected.state).toBe('pending')
  })

  it('does not count another principal’s reads as the agent acting', () => {
    // F3: the demo runner read lag every three seconds under the AGENT's token, and
    // F4: the evaluator's guard probes wrote `mark_dlq_permanent` — a Tier-1 action —
    // after the boundary. Neither is this run's work.
    const withForeign = [
      resetRow(B1),
      toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:00:00Z'),
      auditRow({
        action: 'agent.tool_invoked',
        principal_id: 'runner-sa',
        created_at: '2026-09-19T10:01:00Z',
        extra_data: { tool_name: 'get_consumer_lag', arguments: {} },
      }),
      auditRow({
        action: 'agent.tool_invoked',
        principal_id: 'runner-sa',
        created_at: '2026-09-19T10:02:00Z',
        extra_data: { tool_name: 'mark_dlq_permanent', arguments: {} },
      }),
    ]
    const take = { startAt: B1, endAt: null }
    const mine = platformRow({
      audit: withForeign,
      take,
      runPrincipalId: 'agent-sa',
      ...metricQuiet,
    })
    expect(Object.fromEntries(mine.map((s) => [s.key, s.state])).agent_acting).toBe('pending')

    // With no run selected there is no principal to compare against, and counting
    // every row is the honest reading rather than a guess.
    const anyone = platformRow({ audit: withForeign, take, ...metricQuiet })
    expect(
      Object.fromEntries(anyone.map((s) => [s.key, s.state])).agent_acting,
    ).not.toBe('pending')
  })
})

describe('the agent row says when a late report arrived', () => {
  const runId = 'run-late'
  function reportRow(state: string, at: string): AuditLog {
    return auditRow({
      action: 'agent.run_reported',
      created_at: at,
      extra_data: {
        tool_name: 'report_agent_run',
        arguments: { run_id: runId, state },
      },
    })
  }

  /** The third take's reporter: three events at 10:01:00, all of them arriving at 10:01:41. */
  const burst = [
    reportRow('triage', '2026-09-19T10:01:41Z'),
    reportRow('investigating', '2026-09-19T10:01:41Z'),
  ]
  const late = {
    ...run('investigating', ['triage']),
    id: runId,
    phase_history: [
      { state: 'triage' as AgentRunState, at: '2026-09-19T10:01:00Z' },
      { state: 'investigating' as AgentRunState, at: '2026-09-19T10:01:00Z' },
    ],
  }

  it('reads the arrival of each state off the report rows', () => {
    const arrivals = reportArrivals(burst, runId)
    expect(arrivals.get('triage')).toBe('2026-09-19T10:01:41Z')
    expect(reportArrivals(burst, 'another-run').size).toBe(0)
    expect(reportArrivals(burst, null).size).toBe(0)
  })

  it('takes the earliest row per state — a state reported twice is one station', () => {
    const arrivals = reportArrivals(
      [reportRow('triage', '2026-09-19T10:02:00Z'), reportRow('triage', '2026-09-19T10:01:41Z')],
      runId,
    )
    expect(arrivals.get('triage')).toBe('2026-09-19T10:01:41Z')
  })

  it('labels a station whose report arrived 41 s after the event', () => {
    const stations = agentRow(late, { arrivals: reportArrivals(burst, runId) })
    const byKey = Object.fromEntries(stations.map((s) => [s.key, s]))
    expect(byKey.triage.at).toBe('2026-09-19T10:01:00Z')
    expect(byKey.triage.reportedAt).toBe('2026-09-19T10:01:41Z')
    expect(byKey.investigating.reportedAt).toBe('2026-09-19T10:01:41Z')
  })

  it('says nothing about a report that arrived when it happened', () => {
    const prompt = [reportRow('triage', '2026-09-19T10:01:01Z')]
    const stations = agentRow(late, { arrivals: reportArrivals(prompt, runId) })
    expect(Object.fromEntries(stations.map((s) => [s.key, s])).triage.reportedAt).toBeNull()
  })

  it('has no arrival to show where the audit rows do not carry one', () => {
    const stations = agentRow(late)
    expect(stations.every((s) => s.reportedAt === null)).toBe(true)
  })
})

describe('revealStations — one station at a time, however they arrive', () => {
  const stations = agentRow({
    ...run('remediating', ['triage', 'investigating', 'planning']),
  })

  it('shows every reached station when nothing is held back', () => {
    expect(revealStations(stations, null)).toEqual(stations)
    expect(revealStations(stations, 99)).toEqual(stations)
  })

  it('makes the last revealed station the current one', () => {
    const shown = revealStations(stations, 2)
    const byKey = Object.fromEntries(shown.map((s) => [s.key, s]))
    expect(byKey.triage.state).toBe('passed')
    expect(byKey.investigating.state).toBe('current')
    expect(byKey.planning.state).toBe('pending')
    expect(byKey.remediating.state).toBe('pending')
  })

  it('leaves a held-back station with no timestamp — pending with a time is a contradiction', () => {
    const shown = revealStations(stations, 1)
    const byKey = Object.fromEntries(shown.map((s) => [s.key, s]))
    expect(byKey.planning.at).toBeNull()
    expect(byKey.planning.durationMs).toBeNull()
  })

  it('reveals nothing at zero', () => {
    expect(revealStations(stations, 0).every((s) => s.state === 'pending')).toBe(true)
  })
})

describe('chartMarkers — the set is closed', () => {
  const windowStart = new Date('2026-09-19T09:55:00Z').getTime()
  const windowEnd = new Date('2026-09-19T10:10:00Z').getTime()

  it('marks both boundaries of the take', () => {
    const markers = chartMarkers({
      faultAt: null,
      recoveredAt: null,
      resetAts: [B1, B2],
      steps: [],
      audit: [],
      windowStart,
      windowEnd,
    })
    expect(markers.filter((m) => m.kind === 'reset').map((m) => m.at)).toEqual([B1, B2])
  })

  it('does not mark an action another principal took, nor a lab probe', () => {
    const markers = chartMarkers({
      faultAt: null,
      recoveredAt: null,
      resetAts: [],
      steps: [],
      audit: [
        auditRow({
          action: 'agent.tool_invoked',
          principal_id: 'runner-sa',
          created_at: '2026-09-19T10:01:00Z',
          extra_data: { tool_name: 'mark_dlq_permanent', arguments: {} },
        }),
        auditRow({
          action: 'lab.probe',
          principal_id: 'agent-sa',
          created_at: '2026-09-19T10:02:00Z',
          extra_data: { tool_name: 'mark_dlq_permanent', arguments: {} },
        }),
      ],
      runPrincipalId: 'agent-sa',
      windowStart,
      windowEnd,
    })
    expect(markers.filter((m) => m.kind === 'action')).toEqual([])
  })

  it('still marks the run’s own action from the audit log', () => {
    const markers = chartMarkers({
      faultAt: null,
      recoveredAt: null,
      resetAts: [],
      steps: [],
      audit: [
        auditRow({
          action: 'agent.tool_invoked',
          principal_id: 'agent-sa',
          created_at: '2026-09-19T10:01:00Z',
          extra_data: { tool_name: 'restart_consumer_group', arguments: {} },
        }),
      ],
      runPrincipalId: 'agent-sa',
      windowStart,
      windowEnd,
    })
    expect(markers.map((m) => [m.kind, m.label])).toEqual([['action', 'restart_consumer_group']])
  })
})

describe('chartWindow — the axis is the take', () => {
  const now = new Date('2026-09-19T10:10:00Z').getTime()

  it('starts two minutes before the fault so the climb is readable', () => {
    const span = chartWindow({
      faultAt: '2026-09-19T10:06:00Z',
      takeStartAt: B1,
      takeEndAt: null,
      now,
      windowSeconds: 900,
    })
    expect(span.start).toBe(new Date('2026-09-19T10:04:00Z').getTime())
    expect(span.end).toBe(now)
    expect(span.zoomed).toBe(true)
  })

  it('ends at the boundary on a take that has ended', () => {
    const span = chartWindow({
      faultAt: '2026-09-19T10:00:00Z',
      takeStartAt: B1,
      takeEndAt: B2,
      now,
      windowSeconds: 900,
    })
    expect(span.end).toBe(new Date(B2).getTime())
  })

  it('zooms back out to everything the platform still holds', () => {
    const span = chartWindow({
      faultAt: '2026-09-19T10:06:00Z',
      takeStartAt: B1,
      takeEndAt: null,
      now,
      windowSeconds: 900,
      full: true,
    })
    expect(span.end - span.start).toBe(900_000)
    expect(span.zoomed).toBe(false)
  })

  it('never draws a span narrower than five minutes', () => {
    const span = chartWindow({
      faultAt: '2026-09-19T10:09:50Z',
      takeStartAt: '2026-09-19T10:09:40Z',
      takeEndAt: null,
      now,
      windowSeconds: 900,
    })
    expect(span.end - span.start).toBe(MIN_SPAN_MS)
  })

  it('never claims more history than the platform has', () => {
    const span = chartWindow({
      faultAt: '2026-09-19T09:30:00Z',
      takeStartAt: null,
      takeEndAt: null,
      now,
      windowSeconds: 900,
    })
    expect(span.start).toBe(now - 900_000)
  })

  it('falls back to the platform’s window with no fault and no boundary', () => {
    const span = chartWindow({
      faultAt: null,
      takeStartAt: null,
      takeEndAt: null,
      now,
      windowSeconds: 600,
    })
    expect(span.end - span.start).toBe(600_000)
  })
})

describe('yAxisTicks — round numbers, and zero always drawn', () => {
  it('puts zero and a round top on the axis', () => {
    expect(yAxisTicks(42)).toEqual([0, 13, 25, 38, 50])
    expect(yAxisTicks(0)).toEqual([0, 0.25, 0.5, 0.75, 1])
  })

  it('never prints a fractional count of messages', () => {
    expect(yAxisTicks(30).every((t) => Number.isInteger(t))).toBe(true)
  })
})
