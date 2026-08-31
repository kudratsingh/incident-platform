/**
 * The copy button has to work on the deployed stack, which serves plain HTTP.
 *
 * `navigator.clipboard` is a secure-context-only API. On http:// it is
 * `undefined`, so the old unguarded `navigator.clipboard.writeText(value)`
 * threw a TypeError inside the click handler: nothing was copied and the
 * success toast never rendered either, because the throw happened first. The
 * "permission denied" case was worse — the promise rejected unhandled and the
 * success toast *did* render, telling the user their trace ID was on the
 * clipboard when it was not.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TraceId from '../components/TraceId'
import { ToastProvider } from '../components/Toast'

function renderTraceId() {
  return render(
    <ToastProvider>
      <TraceId label="Trace ID" value="abc-123-def" />
    </ToastProvider>,
  )
}

/** Replace navigator.clipboard, which jsdom leaves undefined by default. */
function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', {
    value,
    configurable: true,
    writable: true,
  })
}

afterEach(() => {
  setClipboard(undefined)
  vi.restoreAllMocks()
})

describe('TraceId copy', () => {
  it('uses the Clipboard API when it is available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    setClipboard({ writeText })

    renderTraceId()
    await userEvent.click(screen.getByRole('button', { name: /copy/i }))

    expect(writeText).toHaveBeenCalledWith('abc-123-def')
    expect(await screen.findByText(/Copied Trace ID/)).toBeTruthy()
  })

  it('falls back to execCommand in a non-secure context and reports success', async () => {
    // http:// — the ALB's posture until HTTPS/ACM lands in Phase 8.
    setClipboard(undefined)
    const exec = vi.fn().mockReturnValue(true)
    document.execCommand = exec

    renderTraceId()
    await userEvent.click(screen.getByRole('button', { name: /copy/i }))

    expect(exec).toHaveBeenCalledWith('copy')
    expect(await screen.findByText(/Copied Trace ID/)).toBeTruthy()
  })

  it('reports failure instead of claiming a copy that did not happen', async () => {
    setClipboard({ writeText: vi.fn().mockRejectedValue(new Error('denied')) })
    const exec = vi.fn().mockReturnValue(false)
    document.execCommand = exec

    renderTraceId()
    await userEvent.click(screen.getByRole('button', { name: /copy/i }))

    expect(await screen.findByText(/Could not copy Trace ID/)).toBeTruthy()
    // The success message must not appear alongside the failure.
    await waitFor(() => {
      expect(screen.queryByText(/Copied Trace ID/)).toBeNull()
    })
  })

  it('does not throw when the clipboard object is missing entirely', async () => {
    setClipboard(undefined)
    // No execCommand either — the harshest environment.
    document.execCommand = undefined as unknown as typeof document.execCommand

    renderTraceId()
    // The assertion is that this click resolves rather than throwing.
    await userEvent.click(screen.getByRole('button', { name: /copy/i }))

    expect(await screen.findByText(/Could not copy Trace ID/)).toBeTruthy()
  })
})
