import { useEffect, useRef, useState } from 'react'
import type { ProgressEvent } from '../types'
import { getAccessToken } from '../utils/tokens'

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
 * The native EventSource API doesn't support custom headers, so we append
 * the token as a query param.  The backend trusts it the same way.
 * (In production you'd use httpOnly cookies instead.)
 *
 * Reconnects automatically if the connection drops before a terminal event.
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

    function connect() {
      const token = getAccessToken()
      const url = `${BASE_URL}/jobs/${jobId}/stream${token ? `?token=${token}` : ''}`
      const es = new EventSource(url)
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
      esRef.current?.close()
    }
  }, [jobId])

  return state
}
