import { api } from './client'
import type { Saga, SagaCreateRequest, SagaStatus } from '../types'

export interface SagaListItem {
  id: string
  name: string
  status: SagaStatus
  created_at: string
  completed_at: string | null
  step_count: number
}

export interface SagaListResponse {
  items: SagaListItem[]
  total: number
  page: number
  page_size: number
}

export const sagasApi = {
  list: (page = 1, page_size = 20) =>
    api.get<SagaListResponse>(`/sagas?page=${page}&page_size=${page_size}`),

  create: (body: SagaCreateRequest) => api.post<Saga>('/sagas', body),

  get: (id: string) => api.get<Saga>(`/sagas/${id}`),
}
