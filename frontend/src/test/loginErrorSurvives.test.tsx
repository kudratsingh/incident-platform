/**
 * A failed login must reach the user (R2-72).
 *
 * The 401 interceptor was blanket: any 401, from any endpoint, was treated as
 * an expired session. A wrong password answers 401 from /auth/login, so the
 * interceptor cleared the tokens and hard-navigated to /login — reloading the
 * page and destroying the "Invalid email or password" message the form had
 * just rendered. The user got a blank form and no explanation.
 *
 * These tests drive the real client with `fetch` stubbed, so they prove the
 * whole path rather than a mocked-out middle.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import LoginPage from '../pages/LoginPage'
import { AuthProvider } from '../hooks/useAuth'
import { authApi } from '../api/auth'
import { AppError } from '../api/client'

function response(status: number, body: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response
}

const INVALID_CREDENTIALS = {
  error_code: 'authentication_failed',
  message: 'Invalid email or password',
}

const fetchMock = vi.fn<typeof fetch>()
let startHref = ''

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  localStorage.clear()
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { href: 'http://localhost:3000/login', pathname: '/login' },
  })
  startHref = window.location.href
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('a rejected login reaches the caller intact', () => {
  it('rejects with the server message, does not refresh, navigate or clear tokens', async () => {
    // A real session is in place — re-authenticating as someone else and
    // getting the password wrong must not destroy it.
    localStorage.setItem('ip_access_token', 'access-1')
    localStorage.setItem('ip_refresh_token', 'refresh-1')
    fetchMock.mockResolvedValue(response(401, INVALID_CREDENTIALS))

    await expect(authApi.login('user@example.com', 'wrong')).rejects.toThrow(
      'Invalid email or password',
    )

    // One request: the login itself. No refresh was attempted, because a bad
    // password is not an expired session.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/auth/login')
    expect(window.location.href).toBe(startHref)
    expect(localStorage.getItem('ip_access_token')).toBe('access-1')
    expect(localStorage.getItem('ip_refresh_token')).toBe('refresh-1')
  })

  it('carries the error code and status through, not a synthetic 401', async () => {
    fetchMock.mockResolvedValue(response(401, INVALID_CREDENTIALS))

    const err = await authApi.login('user@example.com', 'wrong').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(AppError)
    expect((err as AppError).errorCode).toBe('authentication_failed')
    expect((err as AppError).statusCode).toBe(401)
  })

  it('LoginPage renders the server message instead of being reloaded away', async () => {
    fetchMock.mockResolvedValue(response(401, INVALID_CREDENTIALS))

    render(
      <MemoryRouter>
        <AuthProvider>
          <LoginPage />
        </AuthProvider>
      </MemoryRouter>,
    )

    await userEvent.type(screen.getByPlaceholderText('you@example.com'), 'user@example.com')
    await userEvent.type(screen.getByPlaceholderText('••••••••'), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByText('Invalid email or password')).toBeDefined()
    expect(screen.queryByText('Session expired')).toBeNull()
    expect(window.location.href).toBe(startHref)
  })
})
