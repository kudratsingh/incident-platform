/**
 * The fifth take's four findings, as rules (WO-R3-341).
 *
 * The owner ran the demo on 2026-09-21 at 11:39 local. The run was right and the
 * screen was not, and every case below is one of the things they saw. The audit rows
 * of that take — all 162 of them, as the platform served them — are in
 * `fixtures/take5-audit-rows.json` and are the fixture for the two cases that are
 * about real data rather than about a shape.
 *
 *  - **F1, item 1.** The finished run disappeared eleven seconds after it resolved:
 *    the runner's wind-down wrote a new boundary and "the take now running" jumped to
 *    the empty take. The rule is now "the newest take that has a fault or a run", and
 *    an empty newer take gets a banner instead of the screen.
 *  - **F2, item 2.** The PLATFORM row flipped paged → agent acting → paged → agent
 *    acting, because the run's principal and the audit rows arrive on different polls.
 *    Stations latch, and "agent acting" comes from the run's own steps.
 *  - **item 3.** The recovery marker belongs on the first sample back inside the bar
 *    AFTER the remediation, not on a dip before it.
 *  - **F5, item 5.** `agent.tool_invoked get_consumer_lag` every three seconds, from
 *    11:38:56 — the demo runner's own baseline polls under the smoke token, drawn as
 *    the agent because with no run selected the page counted every tool row.
 */

import { describe, it, expect } from 'vitest'
import {
  firstActionAt,
  latchStations,
  metricRecovery,
  platformRow,
  selectTake,
  takeHasWork,
} from '../utils/demoPhase'
import type { MetricSample, PlatformStation, Take } from '../utils/demoPhase'
import { buildLedger, ledgerCounts, ledgerExclusions } from '../utils/demoRun'
import type { AgentRun, AgentRunStepRecord, AuditLog } from '../types'

import take5Rows from './fixtures/take5-audit-rows.json'

/** The take the owner recorded, oldest first, exactly as `GET /audit/logs` served it. */
const TAKE5 = take5Rows as unknown as AuditLog[]

/** The run of that take, and the two principals its rows are written under. */
const RUN_SA = '586042d6-f550-4d76-9f4f-a9f291419d1a'
const SMOKE_SA = 'd6bd0ef1-9a9d-490e-aae6-1bdb8868e349'
const TRIAGE_AT = '2026-09-21T11:39:29.000Z'
const OPEN_BOUNDARY = '2026-09-21T11:38:55.500798Z'
const CLOSE_BOUNDARY = '2026-09-21T11:40:30.234236Z'

function take5Run(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: '7b305000-6dd3-52b0-a39c-2c188573da00',
    tenant_id: 't-1',
    alert_id: '323ffb14-9c12-487d-9415-c42b6bb094ad',
    service_account_id: RUN_SA,
    scenario: 'remediate_consumer_lag_success',
    state: 'resolved',
    phase_history: [
      { state: 'triage', at: '2026-09-21T11:39:29.190979Z' },
      { state: 'investigating', at: '2026-09-21T11:39:29.262200Z' },
      { state: 'planning', at: '2026-09-21T11:39:50.847634Z' },
      { state: 'verifying', at: '2026-09-21T11:39:56.089244Z' },
      { state: 'resolved', at: '2026-09-21T11:40:19.336715Z' },
    ],
    current_hypothesis: null,
    last_step: null,
    briefing: null,
    started_at: '2026-09-21T11:39:29.190979Z',
    updated_at: '2026-09-21T11:40:23.844Z',
    finished_at: '2026-09-21T11:40:19.336715Z',
    active: false,
    ...overrides,
  }
}

function rowsBefore(at: string): AuditLog[] {
  return TAKE5.filter((row) => row.created_at < at)
}

function step(
  seq: number,
  kind: string,
  tool: string,
  at: string | null,
): AgentRunStepRecord {
  return { seq, kind, tool, at, arguments: {}, result_excerpt: null, outcome: 'success' }
}

function stationByKey(row: PlatformStation[]): Record<string, PlatformStation> {
  return Object.fromEntries(row.map((s) => [s.key, s]))
}

const METRIC_BREACHED = {
  metricKnown: true,
  metricInsideThreshold: false,
  metricBreachedSinceFault: true,
}

// ─────────────────────────────────────────────────────────────────────────────
// F1 / item 1 — a finished run stays on screen.
// ─────────────────────────────────────────────────────────────────────────────

describe('item 1 — a finished run stays up until the next one starts', () => {
  const finishedRun = take5Run()

  it('holds the finished take when the newer take has neither a fault nor a run', () => {
    const chosen = selectTake({ runs: [finishedRun], audit: TAKE5, wanted: null })
    expect(chosen.why).toBe('held')
    expect(chosen.run?.id).toBe(finishedRun.id)
    expect(chosen.take.startAt).toBe(OPEN_BOUNDARY)
    expect(chosen.take.endAt).toBe(CLOSE_BOUNDARY)
    // What the banner prints: the reset that opened the take being cleaned up.
    expect(chosen.cleaningUpSince).toBe(CLOSE_BOUNDARY)
  })

  it('switches to the newer take the moment the lab injects its fault', () => {
    const nextFault: AuditLog = {
      ...TAKE5[0],
      id: 'next-fault',
      action: 'chaos.tool_invoked',
      principal_id: 'c50a0f1b-07fe-4dc8-85df-0f8e24206b52',
      created_at: '2026-09-21T11:41:00Z',
      extra_data: {
        tool_name: 'kill_consumer',
        arguments: { consumer_group: 'worker-dispatcher' },
        outcome: 'success',
      },
    }
    const chosen = selectTake({
      runs: [finishedRun],
      audit: [...TAKE5, nextFault],
      wanted: null,
    })
    expect(chosen.why).toBe('current_empty')
    expect(chosen.take.startAt).toBe(CLOSE_BOUNDARY)
    expect(chosen.cleaningUpSince).toBeNull()
  })

  it('switches to the newer take the moment a run reports in it', () => {
    const nextRun = take5Run({
      id: 'run-take-6',
      state: 'triage',
      started_at: '2026-09-21T11:41:00Z',
      finished_at: null,
      active: true,
    })
    const chosen = selectTake({
      runs: [finishedRun, nextRun],
      audit: TAKE5,
      wanted: null,
    })
    expect(chosen.why).toBe('current_run')
    expect(chosen.run?.id).toBe('run-take-6')
  })

  it('still lets ?run= pin a take, held or not', () => {
    const chosen = selectTake({
      runs: [take5Run()],
      audit: TAKE5,
      wanted: take5Run().id,
    })
    expect(chosen.why).toBe('requested')
    expect(chosen.take.endAt).toBe(CLOSE_BOUNDARY)
  })

  it('reads a take as having work from a fault alone, or from a run alone', () => {
    const closed: Take = { startAt: OPEN_BOUNDARY, endAt: CLOSE_BOUNDARY }
    const emptied: Take = { startAt: CLOSE_BOUNDARY, endAt: null }
    expect(takeHasWork(closed, TAKE5, [])).toBe(true)
    expect(takeHasWork(emptied, TAKE5, [])).toBe(false)
    expect(takeHasWork(emptied, TAKE5, [take5Run({ started_at: '2026-09-21T11:41:00Z' })])).toBe(
      true,
    )
  })

  it('keeps the held take’s own stations, ledger rows and fault on screen', () => {
    const chosen = selectTake({ runs: [take5Run()], audit: TAKE5, wanted: null })
    const row = stationByKey(
      platformRow({
        audit: TAKE5,
        take: chosen.take,
        runPrincipalId: RUN_SA,
        metricKnown: true,
        metricInsideThreshold: true,
        metricBreachedSinceFault: true,
        recoveredAt: '2026-09-21T11:40:02.350775Z',
      }),
    )
    expect(row.fault_injected.at).toBe('2026-09-21T11:39:06.995490Z')
    expect(row.paged.at).toBe('2026-09-21T11:39:26.675504Z')
    expect(row.recovered.state).toBe('current')
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// F2 / item 2 — stations never move backwards.
// ─────────────────────────────────────────────────────────────────────────────

describe('item 2a — a station, once reached, is latched', () => {
  const faulted = rowsBefore('2026-09-21T11:39:56Z')

  /** One poll of the PLATFORM row, with or without the run's principal in hand. */
  function poll(principal: string | null): PlatformStation[] {
    return platformRow({
      audit: faulted,
      take: { startAt: OPEN_BOUNDARY, endAt: null },
      runPrincipalId: principal,
      runSteps: principal === null ? [] : [step(1, 'read', 'get_consumer_lag', '2026-09-21T11:39:35.693067Z')],
      ...METRIC_BREACHED,
    })
  }

  it('keeps agent acting reached when the next poll cannot see the principal', () => {
    const known = poll(RUN_SA)
    expect(stationByKey(known).agent_acting.state).toBe('current')

    // The flip: the run-detail poll has not answered, so this poll has no principal.
    const unknown = poll(null)
    expect(stationByKey(unknown).agent_acting.state).toBe('pending')

    const latched = latchStations(known, unknown)
    expect(stationByKey(latched).agent_acting.state).toBe('current')
    expect(stationByKey(latched).agent_acting.at).toBe('2026-09-21T11:39:35.693067Z')
    expect(stationByKey(latched).paged.state).toBe('passed')
  })

  it('produces a monotonic sequence over alternating polls', () => {
    const reached = (row: PlatformStation[]) =>
      row.filter((s) => s.state !== 'pending').map((s) => s.key)
    let latched: PlatformStation[] | null = null
    const seen: string[][] = []
    for (const principal of [RUN_SA, null, RUN_SA, null, null, RUN_SA]) {
      latched = latchStations(latched, poll(principal))
      seen.push(reached(latched))
    }
    // Every answer is a superset of the one before it — the whole of F2.
    for (let i = 1; i < seen.length; i += 1) {
      expect(seen[i]).toEqual(expect.arrayContaining(seen[i - 1]))
      expect(seen[i].length).toBeGreaterThanOrEqual(seen[i - 1].length)
    }
    expect(seen[seen.length - 1]).toContain('agent_acting')
  })

  it('adds a station a later poll reaches for the first time', () => {
    const before = poll(RUN_SA)
    const after = platformRow({
      audit: TAKE5,
      take: { startAt: OPEN_BOUNDARY, endAt: null },
      runPrincipalId: RUN_SA,
      metricKnown: true,
      metricInsideThreshold: true,
      metricBreachedSinceFault: true,
      recoveredAt: '2026-09-21T11:40:02.350775Z',
    })
    const latched = stationByKey(latchStations(before, after))
    expect(latched.recovered.state).toBe('current')
    expect(latched.agent_acting.state).toBe('passed')
  })

  it('starts over rather than latching when there is nothing to latch against', () => {
    const fresh = poll(null)
    expect(latchStations(null, fresh)).toEqual(fresh)
  })
})

describe('item 2b — agent acting comes from the run’s own steps', () => {
  const take: Take = { startAt: OPEN_BOUNDARY, endAt: CLOSE_BOUNDARY }

  it('takes the first read/action step’s own time, in seq order', () => {
    const row = stationByKey(
      platformRow({
        audit: TAKE5,
        take,
        runPrincipalId: RUN_SA,
        runSteps: [
          step(2, 'action', 'restart_consumer_group', '2026-09-21T11:39:56.070705Z'),
          step(1, 'read', 'get_consumer_lag', '2026-09-21T11:39:35.693067Z'),
        ],
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.at).toBe('2026-09-21T11:39:35.693067Z')
    expect(row.agent_acting.note).toBe('restart_consumer_group fired after 1 read')
  })

  it('ignores the planner’s own report steps, which make no call', () => {
    const row = stationByKey(
      platformRow({
        audit: TAKE5,
        take,
        runPrincipalId: RUN_SA,
        runSteps: [
          step(1, 'report', 'investigation_planner', '2026-09-21T11:39:30Z'),
          step(2, 'read', 'get_consumer_lag', '2026-09-21T11:39:35.693067Z'),
        ],
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.at).toBe('2026-09-21T11:39:35.693067Z')
    expect(row.agent_acting.note).toBe('1 read, no action yet')
  })

  it('falls back to the audit rows only where the run has reported no step', () => {
    const row = stationByKey(
      platformRow({ audit: TAKE5, take, runPrincipalId: RUN_SA, runSteps: [], ...METRIC_BREACHED }),
    )
    expect(row.agent_acting.at).toBe('2026-09-21T11:39:35.724821Z')
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// item 3 — the recovery marker follows the action.
// ─────────────────────────────────────────────────────────────────────────────

describe('item 3 — the R marker is the first sample inside the bar after the action', () => {
  /** The take's own shape: a climb, the rate-limited plateau at 28, then the drain. */
  function samples(pairs: [string, number][]): MetricSample[] {
    return pairs.map(([at, v]) => ({ at, v, t: new Date(at).getTime() }))
  }
  const FAULT_AT = '2026-09-21T11:39:06.995490Z'
  const ACTION_AT = '2026-09-21T11:39:56.054874Z'

  it('does not read a dip before the remediation as the recovery', () => {
    const series = samples([
      ['2026-09-21T11:39:10Z', 5],
      ['2026-09-21T11:39:20Z', 24],
      // A pair of readings back inside the bar, before the agent had acted at all.
      ['2026-09-21T11:39:30Z', 18],
      ['2026-09-21T11:39:40Z', 19],
      ['2026-09-21T11:39:50Z', 28],
      ['2026-09-21T11:40:02Z', 18],
      ['2026-09-21T11:40:12Z', 5],
    ])
    expect(metricRecovery(series, 20, FAULT_AT, 2, ACTION_AT).recoveredAt).toBe(
      '2026-09-21T11:40:02Z',
    )
    // Without the action it anchors on the dip, which is what the fifth take drew.
    expect(metricRecovery(series, 20, FAULT_AT, 2).recoveredAt).toBe('2026-09-21T11:39:30Z')
  })

  it('keeps the breach measured from the fault, not from the action', () => {
    const reading = metricRecovery(
      samples([
        ['2026-09-21T11:39:20Z', 24],
        ['2026-09-21T11:40:02Z', 18],
        ['2026-09-21T11:40:12Z', 5],
      ]),
      20,
      FAULT_AT,
      2,
      ACTION_AT,
    )
    expect(reading.breachedAt).toBe('2026-09-21T11:39:20Z')
    expect(reading.sustained).toBe(true)
  })

  it('reads the action off the run’s own step, or off its audit row', () => {
    expect(
      firstActionAt({
        steps: [step(6, 'action', 'restart_consumer_group', '2026-09-21T11:39:56.070705Z')],
        audit: TAKE5,
        runPrincipalId: RUN_SA,
      }),
    ).toBe('2026-09-21T11:39:56.070705Z')
    expect(firstActionAt({ steps: [], audit: TAKE5, runPrincipalId: RUN_SA })).toBe(
      '2026-09-21T11:39:56.054874Z',
    )
    // And nothing at all where no run is selected: the row is somebody's, not the run's.
    expect(firstActionAt({ steps: [], audit: TAKE5 })).toBeNull()
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// F5 / item 5 — with no run selected, no tool row is the agent's.
// ─────────────────────────────────────────────────────────────────────────────

describe('item 5 — the runner’s own polls are not the agent acting', () => {
  const beforeTriage = rowsBefore(TRIAGE_AT)

  it('is the state the owner saw: 40 smoke-principal lag reads before the fault', () => {
    const reads = beforeTriage.filter(
      (r) => r.action === 'agent.tool_invoked' && r.principal_id === SMOKE_SA,
    )
    expect(reads.length).toBeGreaterThan(30)
    expect(beforeTriage.some((r) => r.principal_id === RUN_SA)).toBe(false)
  })

  it('leaves the ledger with the reset, the fault and the page — and nothing else', () => {
    const ledger = buildLedger({ steps: [], audit: beforeTriage })
    expect(ledger.map((e) => e.kind)).toEqual(['alert', 'lab', 'reset'])
    expect(ledger.some((e) => e.kind === 'agent_audit')).toBe(false)
  })

  it('counts every hidden read so the page can say how many', () => {
    const hidden = ledgerExclusions({ audit: beforeTriage })
    expect(hidden.otherPrincipal).toBe(
      beforeTriage.filter((r) => r.action === 'agent.tool_invoked').length,
    )
    expect(hidden.labProbe).toBe(
      beforeTriage.filter((r) => r.action === 'lab.probe').length,
    )
    expect(ledgerCounts({ steps: [], audit: beforeTriage }).auditCalls).toBe(0)
  })

  it('will not light agent acting with no run, however many rows there are', () => {
    const row = stationByKey(
      platformRow({
        audit: beforeTriage,
        take: { startAt: OPEN_BOUNDARY, endAt: null },
        ...METRIC_BREACHED,
      }),
    )
    expect(row.fault_injected.state).toBe('passed')
    expect(row.paged.state).toBe('current')
    expect(row.agent_acting.state).toBe('pending')
  })

  it('shows them again the moment the run’s own principal is known', () => {
    const row = stationByKey(
      platformRow({
        audit: TAKE5,
        take: { startAt: OPEN_BOUNDARY, endAt: CLOSE_BOUNDARY },
        runPrincipalId: RUN_SA,
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.state).not.toBe('pending')
    expect(row.agent_acting.note).toContain('restart_consumer_group')
  })
})
