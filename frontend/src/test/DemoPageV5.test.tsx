/**
 * The `/demo` page against the fifth take's own audit rows (WO-R3-341).
 *
 * The owner ran the demo on 2026-09-21 at 11:39 local, the run was right, and three
 * things on the screen were wrong. This file drives the whole page from the 162 rows
 * the platform recorded that minute (`fixtures/take5-audit-rows.json`) rather than
 * from a shape invented here, because two of the three faults were only visible
 * against real traffic:
 *
 *  - **F1 (item 1)** the finished run left the screen eleven seconds after it resolved,
 *    when the wind-down's reset opened an empty take. It now stays, under a banner.
 *  - **F2 (item 2)** the PLATFORM row flipped between `paged` and `agent acting` as the
 *    run-detail poll and the audit poll answered at different times. Stations latch.
 *  - **F5 (item 5)** 40 `agent.tool_invoked get_consumer_lag` rows, from 11:38:56 —
 *    the demo runner's own baseline polls under the smoke token — were drawn as the
 *    agent's, so the agent appeared to act before the lab had injected anything.
 */

import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import DemoPage from '../pages/DemoPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import take5Rows from './fixtures/take5-audit-rows.json'
import type { AgentRun, AgentRunStepRecord, AuditLog } from '../types'

vi.mock('../api/admin', () => ({
  adminApi: {
    listAgentRuns: vi.fn(),
    getAgentRun: vi.fn(),
    agentRunSteps: vi.fn(),
    consumerLag: vi.fn(),
    dlqStats: vi.fn(),
    listJobs: vi.fn(),
    listAuditLogs: vi.fn(),
    circuitBreakers: vi.fn(),
    listAlerts: vi.fn(),
  },
}))

vi.mock('../hooks/useAuth', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({
    user: {
      id: 'u-1',
      tenant_id: 't-1',
      tenant_slug: 'demo',
      email: 'agent-demo@example.com',
      role: 'admin',
      is_active: true,
      is_platform_admin: true,
      created_at: '2026-01-01T00:00:00Z',
    },
    loading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  }),
}))

const listAgentRuns = vi.mocked(adminApi.listAgentRuns)
const getAgentRun = vi.mocked(adminApi.getAgentRun)
const agentRunSteps = vi.mocked(adminApi.agentRunSteps)
const listAuditLogs = vi.mocked(adminApi.listAuditLogs)

const TAKE5 = take5Rows as unknown as AuditLog[]

const RUN_SA = '586042d6-f550-4d76-9f4f-a9f291419d1a'
const RUN_ID = '7b305000-6dd3-52b0-a39c-2c188573da00'
/** Strictly before the first thing the agent's own principal did, at 11:39:29.153. */
const TRIAGE_AT = '2026-09-21T11:39:29.000Z'
const CLOSE_BOUNDARY = '2026-09-21T11:40:30.234236Z'

function rowsBefore(at: string): AuditLog[] {
  return TAKE5.filter((row) => row.created_at < at)
}

/** The run the owner watched, as `GET /admin/agent-runs/<id>` served it at the end. */
function take5Run(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: RUN_ID,
    tenant_id: 't-1',
    alert_id: '323ffb14-9c12-487d-9415-c42b6bb094ad',
    service_account_id: RUN_SA,
    scenario: 'remediate_consumer_lag_success',
    state: 'resolved',
    phase_history: [
      { state: 'triage', at: '2026-09-21T11:39:29.190979Z' },
      { state: 'investigating', at: '2026-09-21T11:39:29.262200Z' },
      { state: 'planning', at: '2026-09-21T11:39:50.847634Z' },
      { state: 'remediating', at: '2026-09-21T11:39:56.027963Z' },
      { state: 'verifying', at: '2026-09-21T11:39:56.089244Z' },
      { state: 'resolved', at: '2026-09-21T11:40:19.336715Z' },
    ],
    current_hypothesis: null,
    last_step: null,
    briefing: {
      incident_id: 'inc-5',
      final_state: 'resolved',
      alert_summary: 'worker-dispatcher is 24 messages behind (threshold 20)',
      findings: 'restart_consumer_group cleared the kill flag and the group drained.',
      recommendation: 'none — the world is back inside its bar.',
    },
    started_at: '2026-09-21T11:39:29.190979Z',
    updated_at: '2026-09-21T11:40:23.844Z',
    finished_at: '2026-09-21T11:40:19.336715Z',
    active: false,
    ...overrides,
  }
}

function page<T>(items: T[], pageSize = 100) {
  return { items, total: items.length, page: 1, page_size: pageSize, has_next: false }
}

interface Fixture {
  runs?: AgentRun[]
  steps?: AgentRunStepRecord[]
  audit?: AuditLog[]
  /** Called per audit request instead of `audit`, for the alternating-poll case. */
  auditPerCall?: (call: number) => AuditLog[]
}

function stub(f: Fixture = {}) {
  const runs = f.runs ?? []
  listAgentRuns.mockResolvedValue(page(runs, 50))
  getAgentRun.mockResolvedValue((runs[0] ?? null) as AgentRun)
  const steps = f.steps ?? []
  agentRunSteps.mockResolvedValue({
    run_id: runs[0]?.id ?? RUN_ID,
    state: runs[0]?.state ?? 'resolved',
    finished_at: runs[0]?.finished_at ?? null,
    steps: [...steps].sort((a, b) => a.seq - b.seq),
    returned: steps.length,
    total: steps.length,
    steps_dropped: 0,
    after_seq: null,
    next_after_seq: steps.length === 0 ? null : Math.max(...steps.map((s) => s.seq)),
  })
  vi.mocked(adminApi.consumerLag).mockResolvedValue({
    measured_at: '2026-09-21T11:40:20Z',
    total: 1,
    live_group: 'worker-dispatcher',
    sample_window_seconds: 900,
    sample_interval_seconds: 5,
    groups: [
      {
        consumer_group: 'worker-dispatcher',
        lag: 0,
        lag_known: true,
        source: 'live',
        lag_unknown_reason: null,
        measured_at: '2026-09-21T11:40:20Z',
        age_seconds: 3,
        recent_samples: [],
      },
    ],
  })
  vi.mocked(adminApi.dlqStats).mockResolvedValue({ total: 0, by_type: {} })
  vi.mocked(adminApi.listJobs).mockResolvedValue(page([], 20))
  let auditCalls = 0
  listAuditLogs.mockImplementation((params = {}) => {
    if (params.action_prefix === 'event.') return Promise.resolve(page([]))
    auditCalls += 1
    const rows = f.auditPerCall ? f.auditPerCall(auditCalls) : (f.audit ?? [])
    return Promise.resolve({
      items: rows,
      total: rows.length,
      page: params.page ?? 1,
      page_size: 100,
      has_next: false,
    })
  })
  vi.mocked(adminApi.circuitBreakers).mockResolvedValue({
    measured_at: '2026-09-21T11:40:20Z',
    breakers: [],
    total: 0,
    unknown_reason: null,
  })
  vi.mocked(adminApi.listAlerts).mockResolvedValue(page([], 50))
}

function renderDemo(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/demo${search}`]}>
      <ToastProvider>
        <DemoPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

function stationState(key: string): string | undefined {
  return screen.getByTestId(`station-${key}`).dataset.state
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

// ─────────────────────────────────────────────────────────────────────────────
// F1 / item 1 — the finished run stays up, with a banner over it.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage — a finished run stays on screen while the world is cleaned up', () => {
  it('keeps the run, its stations, its ledger and its briefing, under a banner', async () => {
    stub({ runs: [take5Run()], audit: TAKE5 })
    renderDemo()
    await screen.findByTestId('phase-row-platform')

    await waitFor(() => {
      expect(screen.getByTestId('reset-banner')).toBeTruthy()
    })
    const banner = screen.getByTestId('reset-banner').textContent ?? ''
    expect(banner).toMatch(/world reset at/)
    expect(banner).toMatch(/cleaning up, waiting for the next run/)
    expect(banner).toMatch(
      new RegExp(
        new Date(CLOSE_BOUNDARY)
          .toLocaleTimeString(undefined, {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
          })
          .replace(/[.*+?^${}()|[\]\\]/g, '\\$&'),
      ),
    )

    // The run itself is still the one on screen: the whole of F1.
    expect((screen.getByTestId('run-selector') as HTMLSelectElement).value).toBe(RUN_ID)
    expect(stationState('fault_injected')).not.toBe('pending')
    expect(stationState('paged')).not.toBe('pending')
    expect(stationState('terminal')).not.toBe('pending')
    expect(screen.getByTestId('briefing-card')).toBeTruthy()
    expect(screen.getByTestId('fault-clock').textContent).toMatch(/T\+/)
    expect(screen.getByTestId('take-label').textContent).toMatch(/held while the world/)
    expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
  })

  it('drops the banner and moves on when the next take gets its fault', async () => {
    vi.useFakeTimers()
    const nextFault: AuditLog = {
      ...TAKE5[0],
      id: 'next-fault',
      action: 'chaos.tool_invoked',
      created_at: '2026-09-21T11:41:00Z',
      extra_data: {
        tool_name: 'kill_consumer',
        arguments: { consumer_group: 'worker-dispatcher' },
        outcome: 'success',
      },
    }
    stub({
      runs: [take5Run()],
      auditPerCall: (call) => (call <= 1 ? TAKE5 : [...TAKE5, nextFault]),
    })
    renderDemo()
    await vi.waitFor(() => {
      expect(screen.getByTestId('reset-banner')).toBeTruthy()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100)
    })
    await vi.waitFor(() => {
      expect(screen.queryByTestId('reset-banner')).toBeNull()
    })
    // The fresh take, at zero: its own fault, and no run yet.
    expect(screen.getByTestId('take-label').textContent).toMatch(/waiting for this take/)
    expect(stationState('fault_injected')).not.toBe('pending')
    expect(stationState('agent_acting')).toBe('pending')
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// F5 / item 5 — the runner's own polls are not the agent's.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage — no run selected means no row is the agent’s', () => {
  const beforeTriage = rowsBefore(TRIAGE_AT)

  it('shows only the reset, the fault and the page before the run starts', async () => {
    stub({ audit: beforeTriage })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-reset-divider').length).toBe(1)
    })
    const ledger = screen.getByTestId('action-ledger')
    // Two rows plus the boundary divider: the lab's kill, and the platform's page.
    expect(screen.getAllByTestId('ledger-entry')).toHaveLength(2)
    expect(ledger.textContent).toMatch(/kill_consumer/)
    expect(ledger.textContent).toMatch(/consumer_stalled/)
    // The 40 rows the owner saw as the agent's, all of them the demo runner's.
    expect(ledger.textContent).not.toMatch(/get_consumer_lag/)
    expect(screen.getByTestId('ledger-hidden-reads').textContent).toMatch(
      /no run is selected/,
    )
  })

  it('cannot light agent acting before a run exists', async () => {
    stub({ audit: beforeTriage })
    renderDemo()
    await screen.findByTestId('phase-row-platform')
    await waitFor(() => {
      expect(stationState('paged')).toBe('current')
    })
    expect(stationState('agent_acting')).toBe('pending')
    expect(screen.getByTestId('ledger-counts').textContent).toMatch(
      /0 steps reported · 0 calls/,
    )
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// F2 / item 2 — the stations only ever move forwards.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage — the stations never move backwards', () => {
  it('holds agent acting through polls that cannot see the run’s own calls', async () => {
    vi.useFakeTimers()
    const upToVerify = TAKE5.filter((r) => r.created_at < '2026-09-21T11:40:00Z')
    // Alternating polls, the mechanism of F2: one answer carries the run's own rows,
    // the next does not (the traffic loop pushes them off a page of 100).
    const withoutRunRows = upToVerify.filter((r) => r.principal_id !== RUN_SA)
    stub({
      runs: [take5Run({ state: 'verifying', finished_at: null, active: true })],
      auditPerCall: (call) => (call % 2 === 1 ? upToVerify : withoutRunRows),
    })
    renderDemo()
    await vi.waitFor(() => {
      expect(stationState('agent_acting')).toBe('current')
    })

    const seen: (string | undefined)[] = []
    for (let i = 0; i < 5; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100)
      })
      seen.push(stationState('agent_acting'))
      expect(stationState('fault_injected')).not.toBe('pending')
      expect(stationState('paged')).not.toBe('pending')
    }
    expect(seen.every((state) => state !== 'pending')).toBe(true)
  })
})
