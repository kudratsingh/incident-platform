/**
 * One refresh attempt must not poison every later one (R2-72).
 *
 * `tryRefresh` cached its in-flight promise in a module-level
 * `_refreshPromise` to avoid parallel refresh storms, and cleared it in a
 * `finally`. But `if (!refreshToken) return false` sat OUTSIDE that try, so
 * with no stored refresh token the function returned a promise that resolved
 * `false` and was never reset — after one such attempt, every later 401
 * short-circuited to the cached `false` and the client stopped even trying to
 * refresh for the lifetime of the page.
 *
 * Each test imports a fresh copy of the client module, because the latch being
 * tested is module state.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

function response(status: number, body: unknown): Response {
  return {
    ok: status < 400,
    status,
    headers: { get: () => null },
    json: async () => body,
  } as unknown as Response
}

const EXPIRED = { error_code: 'authentication_failed', message: 'Token expired' }

const fetchMock = vi.fn<typeof fetch>()

/** A client module with its own, untouched `_refreshPromise`. */
async function freshClient() {
  vi.resetModules()
  return await import('../api/client')
}

function urlsCalled(): string[] {
  return fetchMock.mock.calls.map((c) => String(c[0]))
}

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  localStorage.clear()
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { href: 'http://localhost:3000/jobs', pathname: '/jobs' },
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('refresh caching', () => {
  it('a refresh attempt with no stored token does not disable later refreshes', async () => {
    const { api } = await freshClient()
    localStorage.setItem('ip_access_token', 'access-1')
    // Deliberately NO refresh token: the attempt must fail without a request.

    fetchMock.mockResolvedValueOnce(response(401, EXPIRED))
    await expect(api.get('/jobs')).rejects.toThrow('Session expired')

    // Nothing was sent to /auth/refresh — there was no token to send.
    expect(urlsCalled().filter((u) => u.includes('/auth/refresh'))).toHaveLength(0)

    // The user logs back in, so a refresh token exists again. The next 401
    // must perform a REAL refresh, not return the cached `false`.
    localStorage.setItem('ip_access_token', 'access-2')
    localStorage.setItem('ip_refresh_token', 'refresh-2')

    fetchMock.mockResolvedValueOnce(response(401, EXPIRED)) // GET /jobs
    fetchMock.mockResolvedValueOnce(
      response(200, { access_token: 'access-3', refresh_token: 'refresh-3', token_type: 'bearer' }),
    ) // POST /auth/refresh
    fetchMock.mockResolvedValueOnce(response(200, { items: [], total: 0 })) // retried GET /jobs

    const result = await api.get<{ items: unknown[] }>('/jobs')

    expect(urlsCalled().filter((u) => u.includes('/auth/refresh'))).toHaveLength(1)
    expect(result.items).toEqual([])
    expect(localStorage.getItem('ip_access_token')).toBe('access-3')
  })

  it('a failed refresh round-trip also leaves the next attempt free to try', async () => {
    const { api } = await freshClient()
    localStorage.setItem('ip_access_token', 'access-1')
    localStorage.setItem('ip_refresh_token', 'refresh-1')

    fetchMock.mockResolvedValueOnce(response(401, EXPIRED)) // GET /jobs
    fetchMock.mockResolvedValueOnce(response(401, EXPIRED)) // POST /auth/refresh — rejected
    await expect(api.get('/jobs')).rejects.toThrow('Session expired')
    expect(urlsCalled().filter((u) => u.includes('/auth/refresh'))).toHaveLength(1)

    localStorage.setItem('ip_access_token', 'access-2')
    localStorage.setItem('ip_refresh_token', 'refresh-2')

    fetchMock.mockResolvedValueOnce(response(401, EXPIRED))
    fetchMock.mockResolvedValueOnce(
      response(200, { access_token: 'access-3', refresh_token: 'refresh-3', token_type: 'bearer' }),
    )
    fetchMock.mockResolvedValueOnce(response(200, { items: [] }))

    await api.get('/jobs')
    expect(urlsCalled().filter((u) => u.includes('/auth/refresh'))).toHaveLength(2)
  })

  it('still collapses concurrent 401s into a single refresh', async () => {
    // The reason the cache exists at all: two calls that 401 together must
    // produce one refresh, not two.
    const { api } = await freshClient()
    localStorage.setItem('ip_access_token', 'access-1')
    localStorage.setItem('ip_refresh_token', 'refresh-1')

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/auth/refresh')) {
        return Promise.resolve(
          response(200, {
            access_token: 'access-2',
            refresh_token: 'refresh-2',
            token_type: 'bearer',
          }),
        )
      }
      // 401 on the first attempt for each caller, 200 once the retry carries
      // the refreshed token.
      const headers = init?.headers as Record<string, string> | undefined
      return Promise.resolve(
        headers?.Authorization === 'Bearer access-2'
          ? response(200, { items: [] })
          : response(401, EXPIRED),
      )
    })

    await Promise.all([api.get('/jobs'), api.get('/sagas')])

    expect(urlsCalled().filter((u) => u.includes('/auth/refresh'))).toHaveLength(1)
  })

  it('a 401 from /auth/refresh itself is not fed back into the interceptor', async () => {
    const { api } = await freshClient()
    localStorage.setItem('ip_access_token', 'access-1')
    localStorage.setItem('ip_refresh_token', 'refresh-1')

    fetchMock.mockResolvedValue(response(401, { error_code: 'token_expired', message: 'Nope' }))

    await expect(api.post('/auth/refresh', { refresh_token: 'refresh-1' })).rejects.toThrow('Nope')
    // Exactly the one call: calling refresh directly must not trigger a
    // refresh of the refresh.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem('ip_refresh_token')).toBe('refresh-1')
  })
})
