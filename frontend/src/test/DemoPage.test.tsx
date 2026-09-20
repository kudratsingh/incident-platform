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
  /** The platform's own window, which the chart labels its axis from. */
  windowSeconds?: number
  intervalSeconds?: number
  stepsDropped?: number
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
  const steps = f.steps ?? []
  agentRunSteps.mockResolvedValue({
    run_id: runs[0]?.id ?? 'run-1',
    state: runs[0]?.state ?? 'investigating',
    finished_at: runs[0]?.finished_at ?? null,
    // Ascending by seq, as the endpoint promises.
    steps: [...steps].sort((a, b) => a.seq - b.seq),
    returned: steps.length,
    total: steps.length,
    steps_dropped: f.stepsDropped ?? 0,
    after_seq: null,
    next_after_seq: steps.length === 0 ? null : Math.max(...steps.map((s) => s.seq)),
  })
  consumerLag.mockResolvedValue({
    measured_at: '2026-09-19T10:02:00Z',
    total: 1,
    live_group: 'worker-dispatcher',
    sample_window_seconds: f.windowSeconds ?? 900,
    sample_interval_seconds: f.intervalSeconds ?? 60,
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
    expect(meter.textContent).toContain('/13')
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

  it('is one row per step, oldest first with the newest at the bottom', async () => {
    // WO-R3-334 turned the ledger round: a live run's newest call is where the eye
    // already is, so the panel reads like a transcript and follows the bottom.
    stub({ runs: [agentRun()], steps, audit: [RESET_ROW, FAULT_ROW] })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThanOrEqual(4)
    })
    const kinds = screen.getAllByTestId('ledger-entry').map((e) => e.dataset.kind)
    expect(kinds.slice(-4)).toEqual(['lab', 'read', 'action', 'report'])
  })

  it('summarises what a read answered on its one line', async () => {
    stub({ runs: [agentRun()], steps, audit: [FAULT_ROW] })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
    })
    const read = screen
      .getAllByTestId('ledger-entry')
      .find((e) => e.dataset.kind === 'read')
    expect(read?.textContent).toMatch(/get_consumer_lag/)
    expect(read?.textContent).toMatch(/→ lag 42, known/)
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
    const read = screen
      .getAllByTestId('ledger-entry')
      .find((e) => e.dataset.kind === 'read')
    await user.click(within(read as HTMLElement).getByRole('button'))
    expect(screen.getByTestId('ledger-result').textContent).toMatch(/lag 42 on worker/)
  })

  it('never collapses an ACTION row — the action is the point', async () => {
    stub({ runs: [agentRun()], steps, audit: [FAULT_ROW] })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(
        screen.getAllByTestId('ledger-entry').some((e) => e.dataset.kind === 'action'),
      ).toBe(true)
    })
    const action = screen
      .getAllByTestId('ledger-entry')
      .find((e) => e.dataset.kind === 'action')
    // Its arguments and its latency are on screen with nothing clicked.
    expect(action?.textContent).toMatch(/worker-dispatcher/)
    expect(action?.textContent).toMatch(/18 ms/)
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

describe('DemoPage — the shapes WO-R3-328 actually ships', () => {
  const steps = [
    step(1, 'read', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
    step(2, 'action', 'restart_consumer_group', '2026-09-19T10:02:00Z'),
  ]

  it('never reads the ledger from a list row', async () => {
    // The LISTING omits `steps` — absent, not emptied, because an empty list
    // there would read as "this run made no calls". A row that carried them
    // anyway must not reach the ledger: the detail and the tail read are the
    // only sources.
    const listRow = {
      ...agentRun(),
      steps: [step(99, 'action', 'replay_dlq_by_ids', '2026-09-19T10:09:00Z')],
    }
    stub({ runs: [listRow], detail: agentRun(), steps })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
    })
    expect(screen.getByTestId('action-ledger').textContent).not.toContain(
      'replay_dlq_by_ids',
    )
  })

  it('polls the tail with the platform’s own cursor, not one it computed', async () => {
    // `next_after_seq` is the highest seq STORED, so a poll that returns nothing
    // still advances; recomputing it from what arrived would re-read the tail.
    vi.useFakeTimers()
    stub({ runs: [agentRun()], steps })
    agentRunSteps.mockResolvedValue({
      run_id: 'run-1',
      state: 'investigating',
      finished_at: null,
      steps,
      returned: 2,
      total: 2,
      steps_dropped: 0,
      after_seq: null,
      next_after_seq: 7,
    })
    renderDemo()
    await vi.waitFor(() => {
      expect(agentRunSteps).toHaveBeenCalledWith('run-1', null)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(agentRunSteps).toHaveBeenCalledWith('run-1', 7)
  })

  it('reports the cap’s dropped steps from the ledger reply', async () => {
    stub({ runs: [agentRun()], steps, stepsDropped: 12 })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getByTestId('action-ledger').textContent).toMatch(
        /12 earlier steps dropped/,
      )
    })
  })

  it('keeps the responder’s own hypothesis order — the order IS the ranking', async () => {
    // Deliberately out of confidence order: the platform stores what the
    // responder sent, best first, and a reader that re-sorted would disagree
    // with the run about what it thought most likely.
    const run = agentRun({
      hypotheses: [
        { name: 'first as sent', category: 'consumer', confidence: 0.4 },
        { name: 'second as sent', category: 'cache', confidence: 0.9 },
      ],
    })
    stub({ runs: [run], detail: run })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getAllByTestId('hypothesis-row')).toHaveLength(2)
    })
    const names = screen.getAllByTestId('hypothesis-row').map((r) => r.textContent)
    expect(names[0]).toContain('first as sent')
    expect(names[1]).toContain('second as sent')
  })

  it('labels the chart’s axis from the reply’s own window', async () => {
    stub({ windowSeconds: 600, intervalSeconds: 30 })
    renderDemo()
    const chart = await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(chart.textContent).toMatch(/10 minutes/)
    })
    expect(chart.textContent).toMatch(/one every 30s/)
    expect(chart.textContent).toMatch(/−10 min/)
  })

  it('renders a step with no tool and no time rather than dropping it', async () => {
    // Every field but `seq` and `kind` can be null: the step is the responder's
    // own account of its call and the platform fills nothing in.
    const bare: AgentRunStepRecord = {
      seq: 1,
      kind: 'read',
      tool: null,
      at: null,
      arguments: null,
      result_excerpt: null,
      outcome: null,
      latency_ms: null,
    }
    stub({ runs: [agentRun()], steps: [bare] })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBe(1)
    })
    const entry = screen.getAllByTestId('ledger-entry')[0]
    expect(entry.textContent).toMatch(/no tool reported/)
    expect(entry.textContent).toMatch(/no time reported/)
  })

  it('renders an unknown verify verdict verbatim', async () => {
    const run = agentRun({
      verifications: [{ verdict: 'verified_unresolved', attempt: 1, of: 2 }],
    })
    stub({ runs: [run], detail: run })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getAllByTestId('verification-row')[0].textContent).toContain(
        'verified_unresolved',
      )
    })
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

  it('says why a run that never acted has no attribution', async () => {
    // The third take's `plan`, `verification` and `attribution` were all null and
    // all three were correct: the agent handed off without acting. "None recorded"
    // reads as a gap in the record; this reads as the consequence it is.
    const neverActed = agentRun({
      state: 'escalated',
      briefing: { ...briefing, final_state: 'escalated', attempted_action: null, attribution: null },
      finished_at: '2026-09-19T10:05:00Z',
      active: false,
    })
    stub({ runs: [neverActed], detail: neverActed })
    renderDemo()
    await screen.findByTestId('briefing-card')
    expect(screen.getByTestId('briefing-attribution').textContent).toMatch(
      /no attribution because no action/i,
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

// ─────────────────────────────────────────────────────────────────────────────
// WO-R3-334 — the page reads ONE take.
//
// The owner recorded the third take, the runner wound the world down, and the page
// was reloaded two minutes later. The screenshot showed a header saying "no run in
// this take yet", a PLATFORM row saying `healthy · since the reset`, an AGENT row
// saying `escalated`, three blue action markers for calls the evaluator's guard
// probes had made, and a ledger claiming 89 calls against 0 steps. Every number was
// true of some moment; none of them were true of the same one.
//
// The rows below are that screenshot's shape:
//
//   09:59  boundary          ← the take opens
//   10:00  kill_consumer     ← the lab's fault
//   10:01  the run starts … escalates at 10:03
//   10:04  boundary          ← the wind-down closes the take
//   10:05  lab.probe + a call by the runner's principal
// ─────────────────────────────────────────────────────────────────────────────

const OPEN_BOUNDARY = auditRow({
  action: 'lab.world_reset',
  created_at: '2026-09-19T09:59:00Z',
  extra_data: { chaos_keys_cleared: 4 },
})
const CLOSE_BOUNDARY = auditRow({
  action: 'lab.world_reset',
  created_at: '2026-09-19T10:04:00Z',
  extra_data: { chaos_keys_cleared: 6 },
})
/** The evaluator's own read, labelled by the lab because it wears the agent's token. */
const LAB_PROBE_ROW = auditRow({
  action: 'lab.probe',
  created_at: '2026-09-19T10:05:00Z',
  extra_data: { tool_name: 'mark_dlq_permanent', arguments: {}, outcome: 'error' },
})
/** A call by the demo runner's own principal, after the boundary. */
const RUNNER_ROW = auditRow({
  action: 'agent.tool_invoked',
  principal_id: 'sa-runner',
  created_at: '2026-09-19T10:05:10Z',
  extra_data: { tool_name: 'mark_dlq_permanent', arguments: {}, outcome: 'success' },
})

const WIND_DOWN_ROWS = [
  OPEN_BOUNDARY,
  FAULT_ROW,
  toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:30Z'),
  CLOSE_BOUNDARY,
  LAB_PROBE_ROW,
  RUNNER_ROW,
]

const CLOSED_RUN = agentRun({
  id: 'run-take-3',
  state: 'escalated',
  phase_history: [
    { state: 'triage', at: '2026-09-19T10:01:00Z' },
    { state: 'investigating', at: '2026-09-19T10:01:00Z' },
    { state: 'escalated', at: '2026-09-19T10:01:00Z' },
  ],
  started_at: '2026-09-19T10:01:00Z',
  finished_at: '2026-09-19T10:03:00Z',
  active: false,
})

describe('DemoPage — one take, chosen by the run', () => {
  it('shows the run’s own take after the wind-down, and says the take ended', async () => {
    stub({ runs: [CLOSED_RUN], detail: CLOSED_RUN, audit: WIND_DOWN_ROWS })
    renderDemo()
    await screen.findByTestId('phase-row-platform')
    await waitFor(() => {
      expect(screen.getByTestId('take-label').textContent).toMatch(/take ended at/)
    })
    // The reading the screenshot could not give: the platform saw the fault of the
    // take the agent's `escalated` belongs to.
    expect(stationState('fault_injected')).not.toBe('pending')
    expect(screen.getByTestId('fault-clock').textContent).toMatch(/T\+/)
    expect(screen.getByTestId('fault-clock').textContent).toMatch(/stopped at the take/)
    expect((screen.getByTestId('run-selector') as HTMLSelectElement).value).toBe('run-take-3')
  })

  it('leaves the next take’s rows out of this take’s ledger', async () => {
    // The seven bogus "agent" rows of finding F4: the evaluator's probes and the
    // world audit, written AFTER the boundary, which the page read as a new run.
    stub({ runs: [CLOSED_RUN], detail: CLOSED_RUN, audit: WIND_DOWN_ROWS })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
    })
    expect(screen.getByTestId('action-ledger').textContent).not.toMatch(/mark_dlq_permanent/)
    // Both edges of the take are drawn, so it is clear where it begins and ends.
    expect(screen.getAllByTestId('ledger-reset-divider')).toHaveLength(2)
  })

  it('marks no agent action for a take where the agent never acted', async () => {
    stub({ runs: [CLOSED_RUN], detail: CLOSED_RUN, audit: WIND_DOWN_ROWS })
    renderDemo()
    await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(screen.getAllByTestId('chart-marker-fault').length).toBe(1)
    })
    expect(screen.queryAllByTestId('chart-marker-action')).toHaveLength(0)
    expect(screen.getAllByTestId('chart-marker-reset')).toHaveLength(2)
  })

  it('honours ?run= and reads that run’s take', async () => {
    const liveRun = agentRun({ id: 'run-live', started_at: '2026-09-19T10:06:00Z' })
    stub({ runs: [CLOSED_RUN, liveRun], detail: liveRun, audit: WIND_DOWN_ROWS })
    renderDemo('?run=run-live')
    await screen.findByTestId('run-selector')
    await waitFor(() => {
      expect(screen.getByTestId('take-label').textContent).toMatch(/live/)
    })
    expect(getAgentRun).toHaveBeenCalledWith('run-live')
  })
})

describe('DemoPage — a late report reads as late', () => {
  const runId = 'run-late'
  function reportRow(state: string, at: string): AuditLog {
    return auditRow({
      action: 'agent.run_reported',
      created_at: at,
      extra_data: { tool_name: 'report_agent_run', arguments: { run_id: runId, state } },
    })
  }
  const late = agentRun({
    id: runId,
    state: 'investigating',
    phase_history: [
      { state: 'triage', at: '2026-09-19T10:01:00Z' },
      { state: 'investigating', at: '2026-09-19T10:01:00Z' },
    ],
  })

  it('shows when the report arrived, beside when the event happened', async () => {
    // F2: `phase_history` was three entries stamped 08:17:59 with 76 ms / 9 ms / 0 ms
    // of duration, and the whole burst reached the platform 41 seconds later.
    stub({
      runs: [late],
      detail: late,
      audit: [
        RESET_ROW,
        FAULT_ROW,
        reportRow('triage', '2026-09-19T10:01:41Z'),
        reportRow('investigating', '2026-09-19T10:01:41Z'),
      ],
    })
    renderDemo()
    await screen.findByTestId('phase-row-agent')
    await waitFor(() => {
      expect(screen.getByTestId('station-triage-reported')).toBeTruthy()
    })
    expect(screen.getByTestId('station-triage-reported').textContent).toMatch(/reported /)
  })

  it('says nothing about a report that arrived when it happened', async () => {
    stub({
      runs: [late],
      detail: late,
      audit: [RESET_ROW, FAULT_ROW, reportRow('triage', '2026-09-19T10:01:01Z')],
    })
    renderDemo()
    await screen.findByTestId('phase-row-agent')
    await waitFor(() => {
      expect(stationState('triage')).not.toBe('pending')
    })
    expect(screen.queryByTestId('station-triage-reported')).toBeNull()
  })

  it('advances one station at a time when a burst of reports lands at once', async () => {
    vi.useFakeTimers()
    const first = agentRun({
      state: 'triage',
      phase_history: [{ state: 'triage', at: '2026-09-19T10:01:00Z' }],
    })
    const burst = agentRun({
      state: 'escalated',
      phase_history: [
        { state: 'triage', at: '2026-09-19T10:01:00Z' },
        { state: 'investigating', at: '2026-09-19T10:01:01Z' },
        { state: 'planning', at: '2026-09-19T10:01:02Z' },
        { state: 'escalated', at: '2026-09-19T10:01:03Z' },
      ],
      finished_at: '2026-09-19T10:01:03Z',
      active: false,
    })
    stub({ runs: [first], detail: first, audit: [RESET_ROW, FAULT_ROW] })
    listAgentRuns.mockResolvedValueOnce(page([first])).mockResolvedValue(page([burst]))
    getAgentRun.mockResolvedValueOnce(first).mockResolvedValue(burst)

    renderDemo()
    await vi.waitFor(() => {
      expect(stationState('triage')).toBe('current')
    })
    // The burst arrives on the next poll: three more stations in one answer.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100)
    })
    expect(stationState('investigating')).toBe('pending')

    // One station per tick, in order, rather than four in one frame. Each advance is
    // one reveal step: the next station's timer is set by the render the previous one
    // caused, so a single long advance would only prove that they all arrive.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(350)
    })
    expect(stationState('investigating')).toBe('current')
    expect(stationState('planning')).toBe('pending')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300)
    })
    expect(stationState('planning')).toBe('current')
    expect(stationState('terminal')).toBe('pending')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300)
    })
    expect(stationState('planning')).toBe('passed')
    expect(stationState('terminal')).toBe('current')
  })
})

describe('DemoPage — the ledger hides what is not this run’s', () => {
  const rows = [
    RESET_ROW,
    FAULT_ROW,
    toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:01:00Z'),
    auditRow({
      action: 'agent.tool_invoked',
      principal_id: 'sa-runner',
      created_at: '2026-09-19T10:01:03Z',
      extra_data: { tool_name: 'get_consumer_lag', arguments: {}, outcome: 'success' },
    }),
    auditRow({
      action: 'lab.probe',
      created_at: '2026-09-19T10:01:06Z',
      extra_data: { tool_name: 'list_dlq_messages', arguments: {}, outcome: 'success' },
    }),
  ]

  it('counts the hidden reads and leaves them out of the comparison', async () => {
    stub({ runs: [agentRun()], steps: [], audit: rows })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getByTestId('ledger-hidden-reads')).toBeTruthy()
    })
    expect(screen.getByTestId('ledger-hidden-reads').textContent).toMatch(
      /2 evaluator\/traffic reads hidden/,
    )
    // One call by this run's own principal, not three.
    expect(screen.getByTestId('ledger-counts').textContent).toMatch(
      /1 calls the platform recorded/,
    )
  })

  it('shows them, each labelled for whose they are, on the toggle', async () => {
    const user = userEvent.setup()
    stub({ runs: [agentRun()], steps: [], audit: rows })
    renderDemo()
    await screen.findByTestId('action-ledger')
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThan(0)
    })
    expect(
      screen.getAllByTestId('ledger-entry').some((e) => e.dataset.kind === 'lab_probe'),
    ).toBe(false)

    await user.click(screen.getByTestId('hidden-reads-toggle'))
    await waitFor(() => {
      expect(
        screen.getAllByTestId('ledger-entry').some((e) => e.dataset.kind === 'lab_probe'),
      ).toBe(true)
    })
    const kinds = screen.getAllByTestId('ledger-entry').map((e) => e.dataset.kind)
    expect(kinds).toContain('other_principal')
    expect(screen.getByTestId('action-ledger').textContent).toMatch(/NOT THIS RUN/)
  })
})

describe('DemoPage — the chart reads the take', () => {
  it('zooms to the take and back out to the platform’s whole window', async () => {
    const user = userEvent.setup()
    stub({
      runs: [agentRun()],
      audit: [
        RESET_ROW,
        auditRow({
          action: 'chaos.tool_invoked',
          created_at: ago(4),
          extra_data: { tool_name: 'kill_consumer', arguments: {} },
        }),
      ],
      samples: [
        { lag: 30, measured_at: ago(0.5) },
        { lag: 10, measured_at: ago(2) },
      ],
      lag: 30,
    })
    renderDemo()
    const chart = await screen.findByTestId('metric-chart-lag')
    // Fault − 2 min → now: six minutes, not fifteen.
    await waitFor(() => {
      expect(chart.textContent).toMatch(/−6 min/)
    })
    await user.click(screen.getByTestId('metric-chart-lag-zoom'))
    await waitFor(() => {
      expect(chart.textContent).toMatch(/−15 min/)
    })
  })

  it('draws a cursor on the right edge with the newest reading on it', async () => {
    stub({ lag: 30, samples: [{ lag: 30, measured_at: ago(0.5) }] })
    renderDemo()
    await screen.findByTestId('metric-chart-lag')
    await waitFor(() => {
      expect(screen.getByTestId('metric-chart-lag-cursor').textContent).toBe('30')
    })
  })
})

describe('DemoPage — the agent panel after the third take', () => {
  const longReasoning =
    'lag climbed from 0 to 30 on worker-dispatcher and no member is assigned to the group, ' +
    'which is what a killed consumer looks like from the outside; the DLQ is empty and every ' +
    'breaker is closed, so nothing downstream explains it.'

  it('shows the top hypothesis whole and truncates the rest', async () => {
    const run = agentRun({
      hypotheses: [
        {
          name: 'dispatcher consumer is down',
          category: 'consumer_failure',
          confidence: 0.82,
          reasoning_excerpt: longReasoning,
        },
        {
          name: 'slow downstream',
          category: 'dependency',
          confidence: 0.2,
          reasoning_excerpt: longReasoning,
        },
      ],
    })
    stub({ runs: [run], detail: run })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getAllByTestId('hypothesis-row')).toHaveLength(2)
    })
    const [top, second] = screen.getAllByTestId('hypothesis-row')
    // The screenshot truncated the top one with "more…", which hid the only
    // explanation of why the agent believed what it believed.
    expect(top.textContent).toContain('nothing downstream explains it.')
    expect(within(top).queryByRole('button', { name: /more/i })).toBeNull()
    expect(second.textContent).not.toContain('nothing downstream explains it.')
    expect(within(second).getByRole('button', { name: /more/i })).toBeTruthy()
  })

  it('draws the remediate threshold on the confidence bar', async () => {
    const run = agentRun({
      hypotheses: [{ name: 'consumer down', category: 'consumer_failure', confidence: 0.82 }],
    })
    stub({ runs: [run], detail: run })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getAllByTestId('confidence-threshold-tick').length).toBe(1)
    })
  })

  it('says plainly why a terminal run has no plan and no verification', async () => {
    const escalated = agentRun({
      state: 'escalated',
      finished_at: '2026-09-19T10:03:00Z',
      active: false,
    })
    stub({ runs: [escalated], detail: escalated })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getByTestId('plan-empty').textContent).toMatch(
        /handed off without acting/,
      )
    })
    expect(screen.getByTestId('verifications-empty').textContent).toMatch(
      /no verification because no action/i,
    )
  })

  it('still says "not yet" while the run is live', async () => {
    stub({ runs: [agentRun()] })
    renderDemo()
    await screen.findByTestId('agent-panel')
    await waitFor(() => {
      expect(screen.getByTestId('plan-empty').textContent).toMatch(/No action planned yet/)
    })
    expect(screen.getByTestId('verifications-empty').textContent).toMatch(/Nothing verified yet/)
  })
})
