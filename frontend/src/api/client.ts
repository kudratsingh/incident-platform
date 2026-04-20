/**
 * Base API client.
 *
 * Handles:
 *  - Auth header injection
 *  - Automatic access-token refresh on 401
 *  - Consistent error parsing (ApiError shape from backend)
 *  - X-Request-ID / X-Trace-ID echoed in every response → shown in UI
 *  - X-Trace-ID sent on every request so the backend logger ties browser
 *    sessions to server-side traces end-to-end.  The session trace ID is
 *    generated once per page load; individual request IDs are per-call.
 */

import type { ApiError, TokenResponse } from '../types'
import { getAccessToken, getRefreshToken, setTokens, clearTokens } from '../utils/tokens'

const BASE_URL = import.meta.env.VITE_API_URL ?? '/api/v1'

/** One UUID generated when the JS bundle loads — ties all requests in this
 *  browser tab to a single session trace that the backend echoes back.
 *  Falls back to a Math.random-based ID when crypto.randomUUID is unavailable
 *  (e.g. HTTP non-secure contexts). */
function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

export const SESSION_TRACE_ID = generateId()

export class AppError extends Error {
  constructor(
    message: string,
    public readonly errorCode: string,
    public readonly requestId?: string,
    public readonly statusCode?: number,
  ) {
    super(message)
    this.name = 'AppError'
  }
}

// Track in-flight refresh to avoid parallel refresh storms
let _refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  if (_refreshPromise) return _refreshPromise

  _refreshPromise = (async () => {
    const refreshToken = getRefreshToken()
    if (!refreshToken) return false

    try {
      const res = await fetch(`${BASE_URL}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      if (!res.ok) return false
      const data: TokenResponse = await res.json()
      setTokens(data.access_token, data.refresh_token)
      return true
    } catch {
      return false
    } finally {
      _refreshPromise = null
    }
  })()

  return _refreshPromise
}

export interface RequestMeta {
  requestId?: string
  traceId?: string
}

// Last response correlation IDs — readable by the UI for debugging display
export const lastMeta: RequestMeta = {}

async function request<T>(
  path: string,
  options: RequestInit = {},
  isRetry = false,
): Promise<T> {
  const token = getAccessToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Trace-ID': SESSION_TRACE_ID,
    ...(options.headers as Record<string, string>),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }

  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers })

  // Capture correlation IDs from every response
  lastMeta.requestId = res.headers.get('X-Request-ID') ?? undefined
  lastMeta.traceId = res.headers.get('X-Trace-ID') ?? undefined

  if (res.status === 401 && !isRetry) {
    const refreshed = await tryRefresh()
    if (refreshed) return request<T>(path, options, true)
    clearTokens()
    window.location.href = '/login'
    throw new AppError('Session expired', 'authentication_failed', undefined, 401)
  }

  if (!res.ok) {
    let apiError: ApiError
    try {
      apiError = await res.json()
    } catch {
      apiError = { error_code: 'unknown', message: `HTTP ${res.status}` }
    }
    throw new AppError(apiError.message, apiError.error_code, apiError.request_id, res.status)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
