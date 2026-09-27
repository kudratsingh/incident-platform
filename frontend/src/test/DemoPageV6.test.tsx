/**
 * The `/demo` page against the seventh take's own run record (WO-R3-354).
 *
 * `fixtures/take7-run.json` is `GET /admin/agent-runs/8ef22e0d…` as the stack served it
 * after the owner's seventh take; `take7-prior-run.json` is the rehearsal run before it.
 */

import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import DemoPage from '../pages/DemoPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import take7Json from './fixtures/take7-run.json'
import priorJson from './fixtures/take7-prior-run.json'
import type { AgentRun, AgentRunStepsResponse } from '../types'

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

const TAKE7 = take7Json as unknown as AgentRun
const PRIOR = priorJson as unknown as AgentRun

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** The take-7 run as it stood mid-verify: the read after the action is in, the judge is not. */
function take7MidVerify(): AgentRun {
  return {
    ...TAKE7,
    state: 'verifying',
    phase_history: TAKE7.phase_history.filter((p) => p.state !== 'resolved'),
    steps: (TAKE7.steps ?? []).filter((s) => s.seq <= 5),
    verification: null,
    verifications: [],
    briefing: null,
    finished_at: null,
    active: true,
  }
}

function page<T>(items: T[], pageSize = 100) {
  return { items, total: items.length, page: 1, page_size: pageSize, has_next: false }
}

function tailOf(run: AgentRun): AgentRunStepsResponse {
  const steps = [...(run.steps ?? [])].sort((a, b) => a.seq - b.seq)
  return {
    run_id: run.id,
    state: run.state,
    finished_at: run.finished_at,
    steps,
    returned: steps.length,
    total: steps.length,
    steps_dropped: 0,
    after_seq: null,
    next_after_seq: steps.length === 0 ? null : steps[steps.length - 1].seq,
  }
}

/** `runsPerCall` answers the run listing per poll, so a test can bring a new run in. */
function stub(runsPerCall: (call: number) => AgentRun[]) {
  let listCalls = 0
  const known = new Map<string, AgentRun>()
  vi.mocked(adminApi.listAgentRuns).mockImplementation(() => {
    listCalls += 1
    const runs = runsPerCall(listCalls)
    for (const r of runs) known.set(r.id, r)
    return Promise.resolve(page(runs, 50))
  })
  vi.mocked(adminApi.getAgentRun).mockImplementation((id: string) =>
    Promise.resolve(known.get(id) as AgentRun),
  )
  vi.mocked(adminApi.agentRunSteps).mockImplementation((id: string) =>
    Promise.resolve(tailOf(known.get(id) as AgentRun)),
  )
  vi.mocked(adminApi.consumerLag).mockResolvedValue({
    measured_at: '2026-09-27T08:52:20Z',
    total: 1,
    live_group: 'worker-dispatcher',
    sample_window_seconds: 900,
    sample_interval_seconds: 5,
    groups: [],
  })
  vi.mocked(adminApi.dlqStats).mockResolvedValue({ total: 0, by_type: {} })
  vi.mocked(adminApi.listJobs).mockResolvedValue(page([], 20))
  vi.mocked(adminApi.listAuditLogs).mockResolvedValue(page([]))
  vi.mocked(adminApi.circuitBreakers).mockResolvedValue({
    measured_at: '2026-09-27T08:52:20Z',
    breakers: [],
    total: 0,
    unknown_reason: null,
  })
  vi.mocked(adminApi.listAlerts).mockResolvedValue(page([], 50))
}

function renderDemo() {
  return render(
    <MemoryRouter initialEntries={['/demo']}>
      <ToastProvider>
        <DemoPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

/** The ledger's rows and dividers in the order they are drawn, one label each. */
function ledgerOrder(): string[] {
  const ledger = screen.getByTestId('action-ledger')
  return [
    ...ledger.querySelectorAll(
      '[data-testid="ledger-phase-divider"], [data-testid="ledger-entry"]',
    ),
  ].map((el) => {
    const node = el as HTMLElement
    if (node.dataset.testid === 'ledger-phase-divider') return `== ${node.dataset.state ?? ''}`
    return `${node.dataset.kind ?? ''}:${node.dataset.seq ?? ''}`
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

// ─────────────────────────────────────────────────────────────────────────────
// Item 1 — transitions are rows in the ledger.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage v6 — every transition is a divider in the ledger', () => {
  it('draws one divider per phase_history entry, newest first, each over its own steps', async () => {
    stub(() => [TAKE7])
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry')).toHaveLength(6)
    })
    expect(ledgerOrder()).toEqual([
      '== resolved',
      '== verifying',
      'think:6',
      'read:5',
      '== remediating',
      'action:4',
      '== planning',
      '== investigating',
      'think:3',
      'read:2',
      'think:1',
      '== triage',
    ])
  })

  it('labels a divider with its state, its time and a plain sentence of what ends it', async () => {
    stub(() => [TAKE7])
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-phase-divider')).toHaveLength(6)
    })
    const investigating = screen
      .getAllByTestId('ledger-phase-divider')
      .find((d) => d.dataset.state === 'investigating') as HTMLElement
    expect(investigating.textContent).toContain(
      `INVESTIGATING · ${clock('2026-09-27T08:51:45.733669+00:00')}`,
    )
    expect(investigating.textContent).toContain(
      'reading the alerted subject and ranking causes; ends when the planner chooses remediate or stop',
    )
    // A finished run pins nothing: there is no "now".
    expect(screen.queryByTestId('ledger-phase-now')).toBeNull()
  })

  it('pins the current state at the top while the run is live, with its exit condition', async () => {
    stub(() => [take7MidVerify()])
    renderDemo()
    const now = await screen.findByTestId('ledger-phase-now')
    expect(now.textContent).toBe(
      'now: VERIFYING — waiting for a reading taken after the action that is inside the threshold',
    )
    const dividers = screen.getAllByTestId('ledger-phase-divider')
    expect(dividers[0].dataset.state).toBe('verifying')
    expect(dividers[0].dataset.pinned).toBe('true')
    expect(dividers.slice(1).every((d) => d.dataset.pinned !== 'true')).toBe(true)
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// Item 2 — the briefing card on a RESOLVED run says what was done.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage v6 — the briefing card of a resolved run', () => {
  it('names the action, that it was accepted, and the verdict with its attempt', async () => {
    stub(() => [TAKE7])
    renderDemo()
    const card = await screen.findByTestId('briefing-card')
    await waitFor(() => {
      expect(screen.getByTestId('briefing-action').textContent).toMatch(/accepted/)
    })
    expect(card.textContent).not.toMatch(/escalated without acting/)
    const action = screen.getByTestId('briefing-action').textContent ?? ''
    expect(action).toContain('restart_consumer_group')
    expect(action).toContain('consumer_group=worker-dispatcher')
    expect(action).toContain('accepted')
    expect(screen.getByTestId('briefing-verify-verdict').textContent).toMatch(
      /verified · attempt 1 of 6/,
    )
  })

  it('says why there is no attribution in ADR 0071’s words', async () => {
    stub(() => [TAKE7])
    renderDemo()
    await screen.findByTestId('briefing-card')
    expect(screen.getByTestId('briefing-attribution').textContent).toMatch(
      /no reading taken after the action showed the fault gone/,
    )
  })

  it('keeps today’s sentence on an escalated run that never acted', async () => {
    const escalated: AgentRun = {
      ...TAKE7,
      state: 'escalated',
      plan: null,
      steps: (TAKE7.steps ?? []).filter((s) => s.seq <= 3),
      verification: null,
      verifications: [],
      briefing: { ...(TAKE7.briefing as NonNullable<AgentRun['briefing']>), final_state: 'escalated' },
    }
    stub(() => [escalated])
    renderDemo()
    await screen.findByTestId('briefing-card')
    expect(screen.getByTestId('briefing-action').textContent).toMatch(
      /None — the agent escalated without acting\./,
    )
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// Item 3 — "ranked 01:25:24" on a 01:52 run.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage v6 — a new run never inherits the previous run’s steps', () => {
  it('stamps "ranked" with the take-7 judge’s own time after switching from the rehearsal', async () => {
    vi.useFakeTimers()
    // The page first shows the rehearsal run, then the take-7 run arrives on a later poll.
    stub((call) => (call <= 1 ? [PRIOR] : [TAKE7, PRIOR]))
    renderDemo()
    await vi.waitFor(() => {
      expect(screen.getByTestId('hypotheses-now').textContent).toContain(
        clock('2026-09-27T08:25:24.782979Z'),
      )
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100)
    })
    await vi.waitFor(() => {
      expect((screen.getByTestId('run-selector') as HTMLSelectElement).value).toBe(TAKE7.id)
    })
    const now = screen.getByTestId('hypotheses-now').textContent ?? ''
    expect(now).toContain(`ranked ${clock('2026-09-27T08:52:13.122199Z')}`)
    expect(now).toContain('step #6')
    expect(now).not.toContain(clock('2026-09-27T08:25:24.782979Z'))
    // The rehearsal's DLQ reads and its seventh and eighth steps are not this run's.
    const ledger = screen.getByTestId('action-ledger').textContent ?? ''
    expect(ledger).not.toMatch(/list_dlq_messages/)
    expect(screen.getAllByTestId('ledger-entry')).toHaveLength(6)
  })
})

// ─────────────────────────────────────────────────────────────────────────────
// Items 4 and 5 — the counts line, and what a verify row judged.
// ─────────────────────────────────────────────────────────────────────────────

describe('DemoPage v6 — counts and the verify row', () => {
  it('says both numbers plainly, with no "which make none"', async () => {
    stub(() => [TAKE7])
    renderDemo()
    await waitFor(() => {
      expect(screen.getByTestId('ledger-counts').textContent).toMatch(
        /^3 tool calls \(reads \+ actions\) · 3 planner\/judge steps/,
      )
    })
    const counts = screen.getByTestId('ledger-counts').textContent ?? ''
    expect(counts).not.toMatch(/which make none/)
    expect(counts).not.toMatch(/steps reported/)
  })

  it('puts the reading the judge was given, and when it was measured, on the verify row', async () => {
    stub(() => [TAKE7])
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry')).toHaveLength(6)
    })
    const judge = screen
      .getAllByTestId('ledger-entry')
      .find((e) => e.dataset.kind === 'think' && e.dataset.seq === '6') as HTMLElement
    expect(judge.textContent).toContain('judged on lag 55 measured 3 s BEFORE the action')
    // The commander reports `ok`; a successful read is not drawn as a failed one.
    const read = screen
      .getAllByTestId('ledger-entry')
      .find((e) => e.dataset.kind === 'read' && e.dataset.seq === '5') as HTMLElement
    expect(read.className).not.toMatch(/border-red/)
  })
})
