import { api } from './client'
import type { AuditLog, Job, PaginatedResponse, User } from '../types'
import type { JobListParams } from './jobs'

export interface AdminJobListParams extends JobListParams {
  user_id?: string
}

export const adminApi = {
  listJobs: (params: AdminJobListParams = {}) => {
    const qs = new URLSearchParams()
    if (params.page) qs.set('page', String(params.page))
    if (params.page_size) qs.set('page_size', String(params.page_size))
    if (params.status) qs.set('status', params.status)
    if (params.type) qs.set('type', params.type)
    if (params.trace_id) qs.set('trace_id', params.trace_id)
    if (params.user_id) qs.set('user_id', params.user_id)
    const q = qs.toString()
    return api.get<PaginatedResponse<Job>>(`/admin/jobs${q ? `?${q}` : ''}`)
  },

  getJob: (id: string) => api.get<Job>(`/admin/jobs/${id}`),

  replayJob: (id: string) => api.post<Job>(`/admin/jobs/${id}/replay`),

  resolveIncident: (id: string) => api.post<Job>(`/admin/incidents/${id}/resolve`),

  listUsers: (page = 1) =>
    api.get<PaginatedResponse<User>>(`/admin/users?page=${page}&page_size=50`),

  listAuditLogs: (params: { page?: number; job_id?: string; user_id?: string; action?: string } = {}) => {
    const qs = new URLSearchParams()
    if (params.page) qs.set('page', String(params.page))
    if (params.job_id) qs.set('job_id', params.job_id)
    if (params.user_id) qs.set('user_id', params.user_id)
    if (params.action) qs.set('action', params.action)
    const q = qs.toString()
    return api.get<PaginatedResponse<AuditLog>>(`/audit/logs${q ? `?${q}` : ''}`)
  },
}
