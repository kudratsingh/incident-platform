import '@testing-library/react'

// jsdom doesn't implement EventSource — provide a minimal mock
// so useJobStream tests can control open/message/error events.
class MockEventSource {
  static instances: MockEventSource[] = []

  url: string
  onopen: (() => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  close() {
    MockEventSource.instances = MockEventSource.instances.filter((i) => i !== this)
  }

  // Test helpers — call these to simulate server events
  simulateOpen() { this.onopen?.() }
  simulateMessage(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
  simulateError() { this.onerror?.() }
}

// Expose on globalThis so the hook picks it up
;(globalThis as unknown as Record<string, unknown>).EventSource = MockEventSource
export { MockEventSource }
