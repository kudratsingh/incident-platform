/**
 * Tests for SagaNewPage — the step-payload editor (F2-11).
 *
 * The textarea must hold the RAW text the user types and only parse it when
 * the form is submitted. Parsing on every keystroke (the old behaviour) made
 * the field untypeable: every intermediate state of hand-typed JSON is
 * invalid, so the parse threw, the state never advanced and the controlled
 * re-render snapped the DOM value back to the last parseable payload ('{}').
 *
 * Covers:
 *  - Typing a payload character-by-character leaves the typed text in place
 *  - Submitting sends the parsed payload on the wire
 *  - Unparseable payload text blocks submit and shows an inline error
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import SagaNewPage from '../pages/SagaNewPage'
import { ToastProvider } from '../components/Toast'
import { AuthProvider } from '../hooks/useAuth'
import { sagasApi } from '../api/sagas'

vi.mock('../api/sagas', () => ({
  sagasApi: {
    create: vi.fn(),
  },
}))

const createMock = vi.mocked(sagasApi.create)

// userEvent treats '{' as the opener of a special-key descriptor; '{{' types a
// literal '{'. So this is the text `{"file": "x.csv"}` entered one key at a time.
const TYPED_PAYLOAD_KEYS = '{{"file": "x.csv"}'
const TYPED_PAYLOAD_TEXT = '{"file": "x.csv"}'

function renderPage() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <ToastProvider>
          <SagaNewPage />
        </ToastProvider>
      </AuthProvider>
    </MemoryRouter>,
  )
}

function payloadField(): HTMLTextAreaElement {
  return screen.getByPlaceholderText('{"file": "data.csv"}') as HTMLTextAreaElement
}

describe('SagaNewPage payload editor', () => {
  beforeEach(() => {
    createMock.mockReset()
    createMock.mockResolvedValue({
      id: 'saga-1',
      name: 'pipeline',
    } as Awaited<ReturnType<typeof sagasApi.create>>)
  })

  it('keeps every keystroke of a hand-typed payload', async () => {
    const user = userEvent.setup()
    renderPage()

    const textarea = payloadField()
    await user.clear(textarea)
    await user.type(textarea, TYPED_PAYLOAD_KEYS)

    expect(textarea.value).toBe(TYPED_PAYLOAD_TEXT)
  })

  it('parses the payload text once, at submit', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.type(
      screen.getByPlaceholderText('e.g. quarterly-import-pipeline'),
      'pipeline',
    )
    const textarea = payloadField()
    await user.clear(textarea)
    await user.type(textarea, TYPED_PAYLOAD_KEYS)
    await user.click(screen.getByRole('button', { name: 'Create saga' }))

    await waitFor(() => expect(createMock).toHaveBeenCalledTimes(1))
    expect(createMock.mock.calls[0][0]).toEqual({
      name: 'pipeline',
      steps: [{ type: 'csv_upload', payload: { file: 'x.csv' } }],
    })
  })

  it('blocks submit and reports which step has unparseable payload text', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.type(
      screen.getByPlaceholderText('e.g. quarterly-import-pipeline'),
      'pipeline',
    )
    const textarea = payloadField()
    await user.clear(textarea)
    await user.type(textarea, '{{"file": ')
    await user.click(screen.getByRole('button', { name: 'Create saga' }))

    expect(
      await screen.findByText('Step 1 payload must be valid JSON'),
    ).toBeTruthy()
    expect(createMock).not.toHaveBeenCalled()
  })
})
