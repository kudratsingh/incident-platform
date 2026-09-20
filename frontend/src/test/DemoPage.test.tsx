/**
 * The /demo page as a run dashboard (WO-R3-330, rebuilt from WO-R3-313).
 *
 * The owner's first live take is the specification these tests come from. Each
 * `describe` below holds one of its findings closed:
 *
 *  - **two rows, always both** — the old single strip switched to the agent's word
 *    the moment a run existed, so `fault injected` was never on screen;
 *  - **the fault is latched** — the row that states it falls off the page of audit
 *    rows within a minute of the traffic loop starting, and the strip fell back to
 *    `healthy` mid-incident;
 *  - **recovery takes two samples** — the cached lag reads 42 → 0 → 42 as it ages,
 *    and one zero was enough to announce a recovery;
 *  - **the agent panel is filled** — from the ranked hypotheses, the plan, the
 *    verify verdicts and the budget (WO-R3-328), with each absence named;
 *  - **the ledger is the steps** — with results, with the lab interleaved, and with
 *    the job events that were 43 of 50 rows behind one toggle, off;
 *  - **the run selector** — and with it the bug that cost the first take its ending:
 *    the page asked for `active=true`, so a resolved run and its briefing vanished.
 *
 * The rules from the first build still hold and are still tested: an absent reading
 * renders as absent WITH its reason and never as zero, and one panel's 403 degrades
 * that panel rather than the page.
 */

import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import DemoPage from '../pages/DemoPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import { AppError } from '../api/client'
import type {
  AgentRun,
  AgentRunStepRecord,
  AuditLog,
  Job,
  LagSample,
} from '../types'

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
const consumerLag = vi.mocked(adminApi.consumerLag)
const dlqStats = vi.mocked(adminApi.dlqStats)
const listJobs = vi.mocked(adminApi.listJobs)
const listAuditLogs = vi.mocked(adminApi.listAuditLogs)

let rowSeq = 0
function auditRow(overrides: Partial<AuditLog>): AuditLog {
  rowSeq += 1
  return {
    id: `row-${String(rowSeq)}`,
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
    created_at: '2026-09-19T10:01:00Z',
    ...overrides,
  }
}

function toolRow(
  action: string,
  tool: string,
  at: string,
  args: Record<string, unknown> = {},
  extra: Record<string, unknown> = {},
): AuditLog {
  return auditRow({
    action,
    created_at: at,
    extra_data: { tool_name: tool, arguments: args, outcome: 'success', ...extra },
  })
}

const FAULT_ROW = toolRow('chaos.tool_invoked', 'kill_consumer', '2026-09-19T10:00:00Z', {
  consumer_group: 'worker-dispatcher',
})

const RESET_ROW = auditRow({
  action: 'lab.world_reset',
  created_at: '2026-09-19T09:59:00Z',
  resource_type: 'world',
  extra_data: { chaos_keys_cleared: 4, hot_set_reseeded: 1 },
})

const JOB_EVENT_ROW = auditRow({
  action: 'event.job.completed',
  created_at: '2026-09-19T10:01:30Z',
})

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
    latency_ms: 18,
    ...overrides,
  }
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: '44444444-4444-4444-4444-444444444444',
    user_id: 'u-9',
    type: 'bulk_api_sync',
    status: 'dead_letter',
    idempotency_key: null,
    payload: null,
    result: null,
    error_message: 'schema validation failed: missing field job_id',
    retry_count: 3,
    max_attempts: 3,
    dead_lettered_by: null,
    priority: 0,
    trace_id: null,
    created_at: '2026-09-19T09:55:00Z',
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

function agentRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'run-1',
    tenant_id: 't-1',
    alert_id: 'alert-1',
    service_account_id: 'sa-1',
    scenario: 'remediate_consumer_lag_success',
    state: 'investigating',
    phase_history: [
      { state: 'triage', at: '2026-09-19T10:01:00Z' },
      { state: 'investigating', at: '2026-09-19T10:01:20Z' },
    ],
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

interface Fixture {
  runs?: AgentRun[]
  /** The detail answer; defaults to the first listed run. */
  detail?: AgentRun | null
  detailError?: unknown
  steps?: AgentRunStepRecord[]
  audit?: AuditLog[]
  jobEvents?: AuditLog[]
  jobs?: Job[]
  dlqJobs?: Job[]
  lagKnown?: boolean
  lag?: number
  /** Newest-first, as the endpoint promises. */
  samples?: LagSample[]
  dlqTotal?: number
}

function page<T>(items: T[], pageSize = 50) {
  return { items, total: items.length, page: 1, page_size: pageSize, has_next: false }
}

/** Minutes before now, so the 15-minute window keeps them. */
function ago(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString()
}

function stub(f: Fixture = {}) {
  const runs = f.runs ?? []
  listAgentRuns.mockResolvedValue(page(runs))
  if (f.detailError) getAgentRun.mockRejectedValue(f.detailError)
  else getAgentRun.mockResolvedValue((f.detail ?? runs[0] ?? null) as AgentRun)
  agentRunSteps.mockResolvedValue({
    run_id: runs[0]?.id ?? 'run-1',
    items: f.steps ?? [],
    max_seq: (f.steps ?? []).length,
    steps_dropped: 0,
  })
  consumerLag.mockResolvedValue({
    measured_at: '2026-09-19T10:02:00Z',
    total: 1,
    live_group: 'worker-dispatcher',
    groups: [
      {
        consumer_group: 'worker-dispatcher',
        lag: f.lagKnown === false ? null : (f.lag ?? 0),
        lag_known: f.lagKnown !== false,
        source: 'live',
        lag_unknown_reason: f.lagKnown === false ? 'no lag reading cached' : null,
        measured_at: '2026-09-19T10:02:00Z',
        age_seconds: 3,
        // The platform's own 15-minute window, newest first (WO-R3-328). Relative
        // to now because the chart's window is relative to now.
        recent_samples: f.samples ?? [
          { lag: f.lag ?? 0, measured_at: ago(0.2) },
          { lag: 0, measured_at: ago(1.2) },
        ],
      },
    ],
  })
  dlqStats.mockResolvedValue({ total: f.dlqTotal ?? 0, by_type: {} })
  listJobs.mockImplementation((params = {}) =>
    Promise.resolve(
      params.status === 'dead_letter'
        ? page(f.dlqJobs ?? [], 20)
        : page(f.jobs ?? [], 20),
    ),
  )
  // Two audit queries: the operator streams the page derives from, and the job
  // lifecycle, which is only asked for while the toggle is on.
  listAuditLogs.mockImplementation((params = {}) =>
    Promise.resolve(
      params.action_prefix === 'event.'
        ? page(f.jobEvents ?? [], 100)
        : page(f.audit ?? [], 100),
    ),
  )
  vi.mocked(adminApi.circuitBreakers).mockResolvedValue({
    measured_at: '2026-09-19T10:02:00Z',
    breakers: [],
    total: 0,
    unknown_reason: null,
  })
  vi.mocked(adminApi.listAlerts).mockResolvedValue(page([]))
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

/** The station's own state, which is what the two rows are asserting. */
function stationState(key: string): string | undefined {
  return screen.getByTestId(`station-${key}`).dataset.state
}

beforeEach(() => {
  vi.clearAllMocks()
  stub()
})

afterEach(() => {
  vi.useRealTimers()
  Object.defineProperty(navigator, 'clipboard', {
    value: undefined,
    configurable: true,
    writable: true,
  })
})

describe('DemoPage — the shape of the page', () => {
  it('renders both phase rows, the chart, the agent panel and the ledger', async () => {
    renderDemo()
    expect(await screen.findByTestId('phase-row-platform')).toBeTruthy()
    expect(screen.getByTestId('phase-row-agent')).toBeTruthy()
    expect(screen.getByTestId('metric-chart-lag')).toBeTruthy()
    expect(screen.getByTestId('agent-panel')).toBeTruthy()
    expect(screen.getByTestId('action-ledger')).toBeTruthy()
  })

  it('starts in consumer_outage and reads the mode from the URL', async () => {
    renderDemo('?mode=dlq_backlog')
    expect(await screen.findByTestId('metric-chart-dlq')).toBeTruthy()
    expect(screen.queryByTestId('metric-chart-lag')).toBeNull()
  })

  it('switches mode from the header and keeps the URL in step', async () => {
    const user = userEvent.setup()
    renderDemo()
    await screen.findByTestId('metric-chart-lag')
    await user.click(screen.getByRole('button', { name: /DLQ backlog/i }))
    expect(await screen.findByTestId('metric-chart-dlq')).toBeTruthy()
  })

  it('asks the audit API for the operator streams only, in one request', async () => {
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(listAuditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ action_prefix: 'agent.,lab.,chaos.' }),
      )
    })
    // The job lifecycle is 43 rows in 50 with traffic running; it is not fetched
    // until the operator asks for it.
    expect(listAuditLogs).not.toHaveBeenCalledWith(
      expect.objectContaining({ action_prefix: 'event.' }),
    )
  })

  it('reads every run rather than only the active ones', async () => {
    // The first take's console asked for `active=true`, and a run's terminal report
    // stamps `finished_at` — so the run and its briefing left the screen the instant
    // it resolved.
    renderDemo()
    await screen.findByTestId('agent-panel')
    expect(listAgentRuns).toHaveBeenCalledWith()
  })
})

describe('DemoPage — two rows, always both, never merged', () => {
  it('shows the platform on fault injected while the agent says investigating', async () => {
    // The first take's single strip lit only the agent's station here, so the one
    // fact only the platform can assert was never on screen.
    stub({ audit: [RESET_ROW, FAULT_ROW], runs: [agentRun({ state: 'investigating' })] })
    renderDemo()
    await screen.findByTestId('phase-row-platform')
    await waitFor(() => {
      expect(stationState('fault_injected')).toBe('current')
    })
    expect(stationState('investigating')).toBe('current')
    expect(screen.getByTestId('phase-reading').textContent).toMatch(/fault injected/i)
    expect(screen.getByTestId('phase-reading').textContent).toMatch(/investigating/i)
  })

  it('renders the agent row in full before any run exists', async () => {
    stub({ audit: [RESET_ROW, FAULT_ROW] })
    renderDemo()
    await screen.findByTestId('phase-row-agent')
    for (const key of ['triage', 'investigating', 'planning', 'remediating', 'verifying']) {
      expect(stationState(key)).toBe('pending')
    }
    expect(screen.getByTestId('agent-panel').textContent).toMatch(
      /waiting for the responder/i,
    )
  })

  it('reads the platform row healthy after a reset with no new fault', async () => {
    stub({ audit: [RESET_ROW] })
    renderDemo()
    await screen.findByTestId('phase-row-platform')
    await waitFor(() => {
      expect(stationState('healthy')).toBe('current')
    })
    expect(stationState('fault_injected')).toBe('pending')
    expect(screen.getByTestId('fault-clock').textContent).toMatch(/no fault injected yet/i)
  })

  it('moves the platform row to agent acting and names the action', async () => {
    stub({
      audit: [
        RESET_ROW,
        FAULT_ROW,
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:02:00Z'),
      ],
      lag: 42,
    })
    renderDemo()
    await waitFor(() => {
      expect(stationState('agent_acting')).toBe('current')
    })
    expect(screen.getByTestId('station-agent_acting').textContent).toContain(
      'restart_consumer_group',
    )
  })

  it('counts the wall clock from the lab’s own audit row', async () => {
    stub({ audit: [RESET_ROW, FAULT_ROW] })
    renderDemo()
    await waitFor(() => {
      expect(screen.getByTestId('fault-clock').textContent).toMatch(/T\+/)
    })
  })

  it('keeps the fault latched when its row scrolls out of the window', async () => {
    // Finding 1's real mechanism: with traffic running, the one `chaos.*` row is
    // gone from the page of rows within a minute, and the row fell back to healthy.
    vi.useFakeTimers()
    stub({ audit: [RESET_ROW, FAULT_ROW] })
    listAuditLogs
      .mockResolvedValueOnce(page([RESET_ROW, FAULT_ROW], 100))
      .mockResolvedValue(page([RESET_ROW], 100))
    renderDemo()
    await vi.waitFor(() => {
      expect(stationState('fault_injected')).toBe('current')
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000)
    })
    expect(stationState('fault_injected')).toBe('current')
    expect(stationState('healthy')).toBe('passed')
  })
})

describe('DemoPage — the chart', () => {
  it('draws the platform’s samples with the threshold and its reason', async () => {
    stub({ lag: 42, samples: [{ lag: 42, measured_at: ago(0.5) }, { lag: 30, measured_at: ago(2) }] })
    renderDemo()
    const chart = await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(within(chart).getByTestId('metric-chart-lag-value').textContent).toBe('42')
    })
    expect(chart.textContent).toMatch(/threshold 20/)
    expect(chart.textContent).toMatch(/lag ≥ 20/)
    expect(chart.textContent).toMatch(/samples \(2\)/)
  })

  it('marks the fault, the action and the recovery on the line', async () => {
    stub({
      audit: [RESET_ROW, auditRow({ action: 'chaos.tool_invoked', created_at: ago(8), extra_data: { tool_name: 'kill_consumer', arguments: {} } })],
      runs: [agentRun()],
      steps: [
        step(1, 'read', 'get_consumer_lag', ago(7)),
        step(2, 'action', 'restart_consumer_group', ago(5)),
      ],
      samples: [
        { lag: 0, measured_at: ago(1) },
        { lag: 0, measured_at: ago(2) },
        { lag: 42, measured_at: ago(6) },
      ],
    })
    renderDemo()
    await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(screen.getAllByTestId('chart-marker-fault').length).toBe(1)
    })
    expect(screen.getAllByTestId('chart-marker-action').length).toBe(1)
    expect(screen.getAllByTestId('chart-marker-recovery').length).toBe(1)
    // Reads are deliberately not marked — the ledger is where every call belongs.
    expect(screen.getByTestId('metric-chart-lag').textContent).not.toMatch(
      /get_consumer_lag/,
    )
  })

  it('renders an unknown lag with its reason, never as zero', async () => {
    stub({ lagKnown: false })
    renderDemo()
    const chart = await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(within(chart).getByTestId('metric-chart-lag-value').textContent).toBe(
        'unknown',
      )
    })
    expect(chart.textContent).toMatch(/no lag reading cached/)
  })

  it('says so when the platform has published no samples', async () => {
    stub({ samples: [] })
    renderDemo()
    const chart = await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(chart.textContent).toMatch(/no samples in this window yet/i)
    })
  })
})

describe('DemoPage — recovery needs more than one sample', () => {
  const faultRow = auditRow({
    action: 'chaos.tool_invoked',
    created_at: ago(9),
    extra_data: { tool_name: 'kill_consumer', arguments: {} },
  })

  it('does not call the world recovered on one sub-threshold sample', async () => {
    // 42 → 0, which is the ageing cached sample. The first take's strip said
    // `recovered` here, in the middle of the incident.
    stub({
      audit: [RESET_ROW, faultRow],
      lag: 0,
      samples: [
        { lag: 0, measured_at: ago(1) },
        { lag: 42, measured_at: ago(5) },
      ],
    })
    renderDemo()
    await screen.findByTestId('phase-row-platform')
    await waitFor(() => {
      expect(screen.getByTestId('recovery-pending')).toBeTruthy()
    })
    expect(stationState('recovered')).toBe('pending')
  })

  it('reaches recovered once two consecutive samples are back inside', async () => {
    stub({
      audit: [RESET_ROW, faultRow],
      lag: 0,
      samples: [
        { lag: 0, measured_at: ago(1) },
        { lag: 0, measured_at: ago(2) },
        { lag: 42, measured_at: ago(5) },
      ],
    })
    renderDemo()
    await screen.findByTestId('phase-row-platform')
    await waitFor(() => {
      expect(stationState('recovered')).toBe('current')
    })
    expect(screen.queryByTestId('recovery-pending')).toBeNull()
  })
})

describe('DemoPage — the agent panel is the middle of the page', () => {
  const rich = agentRun({
    state: 'verifying',
    hypotheses: [
      {
        name: 'dispatcher consumer is down',
        category: 'consumer_failure',
        confidence: 0.82,
        reasoning_excerpt: 'lag climbed to 42 and no member is assigned to the group',
      },
      { name: 'slow downstream', category: 'dependency', confidence: 0.2, reasoning_excerpt: null },
    ],
    plan: {
      action_tool: 'restart_consumer_group',
      action_arguments: { consumer_group: 'worker-dispatcher' },
      target_hypothesis: 'dispatcher consumer is down',
      rationale_excerpt: 'restarting the group re-assigns the partitions',
    },
    verifications: [
      { verdict: 'not_verified', attempt: 1, of: 2, reasoning_excerpt: 'lag still 42' },
      { verdict: 'verified', attempt: 2, of: 2, reasoning_excerpt: 'lag back to 0' },
    ],
    budget: { tool_calls_used: 7, tool_calls_max: 13, tokens_used: 4210, usd_used: 0.42 },
  })

  it('shows the ranked hypotheses with confidence and reasoning', async () => {
    stub({ runs: [rich] })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getAllByTestId('hypothesis-row')).toHaveLength(2)
    })
    const first = screen.getAllByTestId('hypothesis-row')[0]
    expect(first.textContent).toContain('dispatcher consumer is down')
    expect(first.textContent).toContain('82%')
    expect(first.textContent).toContain('no member is assigned')
    expect(screen.getAllByTestId('hypothesis-row')[1].textContent).toMatch(
      /no reasoning reported/i,
    )
  })

  it('shows the plan with its tool, arguments, target and rationale', async () => {
    stub({ runs: [rich] })
    renderDemo()
    const plan = await screen.findByTestId('plan-card')
    expect(plan.textContent).toContain('restart_consumer_group')
    expect(plan.textContent).toContain('worker-dispatcher')
    expect(plan.textContent).toContain('dispatcher consumer is down')
    expect(plan.textContent).toMatch(/re-assigns the partitions/)
  })

  it('lists every verify verdict, not only the last', async () => {
    stub({ runs: [rich] })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getAllByTestId('verification-row')).toHaveLength(2)
    })
    const rows = screen.getAllByTestId('verification-row')
    expect(rows[0].textContent).toContain('not_verified')
    expect(rows[0].textContent).toContain('attempt 1 of 2')
    expect(rows[1].textContent).toContain('verified')
  })

  it('shows the budget as used against its cap', async () => {
    stub({ runs: [rich] })
    renderDemo()
    const meter = await screen.findByTestId('budget-meter')
    expect(meter.textContent).toContain('7')
    expect(meter.textContent).toContain('/ 13')
    expect(meter.textContent).toContain('4210 tokens')
    expect(meter.textContent).toContain('$0.4200')
  })

  it('names each absence rather than rendering a blank panel', async () => {
    stub({ runs: [agentRun()] })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getByTestId('hypotheses-empty')).toBeTruthy()
    })
    expect(screen.getByTestId('plan-empty')).toBeTruthy()
    expect(screen.getByTestId('verifications-empty')).toBeTruthy()
    expect(screen.getByTestId('budget-meter').textContent).toMatch(/not reported/i)
  })

  it('says when all it got was a top hypothesis', async () => {
    // What a commander older than WO-R3-329 reports: one hypothesis, no ranking,
    // no reasoning. Not the same finding as a run that ranked nothing.
    stub({
      runs: [
        agentRun({
          current_hypothesis: { name: 'consumer down', category: 'consumer', confidence: 0.6 },
        }),
      ],
    })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getByTestId('hypotheses-source-note')).toBeTruthy()
    })
    expect(screen.getAllByTestId('hypothesis-row')).toHaveLength(1)
  })

  it('degrades on a 403 and leaves the rest of the page up', async () => {
    const forbidden = () =>
      new AppError('Not permitted to read agent runs.', 'forbidden', 'req-1', 403)
    stub({ detailError: forbidden() })
    listAgentRuns.mockRejectedValue(forbidden())
    renderDemo()
    const panel = await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(panel.textContent).toMatch(/Not permitted to read agent runs/)
    })
    expect(screen.getByTestId('metric-chart-lag')).toBeTruthy()
    expect(screen.getByTestId('action-ledger')).toBeTruthy()
    expect(screen.getByTestId('phase-row-platform')).toBeTruthy()
  })
})

describe('DemoPage — the action ledger', () => {
  const steps = [
    step(1, 'read', 'get_consumer_lag', '2026-09-19T10:01:00Z', {
      result_excerpt: 'lag 42 on worker-dispatcher, known, measured 3s ago',
      arguments: { consumer_group: 'worker-dispatcher' },
    }),
    step(2, 'action', 'restart_consumer_group', '2026-09-19T10:02:00Z', {
      arguments: { consumer_group: 'worker-dispatcher' },
    }),
    step(3, 'report', 'report_agent_run', '2026-09-19T10:02:01Z'),
  ]

  it('is one row per step, newest first, with a kind badge each', async () => {
    stub({ runs: [agentRun()], steps, audit: [RESET_ROW, FAULT_ROW] })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThanOrEqual(4)
    })
    const kinds = screen.getAllByTestId('ledger-entry').map((e) => e.dataset.kind)
    expect(kinds.slice(0, 4)).toEqual(['report', 'action', 'read', 'lab'])
  })

  it('carries the result the audit log does not have, behind one click', async () => {
    const user = userEvent.setup()
    stub({ runs: [agentRun()], steps, audit: [FAULT_ROW] })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
    })
    expect(screen.queryByTestId('ledger-result')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'result' }))
    expect(screen.getByTestId('ledger-result').textContent).toMatch(/lag 42 on worker/)
  })

  it('draws the reset as a divider rather than as an event', async () => {
    stub({ runs: [agentRun()], steps, audit: [RESET_ROW, FAULT_ROW] })
    renderDemo()
    expect(await screen.findByTestId('ledger-reset-divider')).toBeTruthy()
  })

  it('leaves job events out by default and fetches them on the toggle', async () => {
    const user = userEvent.setup()
    stub({
      runs: [agentRun()],
      steps,
      audit: [RESET_ROW, FAULT_ROW],
      jobEvents: [JOB_EVENT_ROW],
    })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
    })
    expect(
      screen.getAllByTestId('ledger-entry').some((e) => e.dataset.kind === 'job_event'),
    ).toBe(false)

    await user.click(screen.getByTestId('job-events-toggle'))
    await waitFor(() => {
      expect(
        screen.getAllByTestId('ledger-entry').some((e) => e.dataset.kind === 'job_event'),
      ).toBe(true)
    })
    expect(listAuditLogs).toHaveBeenCalledWith(
      expect.objectContaining({ action_prefix: 'event.' }),
    )
  })

  it('counts both witnesses and says when they disagree', async () => {
    stub({
      runs: [agentRun()],
      steps: [steps[0]],
      audit: [
        FAULT_ROW,
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:02:00Z'),
      ],
    })
    renderDemo()
    const counts = await screen.findByTestId('ledger-counts')
    await waitFor(() => {
      expect(counts.textContent).toMatch(/1 steps reported/)
    })
    expect(counts.textContent).toMatch(/2 calls the platform recorded/)
    expect(counts.textContent).toMatch(/do not agree/)
  })

  it('falls back to the audit rows when no step was reported, and says so', async () => {
    stub({
      runs: [agentRun()],
      steps: [],
      audit: [
        FAULT_ROW,
        toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
      ],
    })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getByTestId('ledger-audit-fallback')).toBeTruthy()
    })
    const kinds = screen.getAllByTestId('ledger-entry').map((e) => e.dataset.kind)
    expect(kinds).toContain('agent_audit')
  })
})

describe('DemoPage — the run selector', () => {
  const older = agentRun({
    id: 'run-older',
    state: 'resolved',
    started_at: '2026-09-19T10:00:00Z',
    finished_at: '2026-09-19T10:04:00Z',
    active: false,
  })
  const newer = agentRun({ id: 'run-newer', started_at: '2026-09-19T10:06:00Z' })

  it('offers every run of the take and defaults to the newest', async () => {
    stub({ runs: [older, newer], detail: newer })
    renderDemo()
    const select = await screen.findByTestId('run-selector')
    await waitFor(() => {
      expect(within(select).getAllByRole('option')).toHaveLength(2)
    })
    expect((select as HTMLSelectElement).value).toBe('run-newer')
  })

  it('honours ?run= and asks the API for that run', async () => {
    stub({ runs: [older, newer], detail: older })
    renderDemo('?run=run-older')
    await screen.findByTestId('run-selector')
    await waitFor(() => {
      expect(getAgentRun).toHaveBeenCalledWith('run-older')
    })
  })

  it('says so when the take has no run yet', async () => {
    stub({ runs: [] })
    renderDemo()
    expect(await screen.findByTestId('run-selector-empty')).toBeTruthy()
    expect(getAgentRun).not.toHaveBeenCalled()
  })
})

describe('DemoPage — the briefing', () => {
  const briefing = {
    incident_id: 'inc-1',
    final_state: 'resolved',
    alert_summary: 'consumer lag above threshold on worker-dispatcher',
    escalation_reason: '',
    attempted_action: {
      tool: 'restart_consumer_group',
      arguments: { consumer_group: 'worker-dispatcher' },
    },
    incidents: {
      primary: {
        category: 'consumer_failure',
        name: 'dispatcher down',
        confidence: 0.82,
        addressed: true,
      },
      secondary: [],
      unresolved_extra: [],
    },
    attribution: {
      verdict: 'attributed',
      resource: 'worker-dispatcher',
      probe_tool: 'get_consumer_lag',
      acted: true,
      detail: 'lag read 42 before the restart and 0 after it',
    },
    prose: null,
  }
  const finished = agentRun({
    state: 'resolved',
    finished_at: '2026-09-19T10:05:00Z',
    active: false,
    briefing,
    verification: { verdict: 'verified', attempt: 1, of: 2 },
  })

  it('renders nothing until the briefing lands', async () => {
    stub({ runs: [agentRun()] })
    renderDemo()
    await screen.findByTestId('agent-panel')
    expect(screen.queryByTestId('briefing-card')).toBeNull()
  })

  it('stays on screen for a finished run, with its attribution and verdict', async () => {
    stub({ runs: [finished], detail: finished })
    renderDemo()
    const card = await screen.findByTestId('briefing-card')
    expect(within(card).getByTestId('briefing-final-state').textContent).toBe('resolved')
    const attribution = within(card).getByTestId('briefing-attribution')
    expect(attribution.textContent).toContain('attributed')
    expect(attribution.textContent).toContain('get_consumer_lag')
    expect(attribution.textContent).toMatch(/42 before the restart/)
    expect(within(card).getByTestId('briefing-verify-verdict').textContent).toContain(
      'verified',
    )
  })

  it('says so when no attribution was recorded', async () => {
    const noAttribution = agentRun({
      briefing: { ...briefing, attribution: null },
      finished_at: '2026-09-19T10:05:00Z',
      active: false,
    })
    stub({ runs: [noAttribution], detail: noAttribution })
    renderDemo()
    await screen.findByTestId('briefing-card')
    expect(screen.getByTestId('briefing-attribution').textContent).toMatch(
      /none recorded/i,
    )
  })

  it('copies itself as Markdown, attribution and verdict included', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
      writable: true,
    })
    stub({ runs: [finished], detail: finished })
    renderDemo()
    await screen.findByTestId('briefing-card')
    await user.click(screen.getByRole('button', { name: /copy as markdown/i }))
    await waitFor(() => {
      expect(writeText).toHaveBeenCalled()
    })
    const text = writeText.mock.calls[0][0]
    expect(text).toContain('**Verify verdict**: verified')
    expect(text).toContain('**Recovery attribution**: attributed')
    expect(text).toContain('Unresolved extra: none')
  })
})

describe('DemoPage — the DLQ table', () => {
  const poison = job({ id: 'dead-1111-2222-3333', remediation_hint: null })
  const replayable = job({
    id: 'safe-1111-2222-3333',
    remediation_hint: 'replay_safe',
    error_message: 'downstream 503',
  })

  it('badges each row with what the run’s own steps decided', async () => {
    stub({
      runs: [agentRun()],
      dlqJobs: [poison, replayable],
      steps: [
        step(4, 'action', 'replay_dlq_by_ids', '2026-09-19T10:03:00Z', {
          arguments: { job_ids: ['safe-1111-2222-3333'] },
        }),
      ],
    })
    renderDemo('?mode=dlq_backlog')
    await screen.findByTestId('dlq-table')
    await waitFor(() => {
      expect(screen.getAllByTestId('dlq-decision')).toHaveLength(2)
    })
    const badges = screen.getAllByTestId('dlq-decision').map((b) => b.textContent)
    expect(badges).toEqual(['leave', 'replay'])
  })

  it('prints an uncategorised hint as such, never as replay-safe', async () => {
    stub({ runs: [agentRun()], dlqJobs: [poison] })
    renderDemo('?mode=dlq_backlog')
    const table = await screen.findByTestId('dlq-table')
    expect(table.textContent).toMatch(/not categorised/)
  })

  it('says the queue is empty rather than rendering a bare table', async () => {
    stub({ runs: [agentRun()], dlqJobs: [] })
    renderDemo('?mode=dlq_backlog')
    await screen.findByTestId('metric-chart-dlq')
    await waitFor(() => {
      expect(screen.getByText(/No dead-letter rows right now/i)).toBeTruthy()
    })
  })
})
