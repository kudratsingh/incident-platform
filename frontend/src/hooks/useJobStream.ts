import { useEffect, useState } from 'react'
import type { ProgressEvent } from '../types'
import { jobsApi } from '../api/jobs'

interface StreamState {
  events: ProgressEvent[]
  latest: ProgressEvent | null
  connected: boolean
  done: boolean
}

// Mirrors TERMINAL_STATUSES in backend/app/workers/progress.py. `cancelled`
// belongs here: the server closes the stream on it (saga rollbacks cancel
// jobs), so treating it as non-terminal would turn every close into a
// reconnect that immediately receives the same event again.
const TERMINAL = new Set(['completed', 'failed', 'dead_letter', 'cancelled'])
const BASE_URL = import.meta.env.VITE_API_URL ?? '/api/v1'

/**
 * Subscribes to the SSE progress stream for a job.
 *
 * The native EventSource API doesn't support custom headers, so before each
 * connect we POST /jobs/{id}/stream-token (a normal fetch, with the usual
 * Authorization header) for a short-lived token bound to this job, and open
 * the EventSource with that token as a query param.  The backend validates
 * it — never the primary access JWT, which must not appear in URLs (ADR 0014).
 *
 * Reconnects automatically if the connection drops before a terminal event,
 * minting a fresh stream token each time (the old one expires in ~60s).
 *
 * Lifecycle: everything the effect owns (disposal flag, pending reconnect
 * timer, live EventSource, terminal-seen flag) lives in closure variables
 * scoped to a single effect invocation, so StrictMode's dev mount → unmount →
 * remount cycle gets a fresh set instead of the throwaway first cycle poisoning
 * the second (as refs would).  setState updaters are pure — the reconnect is
 * scheduled from the error handler off `sawTerminal`, never from inside an
 * updater, which StrictMode double-invokes.
 */
export function useJobStream(jobId: string | null): StreamState {
  const [state, setState] = useState<StreamState>({
    events: [],
    latest: null,
    connected: false,
    done: false,
  })

  useEffect(() => {
    if (!jobId) return
    const id = jobId

    // Per-invocation state. Fresh on every effect run, discarded by cleanup.
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let es: EventSource | null = null
    let sawTerminal = false

    function scheduleReconnect() {
      if (disposed || sawTerminal) return
      timer = setTimeout(() => {
        timer = null
        void connect()
      }, 2000)
    }

    async function connect() {
      if (disposed) return

      let streamToken: string
      try {
        streamToken = await jobsApi.streamToken(id)
      } catch {
        // Could not mint a token (expired session, network blip). Retry on
        // the same cadence as a dropped stream, unless the job already ended.
        if (disposed) return
        setState((s) => ({ ...s, connected: false }))
        scheduleReconnect()
        return
      }
      if (disposed) return

      const source = new EventSource(`${BASE_URL}/jobs/${id}/stream?token=${streamToken}`)
      es = source

      source.onopen = () => {
        setState((s) => ({ ...s, connected: true }))
      }

      source.onmessage = (e) => {
        let event: ProgressEvent
        try {
          event = JSON.parse(e.data as string) as ProgressEvent
        } catch {
          return // ignore malformed events
        }
        const isTerminal = TERMINAL.has(event.status)
        if (isTerminal) {
          // Recorded in the closure, not read back out of state: deciding a
          // side effect inside an updater is the bug this hook used to have.
          sawTerminal = true
          source.close()
        }
        setState((s) => ({
          events: [...s.events, event],
          latest: event,
          connected: !isTerminal,
          done: isTerminal,
        }))
      }

      source.onerror = () => {
        source.close()
        if (disposed || sawTerminal) return
        setState((s) => ({ ...s, connected: false }))
        scheduleReconnect()
      }
    }

    void connect()

    return () => {
      disposed = true
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      es?.close()
      es = null
    }
  }, [jobId])

  return state
}
