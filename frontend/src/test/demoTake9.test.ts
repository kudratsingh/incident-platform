/**
 * The PLATFORM row's `agent acting` lights when the run starts, on the ninth take's record (WO-R3-358).
 * Owner: "once triage starts it should move to agent acting". The run and the take's audit rows are fixtures.
 */

import { describe, it, expect } from 'vitest'
import { firstActionAt, latchStations, platformRow, stepPoints } from '../utils/demoPhase'
import type { MetricSample, PlatformStation, Take } from '../utils/demoPhase'
import type { AgentRun, AuditLog } from '../types'

import take9Json from './fixtures/take9-run.json'
import take9Rows from './fixtures/take9-audit-rows.json'

const TAKE9 = take9Json as unknown as AgentRun
const ROWS = take9Rows as unknown as AuditLog[]
const STEPS = TAKE9.steps ?? []
const RUN_SA = TAKE9.service_account_id
const TAKE: Take = { startAt: '2026-09-27T11:12:33.686951Z', endAt: null }

const TRIAGE_AT = '2026-09-27T11:13:07.740722+00:00'
const FIRST_READ_STEP_AT = '2026-09-27T11:13:14.382027Z'
const FIRST_READ_ROW_AT = '2026-09-27T11:13:14.372078Z'
const PAGED_AT = '2026-09-27T11:13:04.217095Z'
// The alert.resolved row stands in for the recovery sample; only the durations use it.
const RECOVERED_AT = '2026-09-27T11:13:45.278494Z'

const METRIC_BREACHED = {
  metricKnown: true,
  metricInsideThreshold: false,
  metricBreachedSinceFault: true,
}

/** Local HH:MM:SS, the clock the station's note is written in. */
function clock(iso: string): string {
  const d = new Date(iso)
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')
}

function byKey(row: PlatformStation[]): Record<string, PlatformStation> {
  return Object.fromEntries(row.map((s) => [s.key, s]))
}

function rowsBefore(at: string): AuditLog[] {
  return ROWS.filter((r) => r.created_at < at)
}

function stepsBefore(at: string) {
  return STEPS.filter((s) => s.at !== null && s.at < at)
}

function historyBefore(at: string) {
  return TAKE9.phase_history.filter((p) => new Date(p.at) < new Date(at))
}

describe('agent acting starts with the run (item 1)', () => {
  it('lights at triage, 11:13:07.740, not at the first read 11:13:14', () => {
    const row = byKey(
      platformRow({
        audit: ROWS,
        take: TAKE,
        runPrincipalId: RUN_SA,
        runSteps: STEPS,
        runPhaseHistory: TAKE9.phase_history,
        recoveredAt: RECOVERED_AT,
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.at).toBe(TRIAGE_AT)
    expect(row.agent_acting.note).toBe(
      `run started ${clock(TRIAGE_AT)} · first read +6.6 s · restart_consumer_group fired after 2 reads`,
    )
  })

  it('reads "run started · no read yet" before the run has made a call', () => {
    const at = '2026-09-27T11:13:10Z'
    const row = byKey(
      platformRow({
        audit: rowsBefore(at),
        take: TAKE,
        runPrincipalId: RUN_SA,
        runSteps: stepsBefore(at),
        runPhaseHistory: historyBefore(at),
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.state).toBe('current')
    expect(row.agent_acting.at).toBe(TRIAGE_AT)
    expect(row.agent_acting.note).toBe('run started · no read yet')
  })

  it('adds the first read, then the action, as they happen', () => {
    const at = '2026-09-27T11:13:30Z'
    const row = byKey(
      platformRow({
        audit: rowsBefore(at),
        take: TAKE,
        runPrincipalId: RUN_SA,
        runSteps: stepsBefore(at),
        runPhaseHistory: historyBefore(at),
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.note).toBe(`run started ${clock(TRIAGE_AT)} · first read +6.6 s`)
  })

  it('keeps today’s rule when the run record is absent', () => {
    const withSteps = byKey(
      platformRow({ audit: ROWS, take: TAKE, runPrincipalId: RUN_SA, runSteps: STEPS, ...METRIC_BREACHED }),
    )
    expect(withSteps.agent_acting.at).toBe(FIRST_READ_STEP_AT)
    expect(withSteps.agent_acting.note).toBe('restart_consumer_group fired after 2 reads')
    const auditOnly = byKey(
      platformRow({ audit: ROWS, take: TAKE, runPrincipalId: RUN_SA, ...METRIC_BREACHED }),
    )
    expect(auditOnly.agent_acting.at).toBe(FIRST_READ_ROW_AT)
  })

  it('ignores a run start from before the fault, which is not this incident’s', () => {
    const row = byKey(
      platformRow({
        audit: ROWS,
        take: TAKE,
        runPrincipalId: RUN_SA,
        runSteps: STEPS,
        runPhaseHistory: [{ state: 'triage', at: '2026-09-27T11:12:40Z' }],
        ...METRIC_BREACHED,
      }),
    )
    expect(row.agent_acting.at).toBe(FIRST_READ_STEP_AT)
  })
})

describe('only the start moves (item 2)', () => {
  const input = {
    audit: ROWS,
    take: TAKE,
    runPrincipalId: RUN_SA,
    runSteps: STEPS,
    recoveredAt: RECOVERED_AT,
    ...METRIC_BREACHED,
    metricInsideThreshold: true,
  }

  it('closes the gap after paged to the time triage took to start', () => {
    const row = byKey(platformRow({ ...input, runPhaseHistory: TAKE9.phase_history }))
    expect(row.paged.at).toBe(PAGED_AT)
    expect(row.paged.durationMs).toBe(new Date(TRIAGE_AT).getTime() - new Date(PAGED_AT).getTime())
  })

  it('keeps the agent-acting duration and the chart’s action marker as they were', () => {
    const before = byKey(platformRow(input))
    const after = byKey(platformRow({ ...input, runPhaseHistory: TAKE9.phase_history }))
    expect(after.agent_acting.durationMs).toBe(before.agent_acting.durationMs)
    expect(after.agent_acting.durationMs).toBe(
      new Date(RECOVERED_AT).getTime() - new Date(FIRST_READ_STEP_AT).getTime(),
    )
    expect(firstActionAt({ steps: STEPS, audit: ROWS, runPrincipalId: RUN_SA })).toBe(
      '2026-09-27T11:13:38.932224Z',
    )
  })
})

describe('the latch is unchanged (WO-R3-341)', () => {
  it('holds agent acting at triage through a poll that has no run record', () => {
    const withRun = platformRow({
      audit: ROWS,
      take: TAKE,
      runPrincipalId: RUN_SA,
      runSteps: STEPS,
      runPhaseHistory: TAKE9.phase_history,
      ...METRIC_BREACHED,
    })
    const withoutRun = platformRow({ audit: ROWS, take: TAKE, runPrincipalId: null, ...METRIC_BREACHED })
    expect(byKey(withoutRun).agent_acting.state).toBe('pending')
    const latched = byKey(latchStations(withRun, withoutRun))
    expect(latched.agent_acting.state).toBe('current')
    expect(latched.agent_acting.at).toBe(TRIAGE_AT)
  })
})

describe('the chart draws each sample until the next one (item 4)', () => {
  const sample = (at: string, v: number): MetricSample => ({ t: new Date(at).getTime(), v, at })
  // Take 9's lag around the action, as the platform measured it.
  const SAMPLES = [
    sample('2026-09-27T11:13:34.670Z', 61),
    sample('2026-09-27T11:13:40.110Z', 23),
    sample('2026-09-27T11:13:45.280Z', 0),
  ]
  const ACTION_T = new Date('2026-09-27T11:13:38.918Z').getTime()

  /** The drawn line's value at time `t`, read off its segments. */
  function lineAt(points: { t: number; v: number }[], t: number): number {
    const i = points.findIndex((p, k) => k > 0 && points[k - 1].t <= t && t <= p.t)
    const [a, b] = [points[i - 1], points[i]]
    return b.t === a.t ? a.v : a.v + ((t - a.t) / (b.t - a.t)) * (b.v - a.v)
  }

  it('holds 61 at the action, not a value interpolated towards 23', () => {
    expect(lineAt(stepPoints(SAMPLES), ACTION_T)).toBe(61)
  })

  it('steps down at the sample that saw the change, in time order whatever the input order', () => {
    expect(stepPoints([...SAMPLES].reverse()).map((p) => p.v)).toEqual([61, 61, 23, 23, 0])
  })
})
