/**
 * Tests for the admin DLQ tab's purple `LLM` badge (F2-16).
 *
 * The badge used to render on `retry_count < job.max_retries`, reading
 * "retries were cut short, so the LLM retry policy must have done it". Two
 * populations break that inference and were badged with LLM features off:
 * saga compensation jobs, which dead-letter immediately at retry_count=0
 * because they have no registered processor, and the dispatcher's
 * force-dead-letter safety net.
 *
 * The badge now reads the persisted attribution the dispatcher writes,
 * `jobs.dead_lettered_by`, surfaced on the REST JobResponse.
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import AdminPage from '../pages/AdminPage'
import { ToastProvider } from '../components/Toast'
import { AuthProvider } from '../hooks/useAuth'
import { adminApi } from '../api/admin'
import type { Job } from '../types'

vi.mock('../api/admin', () => ({
  adminApi: {
    dlqStats: vi.fn(),
    listJobs: vi.fn(),
    systemStats: vi.fn(),
    slos: vi.fn(),
  },
}))

const listJobsMock = vi.mocked(adminApi.listJobs)
const dlqStatsMock = vi.mocked(adminApi.dlqStats)

function makeDlqJob(overrides: Partial<Job>): Job {
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

async function renderDlqTab(jobs: Job[]) {
  listJobsMock.mockResolvedValue({
    items: jobs,
    total: jobs.length,
    page: 1,
    page_size: 20,
    has_next: false,
  })

  render(
    <MemoryRouter>
      <AuthProvider>
        <ToastProvider>
          <AdminPage />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  )

  await userEvent.click(screen.getByRole('button', { name: /DLQ/ }))
  await waitFor(() => expect(listJobsMock).toHaveBeenCalled())
}

describe('admin DLQ tab LLM badge', () => {
  beforeEach(() => {
    dlqStatsMock.mockReset()
    listJobsMock.mockReset()
    dlqStatsMock.mockResolvedValue({ total: 1, by_type: {} })
    // AdminPage opens on the overview tab and polls these; stub them so the
    // mount is quiet and only the DLQ assertions matter.
    vi.mocked(adminApi.systemStats).mockResolvedValue({ by_status: {} })
    vi.mocked(adminApi.slos).mockResolvedValue({ slos: [] })
  })

  it('badges a job the LLM retry policy dead-lettered', async () => {
    await renderDlqTab([
      makeDlqJob({ retry_count: 1, dead_lettered_by: 'llm_retry_policy' }),
    ])

    expect(await screen.findByText('LLM')).toBeDefined()
  })

  it('does not badge a compensation job that dead-lettered at retry_count=0', async () => {
    // retry_count(0) < max_retries(3) — the exact shape the old arithmetic
    // mislabelled. No processor was registered for the `.compensate` type,
    // which has nothing to do with the LLM retry policy.
    await renderDlqTab([
      makeDlqJob({ retry_count: 0, max_retries: 3, dead_lettered_by: null }),
    ])

    await waitFor(() => expect(screen.getByText('0/3')).toBeDefined())
    expect(screen.queryByText('LLM')).toBeNull()
  })

  it('does not badge a job whose retries simply ran out', async () => {
    await renderDlqTab([
      makeDlqJob({ retry_count: 3, max_retries: 3, dead_lettered_by: null }),
    ])

    await waitFor(() => expect(screen.getByText('3/3')).toBeDefined())
    expect(screen.queryByText('LLM')).toBeNull()
  })
})
