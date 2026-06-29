import { api } from './client'
import type {
  AuditLog,
  Job,
  JobTimeline,
  JobTriage,
  PaginatedResponse,
  Runbook,
  SLOState,
  SystemStats,
  Tenant,
  User,
} from '../types'
import type { JobListParams } from './jobs'

export interface AdminJobListParams extends JobListParams {
  user_id?: string
  tenant_id?: string
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
    if (params.tenant_id) qs.set('tenant_id', params.tenant_id)
    const q = qs.toString()
    return api.get<PaginatedResponse<Job>>(`/admin/jobs${q ? `?${q}` : ''}`)
  },

  getJob: (id: string) => api.get<Job>(`/admin/jobs/${id}`),

  replayJob: (id: string) => api.post<Job>(`/admin/jobs/${id}/replay`),

  resolveIncident: (id: string) => api.post<Job>(`/admin/incidents/${id}/resolve`),

  dlqStats: () =>
    api.get<{ total: number; by_type: Record<string, number> }>(`/admin/dlq/stats`),

  systemStats: (tenantId?: string) =>
    api.get<SystemStats>(`/admin/stats${tenantId ? `?tenant_id=${tenantId}` : ''}`),

  userStats: (userId: string) =>
    api.get<SystemStats>(`/admin/users/${userId}/stats`),

  jobTimeline: (jobId: string) =>
    api.get<JobTimeline>(`/admin/jobs/${jobId}/timeline`),

  jobTriage: (jobId: string) =>
    api.get<JobTriage>(`/admin/jobs/${jobId}/triage`),

  slos: () => api.get<{ slos: SLOState[] }>(`/admin/slos`),

  runbooks: () => api.get<{ items: Runbook[]; count: number }>(`/admin/runbooks`),

  runbook: (id: string) => api.get<Runbook>(`/admin/runbooks/${id}`),

  listUsers: (page = 1, tenantId?: string) => {
    const tail = tenantId ? `&tenant_id=${tenantId}` : ''
    return api.get<PaginatedResponse<User>>(
      `/admin/users?page=${page}&page_size=50${tail}`,
    )
  },

  listTenants: (page = 1) =>
    api.get<{ items: Tenant[]; total: number; page: number; page_size: number }>(
      `/admin/tenants?page=${page}&page_size=50`,
    ),

  getTenant: (id: string) => api.get<Tenant>(`/admin/tenants/${id}`),

  createTenant: (slug: string, name: string) =>
    api.post<Tenant>('/admin/tenants', { slug, name }),

  updateTenantLimits: (
    id: string,
    body: { rate_limit_per_minute?: number; quota_jobs_per_month?: number },
  ) => api.patch<Tenant>(`/admin/tenants/${id}`, body),

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
