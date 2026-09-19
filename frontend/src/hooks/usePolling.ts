/**
 * `useAsyncData` on a timer — the one repeated-load path.
 *
 * Two live-data patterns already existed: `useJobStream` (SSE, per job) and
 * `useAsyncData` plus a page-local `setInterval`, written once for the admin
 * Overview tab. The /demo page needs four panels on the same cadence, so the
 * interval half lives here rather than being copied four more times and then
 * drifting in four places.
 *
 * Everything `useAsyncData` guarantees still holds, and two of its properties
 * are what make polling safe rather than merely possible:
 *
 *  - a rejected poll sets `error` and leaves `data` alone, so one dropped
 *    request shows the failure next to the last good reading instead of
 *    blanking a panel mid-demo;
 *  - results are matched against a run ticket, so a slow response cannot land
 *    after the tick that superseded it.
 *
 * The caller's rule is also unchanged: `loader` must be memoized (useCallback)
 * because its identity is the cache key. A new identity means a new query and
 * re-fetches at once rather than waiting out the interval.
 *
 * Render rule for a polled panel: key the skeleton off `loading && data === null`.
 * `loading` goes true on every tick — it means "a request is in flight", not
 * "there is nothing to show" — so keying off `loading` alone makes a healthy
 * panel flash a skeleton every two seconds.
 */

import { useEffect } from 'react'
import { useAsyncData } from './useAsyncData'
import type { AsyncData, AsyncDataOptions } from './useAsyncData'

export function usePolling<T>(
  loader: () => Promise<T>,
  intervalMs: number,
  options: AsyncDataOptions = {},
): AsyncData<T> {
  const { enabled = true } = options
  const state = useAsyncData(loader, options)
  const { reload } = state

  useEffect(() => {
    if (!enabled || intervalMs <= 0) return
    const timer = setInterval(reload, intervalMs)
    return () => clearInterval(timer)
    // `reload`'s identity follows the loader's, so a query change restarts the
    // timer from the fresh fetch the hook has already kicked off — the panel
    // never sits on a stale reading waiting for a tick that belongs to the
    // previous query.
  }, [enabled, intervalMs, reload])

  return state
}
