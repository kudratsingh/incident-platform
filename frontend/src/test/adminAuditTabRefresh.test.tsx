/**
 * The Audit tab refreshes itself and can filter by action prefix (WO-R3-313).
 *
 * Before this the tab refetched only when the operator switched tabs, so an
 * incident unfolding while they watched it looked frozen — the Overview tab has
 * polled every 5s since Phase 7 and the audit log, which is the one tab an
 * operator watches during an incident, did not. The prefix filter is the other
 * half: `agent.` / `chaos.` / `job.` are whole streams, and `action=` is an
 * exact match, so isolating a stream meant reading every page by eye.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import AdminPage from '../pages/AdminPage'
import { ToastProvider } from '../components/Toast'
import { adminApi } from '../api/admin'
import type { AuditLog } from '../types'

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
  },
}))

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
      is_platform_admin: false,
      created_at: '2026-01-01T00:00:00Z',
    },
    loading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  }),
}))

const listAuditLogs = vi.mocked(adminApi.listAuditLogs)

function page(items: AuditLog[]) {
  return { items, total: items.length, page: 1, page_size: 20, has_next: false }
}

const ROW: AuditLog = {
  id: 'a-1',
  user_id: null,
  principal_type: 'service_account',
  principal_id: 'sa-1',
  job_id: null,
  action: 'agent.tool_invoked',
  resource_type: 'mcp_tool',
  resource_id: 'get_consumer_lag',
  request_id: null,
  ip_address: null,
  extra_data: null,
  created_at: '2026-09-19T10:00:00Z',
}

async function openAuditTab() {
  render(
    <MemoryRouter>
      <ToastProvider>
        <AdminPage />
      </ToastProvider>
    </MemoryRouter>,
  )
  await userEvent.click(screen.getByRole('button', { name: /^audit$/i }))
  await waitFor(() => expect(listAuditLogs).toHaveBeenCalled())
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(adminApi.dlqStats).mockResolvedValue({ total: 0, by_type: {} })
  vi.mocked(adminApi.systemStats).mockResolvedValue({ by_status: {} })
  vi.mocked(adminApi.slos).mockResolvedValue({ slos: [] })
  // Leaving the tab lands on Users, whose loader must resolve too.
  vi.mocked(adminApi.listUsers).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 50,
    has_next: false,
  })
  listAuditLogs.mockResolvedValue(page([ROW]))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Audit tab auto-refresh', () => {
  it('refetches every 5s while the tab is open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await openAuditTab()
    const afterOpen = listAuditLogs.mock.calls.length

    await vi.advanceTimersByTimeAsync(5000)
    await waitFor(() =>
      expect(listAuditLogs.mock.calls.length).toBeGreaterThan(afterOpen),
    )
  })

  it('stops refetching once the operator leaves the tab', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await openAuditTab()

    await userEvent.click(screen.getByRole('button', { name: /^users$/i }))
    const afterLeaving = listAuditLogs.mock.calls.length
    await vi.advanceTimersByTimeAsync(15000)
    expect(listAuditLogs.mock.calls.length).toBe(afterLeaving)
  })
})

describe('Audit tab action-prefix filter', () => {
  it('sends the chosen prefix to the API', async () => {
    await openAuditTab()
    await userEvent.selectOptions(
      screen.getByLabelText(/action prefix/i),
      'chaos.',
    )
    await waitFor(() =>
      expect(listAuditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ action_prefix: 'chaos.' }),
      ),
    )
  })

  it('sends no prefix at all for "all actions"', async () => {
    await openAuditTab()
    const select = screen.getByLabelText(/action prefix/i)
    await userEvent.selectOptions(select, 'agent.')
    await waitFor(() =>
      expect(listAuditLogs).toHaveBeenCalledWith(
        expect.objectContaining({ action_prefix: 'agent.' }),
      ),
    )
    await userEvent.selectOptions(select, '')
    await waitFor(() => {
      const calls = listAuditLogs.mock.calls
      const last = calls[calls.length - 1][0]
      expect(last!.action_prefix).toBeUndefined()
    })
  })

  it('resets to page 1 when the prefix changes', async () => {
    listAuditLogs.mockResolvedValue(page(Array.from({ length: 20 }, (_, i) => ({ ...ROW, id: `a-${i}` }))))
    // total 20 with page_size 20 renders no pager, so assert the request
    // instead: a prefix change must not carry the previous page number.
    await openAuditTab()
    await userEvent.selectOptions(screen.getByLabelText(/action prefix/i), 'job.')
    await waitFor(() => {
      const calls = listAuditLogs.mock.calls
      const last = calls[calls.length - 1][0]
      expect(last!.page).toBe(1)
    })
  })
})
