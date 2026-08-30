/**
 * A failed list load must render an error, not an empty list (R2-71).
 *
 * Before this, none of the list loaders had a `catch`. A rejected request
 * therefore escaped as an unhandled promise rejection AND left the page
 * rendering its empty state — "No jobs found", "No sagas yet — start one to
 * see it appear here", "Dead letter queue is empty" — each of which is a claim
 * about the data that a failed request cannot support.
 *
 * Every case here asserts the same two things: the error state is on screen,
 * and the empty-state copy is not.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import DashboardPage from '../pages/DashboardPage'
import SagasPage from '../pages/SagasPage'
import AdminPage from '../pages/AdminPage'
import { ToastProvider } from '../components/Toast'
import { AuthProvider } from '../hooks/useAuth'
import { jobsApi } from '../api/jobs'
import { sagasApi } from '../api/sagas'
import { adminApi } from '../api/admin'
import { AppError } from '../api/client'
import type { Job } from '../types'

vi.mock('../api/jobs', () => ({ jobsApi: { list: vi.fn(), create: vi.fn(), get: vi.fn() } }))
vi.mock('../api/sagas', () => ({ sagasApi: { list: vi.fn() } }))
vi.mock('../api/admin', () => ({
  adminApi: {
    listJobs: vi.fn(),
    dlqStats: vi.fn(),
    systemStats: vi.fn(),
    slos: vi.fn(),
  },
}))

const listJobsMock = vi.mocked(jobsApi.list)
const listSagasMock = vi.mocked(sagasApi.list)
const adminListJobsMock = vi.mocked(adminApi.listJobs)

function wrap(ui: React.ReactNode) {
  return (
    <MemoryRouter>
      <AuthProvider>
        <ToastProvider>{ui}</ToastProvider>
      </AuthProvider>
    </MemoryRouter>
  )
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: '11111111-1111-1111-1111-111111111111',
    user_id: '22222222-2222-2222-2222-222222222222',
    type: 'csv_upload',
    status: 'completed',
    idempotency_key: null,
    payload: null,
    result: null,
    error_message: null,
    retry_count: 0,
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

// A rejected load must be handled by the page, so nothing reaches the process.
// Reached through globalThis so the frontend tsconfig needs no node typings.
const nodeProcess = (
  globalThis as unknown as {
    process: {
      on(event: string, cb: (reason: unknown) => void): void
      off(event: string, cb: (reason: unknown) => void): void
    }
  }
).process

const unhandled: unknown[] = []
const captureUnhandled = (reason: unknown) => unhandled.push(reason)

beforeEach(() => {
  unhandled.length = 0
  nodeProcess.on('unhandledRejection', captureUnhandled)
  vi.mocked(adminApi.systemStats).mockResolvedValue({ by_status: {} })
  vi.mocked(adminApi.slos).mockResolvedValue({ slos: [] })
  vi.mocked(adminApi.dlqStats).mockResolvedValue({ total: 0, by_type: {} })
})

afterEach(() => {
  nodeProcess.off('unhandledRejection', captureUnhandled)
  vi.clearAllMocks()
})

/** Give any stray rejection a turn of the loop to surface. */
async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('a failed list load renders an error state, never an empty list', () => {
  it('DashboardPage: a failed GET /jobs does not become "No jobs found"', async () => {
    listJobsMock.mockRejectedValue(
      new AppError('Upstream is unavailable', 'service_unavailable', undefined, 503),
    )

    render(wrap(<DashboardPage />))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Upstream is unavailable')
    expect(screen.queryByText('No jobs found')).toBeNull()

    await flush()
    expect(unhandled).toHaveLength(0)
  })

  it('DashboardPage: Retry re-runs the request and shows the rows', async () => {
    listJobsMock.mockRejectedValueOnce(
      new AppError('Upstream is unavailable', 'service_unavailable', undefined, 503),
    )
    listJobsMock.mockResolvedValueOnce({
      items: [job()],
      total: 1,
      page: 1,
      page_size: 15,
      has_next: false,
    })

    render(wrap(<DashboardPage />))
    await screen.findByRole('alert')

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.getByText('CSV Upload')).toBeDefined()
  })

  it('SagasPage: a failed GET /sagas does not become "No sagas yet"', async () => {
    listSagasMock.mockRejectedValue(
      new AppError('Sagas are unavailable', 'internal_error', undefined, 500),
    )

    render(wrap(<SagasPage />))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Sagas are unavailable')
    expect(screen.queryByText(/No sagas yet/)).toBeNull()

    await flush()
    expect(unhandled).toHaveLength(0)
  })

  it('AdminPage DLQ tab: a failed load does not become "Dead letter queue is empty"', async () => {
    adminListJobsMock.mockRejectedValue(
      new AppError('DLQ query failed', 'internal_error', undefined, 500),
    )

    render(wrap(<AdminPage />))
    await userEvent.click(screen.getByRole('button', { name: /DLQ/ }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('DLQ query failed')
    expect(screen.queryByText('Dead letter queue is empty.')).toBeNull()

    await flush()
    expect(unhandled).toHaveLength(0)
  })

  it('AdminPage overview: a failed GET /admin/stats is reported, not shown as zeros', async () => {
    vi.mocked(adminApi.systemStats).mockRejectedValue(
      new AppError('Stats are unavailable', 'internal_error', undefined, 500),
    )

    render(wrap(<AdminPage />))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Stats are unavailable')

    await flush()
    expect(unhandled).toHaveLength(0)
  })
})
