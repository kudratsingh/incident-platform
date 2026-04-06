import { api } from './client'
import type { Job, JobCreateRequest, PaginatedResponse } from '../types'

export interface JobListParams {
  page?: number
  page_size?: number
  status?: string
  type?: string
  trace_id?: string
}

export const jobsApi = {
  create: (body: JobCreateRequest) => api.post<Job>('/jobs', body),

  list: (params: JobListParams = {}) => {
    const qs = new URLSearchParams()
    if (params.page) qs.set('page', String(params.page))
    if (params.page_size) qs.set('page_size', String(params.page_size))
    if (params.status) qs.set('status', params.status)
    if (params.type) qs.set('type', params.type)
    if (params.trace_id) qs.set('trace_id', params.trace_id)
    const q = qs.toString()
    return api.get<PaginatedResponse<Job>>(`/jobs${q ? `?${q}` : ''}`)
  },

  get: (id: string) => api.get<Job>(`/jobs/${id}`),
}
