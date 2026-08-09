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

  /**
   * Mint a short-lived, single-purpose token for this job's SSE stream.
   *
   * Native EventSource cannot send an Authorization header, so the stream GET
   * authenticates with this token in its query string instead of the primary
   * access JWT (ADR 0014). This POST is a normal fetch and carries the usual
   * Authorization header; the backend authorizes the job (tenant + ownership)
   * before minting. The token expires in ~60s — fetch a fresh one per
   * (re)connect.
   */
  streamToken: async (id: string): Promise<string> => {
    const res = await api.post<{ token: string }>(`/jobs/${id}/stream-token`)
    return res.token
  },
}
