/**
 * Tests for useJobStream — the SSE hook that drives live job progress.
 *
 * Covers:
 *  - Initial state
 *  - The EventSource URL carries a freshly minted stream token, never the
 *    primary access JWT (the F2-01/F2-03 transport fix)
 *  - Receiving progress events
 *  - Terminal event closes the stream and marks done
 *  - Error triggers reconnect (via setTimeout mock) with a FRESH stream token
 *  - Does NOT reconnect after a terminal event
 *  - Cleans up EventSource on unmount, including a token fetch still in flight
 */

import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useJobStream } from '../hooks/useJobStream'
import { jobsApi } from '../api/jobs'
import { MockEventSource } from './setup'

// The hook must never read the primary access JWT for the stream URL. If a
// regression reintroduces getAccessToken() there, this recognizable value
// would leak into the URL and the assertions below would catch it.
vi.mock('../utils/tokens', () => ({
  getAccessToken: () => 'primary-access-jwt',
}))

vi.mock('../api/jobs', () => ({
  jobsApi: {
    streamToken: vi.fn(),
  },
}))

const streamTokenMock = vi.mocked(jobsApi.streamToken)

const JOB_ID = 'job-abc-123'
const STREAM_TOKEN = 'fake-stream-token'

function latest() {
  return MockEventSource.instances[MockEventSource.instances.length - 1]
}

/**
 * Render the hook and flush microtasks: connect() awaits the stream-token
 * POST before constructing the EventSource, so the instance only exists
 * after the mocked promise resolves.
 */
async function renderStream(jobId: string | null = JOB_ID) {
  const utils = renderHook(() => useJobStream(jobId))
  await act(async () => {})
  return utils
}

beforeEach(() => {
  MockEventSource.instances = []
  streamTokenMock.mockReset()
  streamTokenMock.mockResolvedValue(STREAM_TOKEN)
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useJobStream', () => {
  it('starts disconnected with empty state', async () => {
    const { result } = await renderStream()
    expect(result.current.connected).toBe(false)
    expect(result.current.events).toHaveLength(0)
    expect(result.current.latest).toBeNull()
    expect(result.current.done).toBe(false)
  })

  it('opens the stream with a minted stream token, not the access JWT', async () => {
    await renderStream()
    expect(streamTokenMock).toHaveBeenCalledWith(JOB_ID)
    const url = latest().url
    expect(url).toContain(`/jobs/${JOB_ID}/stream?token=${STREAM_TOKEN}`)
    expect(url).not.toContain('primary-access-jwt')
  })

  it('marks connected on open', async () => {
    const { result } = await renderStream()
    act(() => { latest().simulateOpen() })
    expect(result.current.connected).toBe(true)
  })

  it('appends events and tracks latest', async () => {
    const { result } = await renderStream()
    act(() => { latest().simulateOpen() })

    act(() => {
      latest().simulateMessage({
        job_id: JOB_ID, status: 'running', progress: 25,
        message: 'Processing…', retry_count: 0, timestamp: new Date().toISOString(),
      })
    })

    expect(result.current.events).toHaveLength(1)
    expect(result.current.latest?.progress).toBe(25)
    expect(result.current.latest?.status).toBe('running')
  })

  it('marks done and disconnects on terminal event', async () => {
    const { result } = await renderStream()
    act(() => { latest().simulateOpen() })

    act(() => {
      latest().simulateMessage({
        job_id: JOB_ID, status: 'completed', progress: 100,
        message: 'Done', retry_count: 0, timestamp: new Date().toISOString(),
      })
    })

    expect(result.current.done).toBe(true)
    expect(result.current.connected).toBe(false)
  })

  it('reconnects after error if not done, with a FRESH stream token', async () => {
    await renderStream()
    const firstEs = latest()

    // The first token has likely expired by reconnect time (60s TTL) —
    // the hook must mint a new one rather than reuse the stale one.
    streamTokenMock.mockResolvedValue('second-stream-token')

    act(() => { firstEs.simulateError() })

    // Advance past the 2s reconnect delay, then flush the token fetch
    await act(async () => { vi.advanceTimersByTime(2500) })
    await act(async () => {})

    // A new, different EventSource instance should have been created
    expect(latest()).not.toBe(firstEs)
    expect(streamTokenMock).toHaveBeenCalledTimes(2)
    expect(latest().url).toContain('token=second-stream-token')
  })

  it('does NOT reconnect after a terminal event', async () => {
    const { result } = await renderStream()
    act(() => { latest().simulateOpen() })

    // Terminal event — marks done
    act(() => {
      latest().simulateMessage({
        job_id: JOB_ID, status: 'failed', progress: 0,
        message: 'Error', retry_count: 3, timestamp: new Date().toISOString(),
      })
    })
    expect(result.current.done).toBe(true)

    const countAfterTerminal = MockEventSource.instances.length
    const mintsAfterTerminal = streamTokenMock.mock.calls.length

    // Error fires after terminal — should NOT spawn a new connection
    act(() => { latest()?.simulateError() })
    await act(async () => { vi.advanceTimersByTime(2500) })

    expect(MockEventSource.instances.length).toBe(countAfterTerminal)
    expect(streamTokenMock.mock.calls.length).toBe(mintsAfterTerminal)
  })

  it('ignores malformed event data without crashing', async () => {
    const { result } = await renderStream()
    act(() => { latest().simulateOpen() })

    act(() => {
      // Bypass simulateMessage to send raw bad data
      latest().onmessage?.(new MessageEvent('message', { data: '{not valid json' }))
    })

    expect(result.current.events).toHaveLength(0)
    expect(result.current.done).toBe(false)
  })

  it('closes EventSource on unmount', async () => {
    const { unmount } = await renderStream()
    const es = latest()
    const closeSpy = vi.spyOn(es, 'close')
    unmount()
    expect(closeSpy).toHaveBeenCalled()
  })

  it('does not open a stream if unmounted while the token fetch is in flight', async () => {
    let resolveToken: (t: string) => void = () => {}
    streamTokenMock.mockReset()
    streamTokenMock.mockImplementationOnce(
      () => new Promise<string>((resolve) => { resolveToken = resolve }),
    )

    const { unmount } = renderHook(() => useJobStream(JOB_ID))
    unmount()

    await act(async () => { resolveToken('late-token') })
    expect(MockEventSource.instances).toHaveLength(0)
  })

  it('does nothing when jobId is null', async () => {
    await renderStream(null)
    expect(MockEventSource.instances).toHaveLength(0)
    expect(streamTokenMock).not.toHaveBeenCalled()
  })
})
