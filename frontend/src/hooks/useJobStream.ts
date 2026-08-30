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
//
// Exported because the job detail page has to make the same call — it opens a
// stream for every NON-terminal status, and a second copy of this list would
// be a second thing to forget to update.
export const TERMINAL_STATUSES = new Set(['completed', 'failed', 'dead_letter', 'cancelled'])

/**
 * The SSE event names the backend emits.
 *
 * The stream does NOT send unnamed messages: both yield sites in
 * backend/app/api/streaming.py set `"event": event.status`, so the wire
 * carries `event: running`, `event: completed`, and so on. EventSource routes
 * a named event only to a listener registered for that name — `onmessage`
 * fires for `event: message` and nothing else — so a hook that listened on
 * `onmessage` alone connected successfully and then received nothing at all.
 *
 * `running`/`completed` come from _EVENT_TO_STATUS in workers/sse_consumer.py,
 * `retrying`/`dead_letter` from the job.failed split there, and `failed`/
 * `cancelled` from the synthetic terminal event the endpoint builds off the
 * job row. `onmessage` is still wired up, so an unnamed event would also be
 * handled if the server ever sends one.
 */
const STREAM_EVENT_NAMES = [
  'running',
  'retrying',
  'completed',
  'failed',
  'dead_letter',
  'cancelled',
] as const

const BASE_URL = import.meta.env.VITE_API_URL ?? '/api/v1'

function emptyState(): StreamState {
  return { events: [], latest: null, connected: false, done: false }
}

// Reconnect backoff. The first retry stays at 2s — a dropped connection on a
// live job should recover promptly — but consecutive failures back off to a
// 30s ceiling instead of retrying forever at 2s.
//
// The server now caps concurrent streams per process and answers 503 with
// Retry-After past it (see backend/app/workers/progress_broker.py). EventSource
// exposes neither the status code nor that header to onerror, so the client
// cannot read the server's advice; a flat 2s retry would turn the cap into a
// hot loop of refused reconnects from exactly the viewers who could not get in.
// Backing off is the client-side half of that contract. A successful open
// resets the delay, so a healthy stream that blips later still retries fast.
const RECONNECT_BASE_MS = 2000
const RECONNECT_MAX_MS = 30000

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
 * minting a fresh stream token each time (the old one expires in ~60s).  The
 * first retry is prompt (2s) and consecutive failures back off to a 30s
 * ceiling, so a server at its concurrent-stream cap is not hammered by the
 * viewers it just refused; a successful open resets the delay.
 *
 * Switching `jobId` resets everything: events, latest, done and connected all
 * go back to empty before the new job's first event arrives, so a mounted
 * consumer that moves between jobs never shows the previous job's log.
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
  const [state, setState] = useState<StreamState>(emptyState)
  const [streamedJobId, setStreamedJobId] = useState<string | null>(jobId)

  // Reset on job change. Done during render — React's documented pattern for
  // "a prop changed, derived state must follow" — rather than in an effect,
  // so a consumer that switches jobs never renders the previous job's event
  // log, done flag or progress, not even for the single frame between the
  // change and the effect running.
  if (streamedJobId !== jobId) {
    setStreamedJobId(jobId)
    setState(emptyState)
  }

  useEffect(() => {
    if (!jobId) return
    const id = jobId

    // Per-invocation state. Fresh on every effect run, discarded by cleanup.
    let disposed = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let es: EventSource | null = null
    let sawTerminal = false
    let reconnectDelay = RECONNECT_BASE_MS

    function scheduleReconnect() {
      if (disposed || sawTerminal) return
      const delay = reconnectDelay
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS)
      timer = setTimeout(() => {
        timer = null
        void connect()
      }, delay)
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
        // The stream is live — forget any accumulated backoff.
        reconnectDelay = RECONNECT_BASE_MS
        setState((s) => ({ ...s, connected: true }))
      }

      const handleEvent = (e: MessageEvent) => {
        let event: ProgressEvent
        try {
          event = JSON.parse(e.data as string) as ProgressEvent
        } catch {
          return // ignore malformed events
        }
        const isTerminal = TERMINAL_STATUSES.has(event.status)
        if (isTerminal) {
          // Recorded in the closure, not read back out of state: deciding a
          // side effect inside an updater is the bug this hook used to have.
          sawTerminal = true
          source.close()
        }
        setState((s) => {
          // The endpoint replays its retained snapshot to every subscriber,
          // including on reconnect, and the broker subscribes fractionally
          // before reading it — so the same event can legitimately arrive
          // twice in a row. `latest` is unaffected either way, but the event
          // log would show the line twice.
          const previous = s.events[s.events.length - 1]
          const isRepeat =
            previous !== undefined &&
            previous.timestamp === event.timestamp &&
            previous.status === event.status &&
            previous.progress === event.progress
          return {
            events: isRepeat ? s.events : [...s.events, event],
            latest: event,
            connected: !isTerminal,
            done: isTerminal,
          }
        })
      }

      // Named events are how the backend actually speaks; onmessage covers an
      // unnamed one. See STREAM_EVENT_NAMES.
      for (const name of STREAM_EVENT_NAMES) {
        source.addEventListener(name, handleEvent as EventListener)
      }
      source.onmessage = handleEvent

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
