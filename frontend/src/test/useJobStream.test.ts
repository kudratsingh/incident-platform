/**
 * Tests for useJobStream — the SSE hook that drives live job progress.
 *
 * Covers:
 *  - Initial state
 *  - Receiving progress events
 *  - Terminal event closes the stream and marks done
 *  - Error triggers reconnect (via setTimeout mock)
 *  - Does NOT reconnect after a terminal event
 *  - Cleans up EventSource on unmount
 */

import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useJobStream } from '../hooks/useJobStream'
import { MockEventSource } from './setup'

// Mock localStorage so getAccessToken() returns a token without real storage
vi.mock('../utils/tokens', () => ({
  getAccessToken: () => 'test-token',
}))

const JOB_ID = 'job-abc-123'

function latest() {
  return MockEventSource.instances[MockEventSource.instances.length - 1]
}

beforeEach(() => {
  MockEventSource.instances = []
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useJobStream', () => {
  it('starts disconnected with empty state', () => {
    const { result } = renderHook(() => useJobStream(JOB_ID))
    expect(result.current.connected).toBe(false)
    expect(result.current.events).toHaveLength(0)
    expect(result.current.latest).toBeNull()
    expect(result.current.done).toBe(false)
  })

  it('marks connected on open', () => {
    const { result } = renderHook(() => useJobStream(JOB_ID))
    act(() => { latest().simulateOpen() })
    expect(result.current.connected).toBe(true)
  })

  it('appends events and tracks latest', () => {
    const { result } = renderHook(() => useJobStream(JOB_ID))
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

  it('marks done and disconnects on terminal event', () => {
    const { result } = renderHook(() => useJobStream(JOB_ID))
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

  it('reconnects after error if not done', () => {
    renderHook(() => useJobStream(JOB_ID))
    const firstEs = latest()

    act(() => { firstEs.simulateError() })

    // Advance past the 2s reconnect delay
    act(() => { vi.advanceTimersByTime(2500) })

    // A new, different EventSource instance should have been created
    expect(latest()).not.toBe(firstEs)
  })

  it('does NOT reconnect after a terminal event', () => {
    const { result } = renderHook(() => useJobStream(JOB_ID))
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

    // Error fires after terminal — should NOT spawn a new connection
    act(() => { latest()?.simulateError() })
    act(() => { vi.advanceTimersByTime(2500) })

    expect(MockEventSource.instances.length).toBe(countAfterTerminal)
  })

  it('ignores malformed event data without crashing', () => {
    const { result } = renderHook(() => useJobStream(JOB_ID))
    act(() => { latest().simulateOpen() })

    act(() => {
      // Bypass simulateMessage to send raw bad data
      latest().onmessage?.(new MessageEvent('message', { data: '{not valid json' }))
    })

    expect(result.current.events).toHaveLength(0)
    expect(result.current.done).toBe(false)
  })

  it('closes EventSource on unmount', () => {
    const { unmount } = renderHook(() => useJobStream(JOB_ID))
    const es = latest()
    const closeSpy = vi.spyOn(es, 'close')
    unmount()
    expect(closeSpy).toHaveBeenCalled()
  })

  it('does nothing when jobId is null', () => {
    renderHook(() => useJobStream(null))
    expect(MockEventSource.instances).toHaveLength(0)
  })
})
