import { useEffect, useRef, useState } from 'react'
import type { ProgressEvent } from '../types'
import { jobsApi } from '../api/jobs'

interface StreamState {
  events: ProgressEvent[]
  latest: ProgressEvent | null
  connected: boolean
  done: boolean
}

const TERMINAL = new Set(['completed', 'failed', 'dead_letter'])
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
 */
export function useJobStream(jobId: string | null): StreamState {
  const [state, setState] = useState<StreamState>({
    events: [],
    latest: null,
    connected: false,
    done: false,
  })
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!jobId) return
    const id = jobId
    let cancelled = false

    async function connect() {
      let streamToken: string
      try {
        streamToken = await jobsApi.streamToken(id)
      } catch {
        // Could not mint a token (expired session, network blip). Retry on
        // the same cadence as a dropped stream, unless the job already ended.
        if (cancelled) return
        setState((s) => {
          if (!s.done) setTimeout(connect, 2000)
          return { ...s, connected: false }
        })
        return
      }
      if (cancelled) return

      const es = new EventSource(`${BASE_URL}/jobs/${id}/stream?token=${streamToken}`)
      esRef.current = es

      es.onopen = () => {
        setState((s) => ({ ...s, connected: true }))
      }

      es.onmessage = (e) => {
        try {
          const event: ProgressEvent = JSON.parse(e.data as string)
          setState((s) => {
            const done = TERMINAL.has(event.status)
            return {
              events: [...s.events, event],
              latest: event,
              connected: !done,
              done,
            }
          })
          if (TERMINAL.has(event.status)) {
            es.close()
          }
        } catch {
          // ignore malformed events
        }
      }

      es.onerror = () => {
        es.close()
        setState((s) => ({ ...s, connected: false }))
        // Reconnect after 2s if not terminal
        setState((s) => {
          if (!s.done) setTimeout(connect, 2000)
          return s
        })
      }
    }

    connect()

    return () => {
      cancelled = true
      esRef.current?.close()
    }
  }, [jobId])

  return state
}
