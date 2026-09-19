/**
 * The /demo page (WO-R3-313).
 *
 * One screen an operator records: the world on the left, the agent in the
 * middle, the audit timeline on the right, the briefing underneath. What these
 * tests hold in place is mostly about honesty rather than layout:
 *
 *  - an absent reading renders as absent WITH its reason, never as zero
 *    (ADR 0030's rule carried into the UI — a breaker with no record is
 *    missing, not closed, and a lag with `lag_known: false` is not lag 0);
 *  - a 403 on one panel degrades that panel, not the page — the two principals
 *    see different things and the console says which;
 *  - the phase strip shows the agent's word and the platform's reading side by
 *    side when they disagree, because merging them would put a state on screen
 *    that neither source asserted;
 *  - the DLQ row's "agent decided" badge comes from the agent's own audit rows,
 *    so a row nothing touched reads `leave` rather than looking remediated.
 */

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import DemoPage from '../pages/DemoPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import { AppError } from '../api/client'
import type { AgentRun, AuditLog, Job } from '../types'

vi.mock('../api/admin', () => ({
  adminApi: {
    listAgentRuns: vi.fn(),
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
const consumerLag = vi.mocked(adminApi.consumerLag)
const dlqStats = vi.mocked(adminApi.dlqStats)
const listJobs = vi.mocked(adminApi.listJobs)
const listAuditLogs = vi.mocked(adminApi.listAuditLogs)

let rowSeq = 0
function auditRow(overrides: Partial<AuditLog>): AuditLog {
  rowSeq += 1
  return {
    id: `row-${rowSeq}`,
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
  audit?: AuditLog[]
  jobs?: Job[]
  lagKnown?: boolean
  lag?: number
  dlqTotal?: number
}

function page<T>(items: T[], pageSize = 50) {
  return { items, total: items.length, page: 1, page_size: pageSize, has_next: false }
}

function stub(f: Fixture = {}) {
  listAgentRuns.mockResolvedValue(page(f.runs ?? []))
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
        // Newest first, as the endpoint promises — the page has to reverse it.
        // Timestamps are relative to now because the page drops samples older
        // than its five-minute window, which a fixed 2026 timestamp would be.
        recent_samples: [
          { lag: f.lag ?? 0, measured_at: new Date(Date.now() - 10_000).toISOString() },
          { lag: 0, measured_at: new Date(Date.now() - 70_000).toISOString() },
        ],
      },
    ],
  })
  dlqStats.mockResolvedValue({ total: f.dlqTotal ?? 0, by_type: {} })
  listJobs.mockResolvedValue({
    items: f.jobs ?? [],
    total: (f.jobs ?? []).length,
    page: 1,
    page_size: 20,
    has_next: false,
  })
  listAuditLogs.mockResolvedValue({
    items: f.audit ?? [],
    total: (f.audit ?? []).length,
    page: 1,
    page_size: 100,
    has_next: false,
  })
  // The two side readings the "platform readings" strip shows. An empty breaker
  // list WITHOUT `unknown_reason` is the healthy answer; with it set it means
  // the platform could say nothing, which the panel must not conflate.
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

beforeEach(() => {
  vi.clearAllMocks()
  stub()
})

afterEach(() => {
  Object.defineProperty(navigator, 'clipboard', {
    value: undefined,
    configurable: true,
    writable: true,
  })
})

describe('DemoPage — the three panels and the strip', () => {
  it('renders every panel', async () => {
    renderDemo()
    expect(await screen.findByRole('heading', { name: /the world/i })).toBeTruthy()
    expect(screen.getByRole('heading', { name: /the agent/i })).toBeTruthy()
    expect(screen.getByRole('heading', { name: /audit timeline/i })).toBeTruthy()
    // The strip itself, named so the recording has a landmark.
    expect(screen.getByRole('list', { name: /phase/i })).toBeTruthy()
    // And the nav can get here: a page nobody can reach is not a demo.
    expect(screen.getByRole('link', { name: 'Demo' })).toBeTruthy()
  })

  it('starts in consumer_outage and reads the mode from the URL', async () => {
    renderDemo('?mode=dlq_backlog')
    expect(await screen.findByRole('heading', { name: /dead-letter rows/i })).toBeTruthy()
  })

  it('shows no DLQ mini-table in consumer_outage mode', async () => {
    renderDemo('?mode=consumer_outage')
    await screen.findByRole('heading', { name: /the world/i })
    expect(screen.queryByRole('heading', { name: /dead-letter rows/i })).toBeNull()
  })

  it('switches mode from the header and keeps the URL in step', async () => {
    renderDemo()
    await screen.findByRole('heading', { name: /the world/i })
    await userEvent.click(screen.getByRole('button', { name: /dlq backlog/i }))
    expect(await screen.findByRole('heading', { name: /dead-letter rows/i })).toBeTruthy()
  })

  it('names both metrics with their thresholds', async () => {
    renderDemo()
    expect(await screen.findByText(/worker-dispatcher lag/i)).toBeTruthy()
    expect(screen.getByText(/dlq depth/i)).toBeTruthy()
    // The dashed line's value is on screen, not only in the SVG.
    expect(screen.getAllByText(/threshold/i).length).toBeGreaterThanOrEqual(2)
  })
})

describe('DemoPage — absent readings stay absent', () => {
  it('renders an unknown lag with its reason, never as zero', async () => {
    stub({ lagKnown: false })
    renderDemo()
    expect(await screen.findByText(/no lag reading cached/i)).toBeTruthy()
    const lagPanel = screen.getByTestId('metric-lag')
    expect(within(lagPanel).getByText(/unknown/i)).toBeTruthy()
    expect(within(lagPanel).queryByText(/^0$/)).toBeNull()
  })

  it('degrades one panel on a 403 and leaves the rest of the page up', async () => {
    listAgentRuns.mockRejectedValue(
      new AppError('Not permitted for this principal', 'authorization_failed', undefined, 403),
    )
    renderDemo()
    expect(await screen.findByText(/not permitted for this principal/i)).toBeTruthy()
    // The world panel still loaded.
    expect(screen.getByRole('heading', { name: /the world/i })).toBeTruthy()
  })

  it('says it is waiting when no run has been reported', async () => {
    renderDemo()
    expect(await screen.findByText(/waiting for the agent/i)).toBeTruthy()
  })

  it('says so when the audit log has nothing yet', async () => {
    renderDemo()
    expect(await screen.findByText(/no audit rows yet/i)).toBeTruthy()
  })

  it('says no fault has been injected before any lab row', async () => {
    renderDemo()
    expect(await screen.findByText(/no fault injected yet/i)).toBeTruthy()
  })

  it('tells an unknowable breaker listing apart from an empty one', async () => {
    // Both answers are an empty array. Only one of them means "nothing is
    // open"; the other means the platform could tell you nothing, and
    // rendering them the same way is the ADR 0030 mistake one layer up.
    stub()
    renderDemo()
    expect(await screen.findByText(/no breaker open among those publishing state/i)).toBeTruthy()

    vi.mocked(adminApi.circuitBreakers).mockResolvedValue({
      measured_at: '2026-09-19T10:02:00Z',
      breakers: [],
      total: 0,
      unknown_reason: 'the breaker state store is unreachable',
    })
    renderDemo()
    expect(
      await screen.findByText(/breaker state unknown — the breaker state store is unreachable/i),
    ).toBeTruthy()
  })

  it('reads the lag sparkline’s seed samples oldest-first', async () => {
    // `recent_samples` arrives NEWEST first. Fed in as given, the series' own
    // span goes negative and every point lands off the left edge — so assert
    // the rendered polyline advances left to right.
    stub({ lag: 40 })
    renderDemo()
    const panel = await screen.findByTestId('metric-lag')
    await waitFor(() => expect(panel.querySelector('polyline')).toBeTruthy())
    const points = panel.querySelector('polyline')!.getAttribute('points')!
    const xs = points.split(' ').map((p) => Number(p.split(',')[0]))
    expect(xs.length).toBeGreaterThan(1)
    expect(xs[0]).toBe(0)
    for (let i = 1; i < xs.length; i += 1) {
      expect(xs[i]).toBeGreaterThanOrEqual(xs[i - 1])
    }
  })
})

describe('DemoPage — the agent card', () => {
  it('shows the state, the hypothesis with its confidence, and the last step', async () => {
    stub({
      runs: [
        agentRun({
          state: 'planning',
          current_hypothesis: {
            name: 'dispatcher consumer stopped',
            category: 'consumer_saturation',
            confidence: 0.82,
          },
          last_step: {
            kind: 'read',
            tool: 'get_consumer_lag',
            at: '2026-09-19T10:02:00Z',
          },
        }),
      ],
      audit: [FAULT_ROW],
    })
    renderDemo()

    const card = await screen.findByTestId('agent-card')
    expect(within(card).getByText(/planning/i)).toBeTruthy()
    expect(within(card).getByText(/dispatcher consumer stopped/)).toBeTruthy()
    expect(within(card).getByText(/consumer_saturation/)).toBeTruthy()
    expect(within(card).getByText(/82%/)).toBeTruthy()
    expect(within(card).getByText(/get_consumer_lag/)).toBeTruthy()
  })

  it('renders the phase history as a timeline with durations', async () => {
    stub({ runs: [agentRun()], audit: [FAULT_ROW] })
    renderDemo()
    const card = await screen.findByTestId('agent-card')
    expect(within(card).getByText(/triage/i)).toBeTruthy()
    // triage → investigating took 20s.
    expect(within(card).getByText(/20\.0s/)).toBeTruthy()
  })
})

describe('DemoPage — the phase strip’s two sources', () => {
  it('shows one reading when the agent and the platform agree', async () => {
    stub({
      runs: [agentRun({ state: 'remediating' })],
      audit: [
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z'),
        FAULT_ROW,
      ],
      lag: 40,
    })
    renderDemo()
    await screen.findByTestId('agent-card')
    await waitFor(() => expect(screen.queryByTestId('phase-disagreement')).toBeNull())
  })

  it('shows both readings, unmerged, when they disagree', async () => {
    stub({
      runs: [agentRun({ state: 'verifying' })],
      audit: [
        toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z'),
        FAULT_ROW,
      ],
      lag: 40,
    })
    renderDemo()
    const note = await screen.findByTestId('phase-disagreement')
    expect(note.textContent).toMatch(/verifying/i)
    expect(note.textContent).toMatch(/remediating/i)
  })

  it('counts the wall clock from the lab’s own audit row', async () => {
    stub({ audit: [FAULT_ROW] })
    renderDemo()
    expect(await screen.findByTestId('fault-clock')).toBeTruthy()
    expect(screen.queryByText(/no fault injected yet/i)).toBeNull()
  })
})

describe('DemoPage — the audit timeline', () => {
  const rows = [
    toolRow('agent.tool_invoked', 'restart_consumer_group', '2026-09-19T10:03:00Z', {
      consumer_group: 'worker-dispatcher',
    }, { latency_ms: 41.5 }),
    toolRow('agent.tool_invoked', 'get_consumer_lag', '2026-09-19T10:02:00Z'),
    toolRow('agent.tool_invoked', 'list_dlq_messages', '2026-09-19T10:01:30Z'),
    auditRow({
      action: 'agent.run_reported',
      created_at: '2026-09-19T10:01:10Z',
      extra_data: { state: 'investigating' },
    }),
    auditRow({
      action: 'job.created',
      principal_type: 'user',
      user_id: 'u-1',
      created_at: '2026-09-19T10:00:30Z',
    }),
    FAULT_ROW,
  ]

  it('expands agent actions and collapses reads behind a count', async () => {
    stub({ audit: rows })
    renderDemo()
    const panel = await screen.findByTestId('audit-timeline')
    // The action is expanded: tool, arguments and latency are all on screen.
    expect(within(panel).getByText(/restart_consumer_group/)).toBeTruthy()
    // The lab row shows its arguments too, so this name appears more than once.
    expect(within(panel).getAllByText(/worker-dispatcher/).length).toBeGreaterThan(0)
    expect(within(panel).getByText(/41\.5\s*ms/)).toBeTruthy()
    // The two reads are one collapsed group with a count.
    expect(within(panel).getByTestId('audit-reads-group').textContent).toMatch(/2/)
  })

  it('filters to the lab, to the agent and to humans', async () => {
    stub({ audit: rows })
    renderDemo()
    const panel = await screen.findByTestId('audit-timeline')

    await userEvent.click(within(panel).getByRole('button', { name: /^lab$/i }))
    await waitFor(() => expect(within(panel).getByText(/kill_consumer/)).toBeTruthy())
    expect(within(panel).queryByText(/restart_consumer_group/)).toBeNull()

    await userEvent.click(within(panel).getByRole('button', { name: /^human$/i }))
    await waitFor(() => expect(within(panel).getByText(/job\.created/)).toBeTruthy())
    expect(within(panel).queryByText(/kill_consumer/)).toBeNull()

    await userEvent.click(within(panel).getByRole('button', { name: /^agent$/i }))
    await waitFor(() =>
      expect(within(panel).getByText(/restart_consumer_group/)).toBeTruthy(),
    )
    expect(within(panel).queryByText(/job\.created/)).toBeNull()
  })

  it('asks the API for the lab stream by prefix when the lab chip is on', async () => {
    stub({ audit: rows })
    renderDemo()
    const panel = await screen.findByTestId('audit-timeline')
    await userEvent.click(within(panel).getByRole('button', { name: /^lab$/i }))
    await waitFor(() =>
      expect(listAuditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ action_prefix: 'chaos.' }),
      ),
    )
  })
})

describe('DemoPage — the DLQ mini-table', () => {
  const poison = job({
    id: '44444444-4444-4444-4444-444444444444',
    remediation_hint: 'replay_safe',
    dead_lettered_at: '2026-09-19T09:56:00Z',
    triage: {
      root_cause_category: 'validation_error',
      summary: 'payload fails the job.submitted schema',
    },
  })
  const fenced = job({
    id: '55555555-5555-5555-5555-555555555555',
    remediation_hint: 'human_required',
    fenced_at: '2026-09-19T09:57:00Z',
    // The real shape: `{principal_type}:{id}`, not a friendly name.
    fenced_by: 'service_account:99999999-9999-9999-9999-999999999999',
  })

  it('renders hint, triage class and fence state per row', async () => {
    stub({ jobs: [poison, fenced], dlqTotal: 2 })
    renderDemo('?mode=dlq_backlog')
    const table = await screen.findByTestId('dlq-mini-table')
    expect(within(table).getByText(/replay_safe/)).toBeTruthy()
    expect(within(table).getByText(/validation_error/)).toBeTruthy()
    expect(within(table).getByText(/human_required/)).toBeTruthy()
    // The fenced row says who fenced it, id truncated with the whole value in
    // the title.
    expect(within(table).getByText(/service_account:/)).toBeTruthy()
  })

  it('badges each row with what the agent decided about it', async () => {
    stub({
      jobs: [poison, fenced],
      dlqTotal: 2,
      audit: [
        toolRow('agent.tool_invoked', 'replay_dlq_by_ids', '2026-09-19T10:04:00Z', {
          job_ids: [poison.id],
        }),
        FAULT_ROW,
      ],
    })
    renderDemo('?mode=dlq_backlog')
    const table = await screen.findByTestId('dlq-mini-table')
    const rows = within(table).getAllByRole('row').slice(1) // drop the header
    // By testid, not by text: the poison row's own `remediation_hint` is
    // `replay_safe`, so /replay/ matches the hint as well as the badge.
    expect(within(rows[0]).getByTestId('dlq-decision').textContent).toMatch(/^replay$/)
    expect(within(rows[1]).getByTestId('dlq-decision').textContent).toMatch(/^leave$/)
  })

  it('says the queue is empty rather than rendering a bare table', async () => {
    stub({ jobs: [], dlqTotal: 0 })
    renderDemo('?mode=dlq_backlog')
    expect(await screen.findByText(/no dead-letter rows/i)).toBeTruthy()
  })
})

describe('DemoPage — the briefing card', () => {
  const briefing = {
    incident_id: 'inc-1',
    final_state: 'escalated',
    alert_summary: 'consumer lag on worker-dispatcher',
    escalation_reason: 'the lag never moved after the restart',
    attempted_action: {
      tool: 'restart_consumer_group',
      arguments: { consumer_group: 'worker-dispatcher' },
    },
    incidents: {
      primary: {
        category: 'consumer_saturation',
        name: 'dispatcher stopped',
        confidence: 0.75,
        addressed: true,
      },
      secondary: [],
      unresolved_extra: [
        {
          category: 'dlq_backlog',
          name: 'five dead letters',
          confidence: 0.4,
          addressed: false,
        },
      ],
    },
    prose: 'The restart landed but the group stayed down.',
  }

  it('renders nothing until the briefing lands', async () => {
    stub({ runs: [agentRun()] })
    renderDemo()
    await screen.findByTestId('agent-card')
    expect(screen.queryByTestId('briefing-card')).toBeNull()
  })

  it('renders the final state, the reason, the slots and the prose', async () => {
    stub({ runs: [agentRun({ state: 'escalated', briefing })] })
    renderDemo()
    const card = await screen.findByTestId('briefing-card')
    expect(within(card).getByTestId('briefing-final-state').textContent).toBe('escalated')
    expect(within(card).getByText(/consumer lag on worker-dispatcher/)).toBeTruthy()
    expect(within(card).getByText(/the lag never moved after the restart/)).toBeTruthy()
    expect(within(card).getByText(/dispatcher stopped/)).toBeTruthy()
    expect(within(card).getByText(/five dead letters/)).toBeTruthy()
    expect(within(card).getByText(/restart_consumer_group/)).toBeTruthy()
    expect(within(card).getByText(/the restart landed but the group stayed down/i)).toBeTruthy()
  })

  it('copies itself as Markdown', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
      writable: true,
    })
    stub({ runs: [agentRun({ state: 'escalated', briefing })] })
    renderDemo()
    const card = await screen.findByTestId('briefing-card')
    await userEvent.click(within(card).getByRole('button', { name: /copy as markdown/i }))

    expect(writeText).toHaveBeenCalledTimes(1)
    const md = String(writeText.mock.calls[0][0])
    expect(md).toMatch(/^# Escalation briefing/m)
    expect(md).toMatch(/consumer lag on worker-dispatcher/)
    expect(md).toMatch(/unresolved/i)
    expect(md).toMatch(/five dead letters/)
  })
})
