/**
 * A job that is still moving must keep moving on screen (R2-74).
 *
 * The detail page opened the SSE stream only for `running`/`pending` and never
 * polled. A job viewed in `waiting` (held on a dependency) or `retrying`
 * (between attempts) therefore sat frozen at its load-time snapshot forever —
 * the operator had to reload the page to discover the job had moved on
 * minutes ago.
 *
 * The page now streams every NON-terminal status, and polls the row whenever
 * it is live but not connected, so neither channel failing leaves the view
 * stuck.
 */

import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import JobDetailPage from '../pages/JobDetailPage'
import { ToastProvider } from '../components/Toast'
import { jobsApi } from '../api/jobs'
import { MockEventSource } from './setup'
import type { Job } from '../types'

vi.mock('../api/jobs', () => ({
  jobsApi: { get: vi.fn(), streamToken: vi.fn() },
}))

vi.mock('../api/admin', () => ({
  adminApi: { jobTimeline: vi.fn(), jobTriage: vi.fn() },
}))

vi.mock('../hooks/useAuth', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
  useAuth: () => ({
    user: {
      id: 'u-1',
      tenant_id: 't-1',
      tenant_slug: 'acme',
      email: 'user@example.com',
      role: 'user',
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

const getJob = vi.mocked(jobsApi.get)
const streamToken = vi.mocked(jobsApi.streamToken)

const JOB_ID = 'job-abc-123'

function job(status: Job['status']): Job {
  return {
    id: JOB_ID,
    user_id: 'u-1',
    type: 'csv_upload',
    status,
    idempotency_key: null,
    payload: null,
    result: null,
    error_message: null,
    retry_count: 0,
    max_retries: 3,
    dead_lettered_by: null,
    priority: 0,
    trace_id: null,
    created_at: '2026-08-13T00:00:00Z',
    started_at: null,
    completed_at: null,
  }
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={[`/jobs/${JOB_ID}`]}>
      <ToastProvider>
        <Routes>
          <Route path="/jobs/:id" element={<JobDetailPage />} />
        </Routes>
      </ToastProvider>
    </MemoryRouter>,
  )
}

function latest() {
  return MockEventSource.instances[MockEventSource.instances.length - 1]
}

beforeEach(() => {
  MockEventSource.instances = []
  getJob.mockReset()
  streamToken.mockReset()
  streamToken.mockResolvedValue('stream-token')
})

afterEach(() => {
  vi.useRealTimers()
})

describe('job detail keeps non-terminal jobs live', () => {
  it('opens the stream for a waiting job and shows it move to running', async () => {
    getJob.mockResolvedValue(job('waiting'))

    renderDetail()
    expect(await screen.findAllByText('Waiting')).not.toHaveLength(0)

    // The stream must be open — `waiting` is not a terminal state.
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1))
    expect(latest().url).toContain(`/jobs/${JOB_ID}/stream`)

    act(() => { latest().simulateOpen() })
    act(() => {
      latest().simulateMessage({
        job_id: JOB_ID, status: 'running', progress: 30,
        message: 'Processing…', retry_count: 0, timestamp: new Date().toISOString(),
      })
    })

    expect(await screen.findAllByText('Running')).not.toHaveLength(0)
    expect(screen.queryByText('Waiting')).toBeNull()
  })

  it('opens the stream for a retrying job too', async () => {
    getJob.mockResolvedValue(job('retrying' as Job['status']))

    renderDetail()
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1))
  })

  it('does not open a stream for a terminal job', async () => {
    getJob.mockResolvedValue(job('completed'))

    renderDetail()
    expect(await screen.findAllByText('Completed')).not.toHaveLength(0)

    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(MockEventSource.instances).toHaveLength(0)
    expect(streamToken).not.toHaveBeenCalled()
  })

  it('polls the row while the job is live but the stream is not connected', async () => {
    // The stream token cannot be minted, so no stream ever connects. The page
    // must still advance rather than sit on its load-time snapshot.
    // Fake timers are installed BEFORE the render, so the poll interval the
    // page arms is one this test controls.
    vi.useFakeTimers()
    streamToken.mockRejectedValue(new Error('no token for you'))
    getJob.mockResolvedValue(job('waiting'))

    renderDetail()
    await act(async () => {})
    expect(getJob).toHaveBeenCalledTimes(1)
    expect(screen.getAllByText('Waiting').length).toBeGreaterThan(0)

    getJob.mockResolvedValue(job('running'))
    await act(async () => { vi.advanceTimersByTime(5000) })
    await act(async () => {})

    expect(getJob.mock.calls.length).toBeGreaterThan(1)
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
    expect(screen.queryByText('Waiting')).toBeNull()
  })

  it('stops polling once the job reaches a terminal state', async () => {
    vi.useFakeTimers()
    streamToken.mockRejectedValue(new Error('no token for you'))
    getJob.mockResolvedValue(job('completed'))

    renderDetail()
    await act(async () => {})
    expect(screen.getAllByText('Completed').length).toBeGreaterThan(0)
    const callsAfterLoad = getJob.mock.calls.length

    await act(async () => { vi.advanceTimersByTime(30000) })
    await act(async () => {})

    expect(getJob.mock.calls.length).toBe(callsAfterLoad)
  })
})
