/**
 * The one data-loading path for list pages.
 *
 * Every list loader used to be an ad-hoc `async function load()` with a
 * `try/finally` that only reset the spinner. A rejected request therefore did
 * two bad things at once: it escaped as an unhandled promise rejection, and it
 * left the page rendering its *empty* state — "No jobs found", "No sagas yet —
 * start one to see it appear here" — which tells the operator the opposite of
 * what happened.
 *
 * This hook owns data + loading + error together so a page cannot render an
 * empty state it did not earn. The rule callers must follow when rendering:
 *
 *     loading  → skeleton
 *     error    → <ErrorState onRetry={reload} />
 *     rows = 0 → empty state          ← only reachable after a 2xx response
 *     else     → the table
 *
 * Two details that matter:
 *
 *  - `loader` must be memoized (useCallback). Its *identity* is the cache key:
 *    a new identity means "different query", which re-fetches and reports
 *    `loading` from the very first render, before the effect has even run.
 *    Without that, a tab switch would flash the empty state for one frame.
 *  - Results are matched against a run ticket, so a slow response for a query
 *    the user has already moved on from (or unmounted) is discarded instead of
 *    overwriting the current one.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { AppError } from '../api/client'

/** Prefer the backend's own message; fall back to the caller's wording. */
export function loadErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof AppError) return err.message
  if (err instanceof Error && err.message) return err.message
  return fallback
}

export interface AsyncData<T> {
  data: T | null
  loading: boolean
  error: string | null
  /** Re-run the loader — the Retry button, and any post-mutation refresh. */
  reload: () => void
  /** Local, optimistic edits to already-loaded data (e.g. a saved row). */
  setData: Dispatch<SetStateAction<T | null>>
}

export interface AsyncDataOptions {
  /** False parks the loader without running it — used for inactive tabs. */
  enabled?: boolean
  /** Shown when the rejection carries no message of its own. */
  errorMessage?: string
}

/** Sentinel: distinguishes "never loaded" from a loader that returned null. */
const NEVER_LOADED = Symbol('never-loaded')

export function useAsyncData<T>(
  loader: () => Promise<T>,
  options: AsyncDataOptions = {},
): AsyncData<T> {
  const { enabled = true, errorMessage: fallback = 'Could not load this data.' } = options

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inFlight, setInFlight] = useState(false)
  const [loadedFor, setLoadedFor] = useState<unknown>(NEVER_LOADED)
  const runIdRef = useRef(0)

  const reload = useCallback(() => {
    const runId = ++runIdRef.current
    setInFlight(true)
    loader().then(
      (result) => {
        if (runIdRef.current !== runId) return
        setData(result)
        setError(null)
        setInFlight(false)
        setLoadedFor(() => loader)
      },
      (err: unknown) => {
        // The rejection stops here. It becomes an error state the operator can
        // see and retry, never an unhandled rejection plus a blank table.
        if (runIdRef.current !== runId) return
        setError(loadErrorMessage(err, fallback))
        setInFlight(false)
        setLoadedFor(() => loader)
      },
    )
  }, [loader, fallback])

  useEffect(() => {
    if (!enabled) return
    reload()
    // Invalidate the in-flight run: its result belongs to a query (or a
    // mounted component) that no longer exists.
    return () => {
      runIdRef.current += 1
    }
  }, [enabled, reload])

  return {
    data,
    error,
    // `loadedFor !== loader` covers the gap between a query changing and the
    // effect firing, so the caller never sees "not loading, no rows" for a
    // query that has not been answered yet.
    loading: inFlight || (enabled && loadedFor !== loader),
    reload,
    setData,
  }
}
