/**
 * The admin console's list state belongs to each tab, not to the page (R2-73).
 *
 * One shared `total`/`page`/rows object surfaced five ways: the NL-filtered
 * job list was overwritten by the structured loader it re-fired; resolving
 * from the DLQ tab refreshed the Jobs tab; the pager hardcoded 20 rows on tabs
 * that request 50; the pager rendered on tabs whose loaders never set a total,
 * driven by the previously visited tab's count; and a created tenant was
 * spliced in as a full row it is not.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import AdminPage from '../pages/AdminPage'
import DashboardPage from '../pages/DashboardPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import { jobsApi } from '../api/jobs'
import type { Job, Tenant, User } from '../types'

vi.mock('../api/admin', () => ({
  adminApi: {
    listJobs: vi.fn(),
    listUsers: vi.fn(),
    listTenants: vi.fn(),
    listAuditLogs: vi.fn(),
    listDigests: vi.fn(),
    runbooks: vi.fn(),
    dlqStats: vi.fn(),
    systemStats: vi.fn(),
    slos: vi.fn(),
    nlQuery: vi.fn(),
    resolveIncident: vi.fn(),
    createTenant: vi.fn(),
  },
}))

vi.mock('../api/jobs', () => ({ jobsApi: { list: vi.fn(), create: vi.fn() } }))

// AdminPage only offers the Tenants tab to a platform admin, and Layout needs
// a user too.
vi.mock('../hooks/useAuth', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({
    user: {
      id: 'u-1',
      tenant_id: 't-1',
      tenant_slug: 'acme',
      email: 'admin@example.com',
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

const listJobs = vi.mocked(adminApi.listJobs)
const listUsers = vi.mocked(adminApi.listUsers)
const listTenants = vi.mocked(adminApi.listTenants)
const nlQuery = vi.mocked(adminApi.nlQuery)
const resolveIncident = vi.mocked(adminApi.resolveIncident)
const createTenant = vi.mocked(adminApi.createTenant)

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: '11111111-1111-1111-1111-111111111111',
    user_id: '22222222-2222-2222-2222-222222222222',
    type: 'csv_upload',
    status: 'dead_letter',
    idempotency_key: null,
    payload: null,
    result: null,
    error_message: 'boom',
    retry_count: 3,
    max_retries: 3,
    dead_lettered_by: null,
    priority: 0,
    trace_id: null,
    created_at: '2026-08-09T00:00:00Z',
    started_at: null,
    completed_at: null,
    ...overrides,
  }
}

function page<T>(items: T[], total = items.length, pageSize = 20) {
  return { items, total, page: 1, page_size: pageSize, has_next: false }
}

function tenant(overrides: Partial<Tenant> = {}): Tenant {
  return {
    id: 'tenant-1',
    slug: 'acme',
    name: 'Acme Corp',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
    users: 4,
    jobs: 12,
    rate_limit_per_minute: 60,
    quota_jobs_per_month: 5000,
    ...overrides,
  }
}

function user(i: number): User {
  return {
    id: `user-${i}`,
    tenant_id: 't-1',
    tenant_slug: 'acme',
    email: `user${i}@example.com`,
    role: 'user',
    is_active: true,
    is_platform_admin: false,
    created_at: '2026-01-01T00:00:00Z',
  }
}

function renderAdmin() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <AdminPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(adminApi.systemStats).mockResolvedValue({ by_status: {} })
  vi.mocked(adminApi.slos).mockResolvedValue({ slos: [] })
  vi.mocked(adminApi.dlqStats).mockResolvedValue({ total: 1, by_type: {} })
  vi.mocked(adminApi.runbooks).mockResolvedValue({ items: [], count: 0 })
  vi.mocked(adminApi.listDigests).mockResolvedValue({ items: [], count: 0 })
  vi.mocked(adminApi.listAuditLogs).mockResolvedValue(page([]))
  listJobs.mockResolvedValue(page([job({ type: 'csv_upload' })]))
  listUsers.mockResolvedValue(page([user(1)], 1, 50))
  listTenants.mockResolvedValue({ items: [tenant()], total: 1, page: 1, page_size: 50 })
})

describe('admin console per-tab list state', () => {
  it('keeps the NL-filtered job list instead of re-firing the structured loader over it', async () => {
    // The structured loader always answers with a CSV Upload row; the NL query
    // answers with a Doc Analysis one. Clearing the status filter after
    // storing the NL results used to change the loader's identity and re-fire
    // it, replacing the LLM's answer while the chip still claimed it applied.
    listJobs.mockResolvedValue(page([job({ type: 'csv_upload', status: 'failed' })]))
    nlQuery.mockResolvedValue({
      spec: { status: 'dead_letter' },
      model: 'test-model',
      usage: {},
      items: [job({ id: '33333333-3333-3333-3333-333333333333', type: 'doc_analysis' })],
      total: 1,
    })

    renderAdmin()
    await userEvent.click(screen.getByRole('button', { name: 'jobs' }))
    await screen.findByText('CSV Upload')

    // A structured filter has to be in play — that is what runNlQuery cleared.
    await userEvent.selectOptions(screen.getByRole('combobox'), 'failed')
    await waitFor(() => expect(listJobs).toHaveBeenCalledTimes(2))

    await userEvent.type(
      screen.getByPlaceholderText(/dead-lettered CSV uploads/),
      'show me dead letters',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Ask' }))

    expect(await screen.findByText('Doc Analysis')).toBeDefined()
    // Settle: any re-fire would have landed by now.
    await waitFor(() => expect(screen.queryByText('CSV Upload')).toBeNull())
    expect(listJobs).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Doc Analysis')).toBeDefined()
  })

  it('refreshes the DLQ tab — not the Jobs tab — when resolving from the DLQ', async () => {
    resolveIncident.mockResolvedValue(job())

    renderAdmin()
    await userEvent.click(screen.getByRole('button', { name: /DLQ/ }))
    await waitFor(() => expect(listJobs).toHaveBeenCalledTimes(1))
    expect(listJobs.mock.calls[0][0]).toMatchObject({ status: 'dead_letter' })

    // After resolving, the row is gone from the DLQ.
    listJobs.mockResolvedValue(page([]))
    await userEvent.click(screen.getAllByRole('button', { name: 'Resolve' })[0])

    await waitFor(() => expect(listJobs).toHaveBeenCalledTimes(2))
    // The refresh must be the DLQ's own query, not the jobs tab's.
    expect(listJobs.mock.calls[1][0]).toMatchObject({ status: 'dead_letter' })
    expect(await screen.findByText('Dead letter queue is empty.')).toBeDefined()
  })

  it('pages a 50-per-page tab by 50, so it never offers a page that does not exist', async () => {
    // 60 users at 50 per page is exactly two pages. The old pager divided by a
    // hardcoded 20 and offered three.
    listUsers.mockResolvedValue(page(Array.from({ length: 50 }, (_, i) => user(i)), 60, 50))

    renderAdmin()
    await userEvent.click(screen.getByRole('button', { name: 'users' }))

    expect(await screen.findByText('Page 1 of 2')).toBeDefined()

    await userEvent.click(screen.getByRole('button', { name: 'Next →' }))
    await screen.findByText('Page 2 of 2')
    expect(screen.getByRole('button', { name: 'Next →' })).toHaveProperty('disabled', true)
  })

  it('renders no pager on a tab whose loader reports no total', async () => {
    listUsers.mockResolvedValue(page(Array.from({ length: 50 }, (_, i) => user(i)), 60, 50))

    renderAdmin()
    await userEvent.click(screen.getByRole('button', { name: 'users' }))
    await screen.findByText('Page 1 of 2')

    // Runbooks is unpaginated. Its pager used to be driven by the count left
    // behind by the previous tab.
    await userEvent.click(screen.getByRole('button', { name: 'runbooks' }))
    await screen.findByText('No runbooks found.')
    expect(screen.queryByRole('button', { name: 'Next →' })).toBeNull()
    expect(screen.queryByText(/^Page /)).toBeNull()
  })

  it('shows a created tenant with its real limits by refetching the list', async () => {
    // POST /admin/tenants returns the row only — no counts, no limits.
    createTenant.mockResolvedValue({
      id: 'tenant-2',
      slug: 'newco',
      name: 'New Co',
      is_active: true,
      created_at: '2026-08-13T00:00:00Z',
    })

    renderAdmin()
    await userEvent.click(screen.getByRole('button', { name: 'tenants' }))
    await screen.findByText('Acme Corp')

    listTenants.mockResolvedValue({
      items: [tenant({ id: 'tenant-2', slug: 'newco', name: 'New Co', users: 0, jobs: 0, rate_limit_per_minute: 120, quota_jobs_per_month: 10000 }), tenant()],
      total: 2,
      page: 1,
      page_size: 50,
    })

    await userEvent.click(screen.getByRole('button', { name: '+ New tenant' }))
    await userEvent.type(screen.getByPlaceholderText('acme'), 'newco')
    await userEvent.type(screen.getByPlaceholderText('Acme Corp'), 'New Co')
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(listTenants).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('New Co')).toBeDefined()
    // The new row's limit inputs carry the server's values, not blanks.
    const rateInputs = screen.getAllByDisplayValue('120')
    expect(rateInputs.length).toBeGreaterThan(0)
    expect(screen.getAllByDisplayValue('10000').length).toBeGreaterThan(0)
  })
})

describe('dashboard list stays consistent with its own filter', () => {
  it('does not insert a created job into a list that filters it out', async () => {
    const listMock = vi.mocked(jobsApi.list)
    listMock.mockResolvedValue(page([job({ type: 'report_gen', status: 'completed' })], 1, 15))
    vi.mocked(jobsApi.create).mockResolvedValue(
      job({ id: '44444444-4444-4444-4444-444444444444', type: 'doc_analysis', status: 'pending' }),
    )

    render(
      <MemoryRouter>
        <ToastProvider>
          <DashboardPage />
        </ToastProvider>
      </MemoryRouter>,
    )
    await screen.findByText('Report Gen')

    // Filter to Completed — a brand-new `pending` job does not belong here.
    await userEvent.click(screen.getByRole('button', { name: 'Completed' }))
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2))

    await userEvent.click(screen.getByRole('button', { name: '+ New job' }))
    await userEvent.click(screen.getByRole('button', { name: 'Submit job' }))

    // It refetches instead of prepending a row the filter excludes.
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(3))
    expect(screen.queryByText('Doc Analysis')).toBeNull()
  })
})
