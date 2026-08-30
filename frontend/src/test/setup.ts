import '@testing-library/react'

/**
 * jsdom doesn't implement EventSource — provide a minimal mock so
 * useJobStream tests can control open/message/error events.
 *
 * Two properties this mock has to get right:
 *
 *  - **Closed instances stay in `instances`.** It used to remove itself on
 *    `close()`, which meant a test that reached for "the last EventSource"
 *    after a terminal event got `undefined`, and the optional-chained
 *    `latest()?.simulateError()` in the "does NOT reconnect after a terminal
 *    event" test was a silent no-op — the test never fired the event it was
 *    named for and would have passed with the reconnect guard deleted. Tests
 *    that need "how many sockets are live" read `openInstances` instead.
 *  - **Named events are dispatched by name.** The backend sends
 *    `event: <status>` (backend/app/api/streaming.py), and a real EventSource
 *    routes those only to `addEventListener(status, ...)` — never to
 *    `onmessage`. A mock that called `onmessage` for everything would hide
 *    exactly that mismatch.
 */
class MockEventSource {
  static instances: MockEventSource[] = []

  url: string
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null

  private listeners = new Map<string, ((e: MessageEvent) => void)[]>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  /** Sockets that have not been closed — the live-connection count. */
  static get openInstances(): MockEventSource[] {
    return MockEventSource.instances.filter((i) => !i.closed)
  }

  addEventListener(type: string, fn: (e: MessageEvent) => void) {
    const existing = this.listeners.get(type)
    if (existing) existing.push(fn)
    else this.listeners.set(type, [fn])
  }

  removeEventListener(type: string, fn: (e: MessageEvent) => void) {
    const existing = this.listeners.get(type)
    if (existing) this.listeners.set(type, existing.filter((f) => f !== fn))
  }

  close() {
    this.closed = true
  }

  // Test helpers — call these to simulate server events
  simulateOpen() { this.onopen?.() }

  /**
   * Deliver a progress event the way the server does: as a named event whose
   * name is the event's `status`.
   */
  simulateMessage(data: { status?: string } & Record<string, unknown>) {
    this.dispatch(String(data.status), JSON.stringify(data))
  }

  /** Raw dispatch, for malformed payloads and hand-picked event names. */
  dispatch(eventName: string, raw: string) {
    const event = new MessageEvent(eventName, { data: raw })
    const named = this.listeners.get(eventName)
    if (named && named.length > 0) {
      named.forEach((fn) => fn(event))
      return
    }
    // No listener for this name — a real EventSource falls back to onmessage
    // only for unnamed / `message` events, and so does this.
    if (eventName === 'message') this.onmessage?.(event)
  }

  simulateError() { this.onerror?.() }
}

// Expose on globalThis so the hook picks it up
;(globalThis as unknown as Record<string, unknown>).EventSource = MockEventSource
export { MockEventSource }
