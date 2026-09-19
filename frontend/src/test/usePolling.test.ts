/**
 * `usePolling` — the one repeated-load path (WO-R3-313).
 *
 * The console already had two ways to keep a panel fresh: `useAsyncData` plus a
 * page-local `setInterval` (the Overview tab), and `useJobStream` for SSE. The
 * /demo page needs three panels on the same 2s cadence, so the interval half is
 * hoisted here instead of being written three more times.
 *
 * What the tests pin: it keeps every `useAsyncData` guarantee (loading /error/
 * empty cannot be confused), it stops polling when disabled and when unmounted,
 * and a poll that fails leaves the last good data on screen with the error
 * beside it rather than blanking the panel — an operator watching a live demo
 * must not lose the last reading to one dropped request.
 */

import { useCallback } from 'react'
import { renderHook, waitFor, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { usePolling } from '../hooks/usePolling'

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('usePolling', () => {
  it('loads once, then again on every interval tick', async () => {
    let n = 0
    const loader = vi.fn(() => Promise.resolve(++n))
    const { result } = renderHook(() => usePolling(loader, 2000))

    await waitFor(() => expect(result.current.data).toBe(1))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    await waitFor(() => expect(result.current.data).toBe(2))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    await waitFor(() => expect(result.current.data).toBe(3))
  })

  it('does not poll while disabled', async () => {
    const loader = vi.fn(() => Promise.resolve('x'))
    renderHook(() => usePolling(loader, 1000, { enabled: false }))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(loader).not.toHaveBeenCalled()
  })

  it('stops polling once unmounted', async () => {
    const loader = vi.fn(() => Promise.resolve('x'))
    const { unmount } = renderHook(() => usePolling(loader, 1000))
    await waitFor(() => expect(loader).toHaveBeenCalledTimes(1))

    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(loader).toHaveBeenCalledTimes(1)
  })

  it('keeps the last good reading when one poll fails', async () => {
    const loader = vi
      .fn<() => Promise<string>>()
      .mockResolvedValueOnce('good')
      .mockRejectedValueOnce(new Error('network blip'))
    const { result } = renderHook(() => usePolling(loader, 1000))

    await waitFor(() => expect(result.current.data).toBe('good'))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    await waitFor(() => expect(result.current.error).toBe('network blip'))
    // The panel still has something true to show.
    expect(result.current.data).toBe('good')
  })

  it('re-fetches immediately when the query changes, not on the next tick', async () => {
    // Same contract as useAsyncData: the loader's identity IS the cache key, so
    // callers memoize it and a new identity means a new query.
    const loader = vi.fn((q: string) => Promise.resolve(q))
    const { result, rerender } = renderHook(
      ({ q }: { q: string }) => {
        const load = useCallback(() => loader(q), [q])
        return usePolling(load, 60_000)
      },
      { initialProps: { q: 'a' } },
    )
    await waitFor(() => expect(result.current.data).toBe('a'))

    rerender({ q: 'b' })
    await waitFor(() => expect(result.current.data).toBe('b'))
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('reports loading only until the first reading lands', async () => {
    // A 2s poll must not flash a skeleton every 2s: the panels key their
    // skeleton off "loading AND nothing loaded yet", which needs `data` to
    // survive an in-flight refresh.
    let n = 0
    const loader = vi.fn(() => Promise.resolve(++n))
    const { result } = renderHook(() => usePolling(loader, 1000))

    expect(result.current.loading).toBe(true)
    expect(result.current.data).toBeNull()
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.data).toBe(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    await waitFor(() => expect(result.current.data).toBe(2))
    expect(result.current.error).toBeNull()
  })
})
