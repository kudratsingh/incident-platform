/**
 * Token storage utilities.
 *
 * Keeps access + refresh tokens in localStorage.  In a production app these
 * would live in httpOnly cookies set by the server, but localStorage is fine
 * for a demo/portfolio project and makes the auth flow visible in DevTools.
 */

const ACCESS_KEY = 'ip_access_token'
const REFRESH_KEY = 'ip_refresh_token'

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY)
}

export function setTokens(access: string, refresh: string): void {
  localStorage.setItem(ACCESS_KEY, access)
  localStorage.setItem(REFRESH_KEY, refresh)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

export function isLoggedIn(): boolean {
  return !!getAccessToken()
}
