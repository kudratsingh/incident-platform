/**
 * Nothing on the `/demo` page's ledger, hypotheses or briefing is cut short (WO-R3-359).
 * Owner after the ninth take: "the tools, hypotheses etc are cut off, I need to be able to see all the data".
 * Assertions read textContent and the clipping classes, never what happens to be visible in jsdom.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import DemoPage from '../pages/DemoPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import { thinkHeadline } from '../utils/demoRun'
import take9Json from './fixtures/take9-run.json'
import take9Rows from './fixtures/take9-audit-rows.json'
import type { AgentRun, AgentRunStepRecord, AgentRunStepsResponse, AuditLog } from '../types'

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

const TAKE9 = take9Json as unknown as AgentRun
const ROWS = take9Rows as unknown as AuditLog[]

// A 40-character tool name and a 300-character plain-text excerpt no summariser knows.
const LONG_TOOL = 'get_worker_dispatcher_partition_offsets_'
const LONG_EXCERPT = Array.from(
  { length: 12 },
  (_, i) => `partition ${String(i).padStart(2, '0')} holds 0003 unread`,
).join('; ')

/** Class names that clip text: none may appear inside a panel that carries data. */
const CLIPPING = /\b(truncate|line-clamp-\d|text-ellipsis)\b/

function clippedIn(root: HTMLElement): string[] {
  return [...root.querySelectorAll<HTMLElement>('*')]
    .filter((el) => CLIPPING.test(el.getAttribute('class') ?? ''))
    .map((el) => el.textContent ?? '')
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

function stub(run: AgentRun, rows: AuditLog[] = []) {
  vi.mocked(adminApi.listAgentRuns).mockResolvedValue(page([run], 50))
  vi.mocked(adminApi.getAgentRun).mockResolvedValue(run)
  vi.mocked(adminApi.agentRunSteps).mockResolvedValue(tailOf(run))
  vi.mocked(adminApi.consumerLag).mockResolvedValue({
    measured_at: '2026-09-27T11:14:00Z',
    total: 1,
    live_group: 'worker-dispatcher',
    sample_window_seconds: 900,
    sample_interval_seconds: 5,
    groups: [],
  })
  vi.mocked(adminApi.dlqStats).mockResolvedValue({ total: 0, by_type: {} })
  vi.mocked(adminApi.listJobs).mockResolvedValue(page([], 20))
  vi.mocked(adminApi.listAuditLogs).mockResolvedValue(page(rows))
  vi.mocked(adminApi.circuitBreakers).mockResolvedValue({
    measured_at: '2026-09-27T11:14:00Z',
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

/** Take 9 with its deploy-history read renamed to a 40-char tool carrying a 300-char answer. */
function take9WithLongRead(): AgentRun {
  const steps: AgentRunStepRecord[] = (TAKE9.steps ?? []).map((s) =>
    s.seq === 4 ? { ...s, tool: LONG_TOOL, result_excerpt: LONG_EXCERPT } : s,
  )
  return { ...TAKE9, steps }
}

function entry(seq: number): HTMLElement {
  const found = screen
    .getAllByTestId('ledger-entry')
    .find((el) => el.dataset.seq === String(seq))
  if (found === undefined) throw new Error(`no ledger row for step #${String(seq)}`)
  return found
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('the ledger shows every character (item 1)', () => {
  it('renders a 40-char tool name and a 300-char excerpt whole on the collapsed row', async () => {
    expect(LONG_TOOL).toHaveLength(40)
    expect(LONG_EXCERPT.length).toBeGreaterThanOrEqual(300)
    stub(take9WithLongRead())
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry')).toHaveLength(10)
    })
    const row = entry(4)
    expect(row.getAttribute('aria-expanded') ?? 'closed').not.toBe('true')
    expect(row.textContent).toContain(LONG_TOOL)
    expect(row.textContent).toContain(LONG_EXCERPT)
    expect(row.textContent).not.toContain('…')
  })

  it('clips nothing anywhere in the ledger', async () => {
    stub(take9WithLongRead(), ROWS)
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry').length).toBeGreaterThanOrEqual(10)
    })
    expect(clippedIn(screen.getByTestId('action-ledger'))).toEqual([])
  })

  it('shows the whole 400-char excerpt a read carries (get_deploy_history) without a click', async () => {
    stub(TAKE9)
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry')).toHaveLength(10)
    })
    const excerpt = (TAKE9.steps ?? []).find((s) => s.seq === 4)?.result_excerpt ?? ''
    expect(excerpt).toHaveLength(400)
    expect(entry(4).textContent).toContain(excerpt)
    // Opened, the result block carries it too, and nothing in the row is clipped.
    fireEvent.click(within(entry(4)).getByRole('button'))
    expect(within(entry(4)).getByTestId('ledger-result').textContent).toBe(excerpt)
    expect(clippedIn(entry(4))).toEqual([])
  })

  it('heads a THINK row with the top hypothesis whole, its category, confidence and next action', async () => {
    stub(TAKE9)
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry')).toHaveLength(10)
    })
    const probe = entry(3).textContent ?? ''
    expect(probe).toContain('worker-dispatcher-lag-36-climbing')
    expect(probe).toContain('consumer_saturation')
    expect(probe).toContain('0.85')
    expect(probe).toContain('→ probe get_deploy_history')

    const remediate = entry(5)
    expect(remediate.textContent).toContain('worker-dispatcher-lag-36-climbing')
    expect(remediate.textContent).toContain('0.92')
    expect(remediate.textContent).toContain('→ remediate')
    // The planner's reason as the platform holds it, every character; the page adds no "…".
    const reason = ((TAKE9.steps ?? []).find((s) => s.seq === 5)?.result_excerpt ?? '').replace(
      /^top \S+ [\d.]+ → remediate: /,
      '',
    )
    expect(reason.length).toBeGreaterThan(200)
    expect(within(remediate).getByTestId('ledger-think-reason').textContent).toBe(reason)
  })

  it('shows the verify judge’s verdict and its whole reason', async () => {
    stub(TAKE9)
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ledger-entry')).toHaveLength(10)
    })
    const judge = entry(8)
    expect(judge.textContent).toContain('→ verify 1/6 not_verified')
    const excerpt = (TAKE9.steps ?? []).find((s) => s.seq === 8)?.result_excerpt ?? ''
    expect(judge.textContent).toContain(excerpt.slice(excerpt.indexOf('All 15 samples')))
  })
})

describe('the hypotheses panel shows every ranked cause whole (item 2)', () => {
  it('renders every hypothesis’s full reasoning with no "more" button', async () => {
    stub(TAKE9)
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('hypothesis-row')).toHaveLength(3)
    })
    const rows = screen.getAllByTestId('hypothesis-row')
    TAKE9.hypotheses?.forEach((h, i) => {
      expect(rows[i].textContent).toContain(h.name)
      expect(rows[i].textContent).toContain(h.category ?? '')
      expect(rows[i].textContent).toContain(h.reasoning_excerpt ?? '')
      expect(within(rows[i]).queryByRole('button', { name: /more/i })).toBeNull()
    })
    expect(clippedIn(screen.getByTestId('agent-panel'))).toEqual([])
  })

  it('opens the earlier rankings to every cause they carry, with the planner’s reason', async () => {
    stub(TAKE9)
    renderDemo()
    await waitFor(() => {
      expect(screen.getAllByTestId('ranking-history-entry').length).toBeGreaterThan(0)
    })
    const older = screen.getAllByTestId('ranking-history-entry')
    const remediateStep = (TAKE9.steps ?? []).find((s) => s.seq === 5)
    const reason = String(remediateStep?.arguments?.reason ?? '')
    const text = older.map((el) => el.textContent ?? '').join('\n')
    expect(text).toContain('v0.4.3-prod-deploy-2h-before-incident')
    expect(text).toContain(reason)
  })
})

describe('the briefing and the stations are not clamped (item 3)', () => {
  it('shows the writer’s whole prose and the paged station’s whole summary', async () => {
    stub(TAKE9, ROWS)
    renderDemo()
    const card = await screen.findByTestId('briefing-card')
    expect(card.textContent).toContain(TAKE9.briefing?.prose?.split('\n\n')[1] ?? 'missing')
    expect(clippedIn(card)).toEqual([])

    const raised = ROWS.find((r) => r.action === 'alert.raised')
    const summary = String(raised?.extra_data?.summary ?? '')
    expect(summary.length).toBeGreaterThan(100)
    await waitFor(() => {
      expect(screen.getByTestId('station-paged').textContent).toContain(summary)
    })
    expect(clippedIn(screen.getByTestId('station-paged'))).toEqual([])
  })
})

describe('thinkHeadline — the THINK row’s two lines', () => {
  const judge = (TAKE9.steps ?? []).find((s) => s.seq === 10) as AgentRunStepRecord

  it('splits the judge’s sentence into the verdict headline and its whole reason', () => {
    const line = thinkHeadline(judge)
    expect(line?.headline).toBe(
      'top worker-dispatcher-lag-36-climbing (consumer_saturation 0.92) → verify 2/6 verified',
    )
    expect(line?.reason).toBe(
      (judge.result_excerpt ?? '').replace(/^top \S+ [\d.]+ → verify 2\/6 verified: /, ''),
    )
  })

  it('keeps a sentence of another shape whole under a headline built from the arguments', () => {
    const odd = { ...judge, result_excerpt: 'no verdict: the reading was missing' }
    expect(thinkHeadline(odd)).toEqual({
      headline: 'top worker-dispatcher-lag-36-climbing (consumer_saturation 0.92)',
      reason: 'no verdict: the reading was missing',
    })
  })

  it('falls back to the sentence alone when the step carries no ranking', () => {
    const bare = { ...judge, arguments: {}, result_excerpt: 'escalating: budget spent' }
    expect(thinkHeadline(bare)).toEqual({ headline: 'escalating: budget spent', reason: null })
    expect(thinkHeadline((TAKE9.steps ?? [])[1])).toBeNull()
  })
})
