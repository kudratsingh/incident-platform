import { api } from './client'
import type { TokenResponse, User } from '../types'

export const authApi = {
  register: (email: string, password: string) =>
    api.post<User>('/auth/register', { email, password }),

  login: (email: string, password: string) =>
    api.post<TokenResponse>('/auth/login', { email, password }),

  refresh: (refreshToken: string) =>
    api.post<TokenResponse>('/auth/refresh', { refresh_token: refreshToken }),

  me: () => api.get<User>('/auth/me'),
}
