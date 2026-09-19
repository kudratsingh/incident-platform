import { api } from './client'
import type {
  AgentRun,
  AuditLog,
  CircuitBreakersResponse,
  ConsumerLagResponse,
  IncidentDigest,
  Job,
  JobTimeline,
  JobTriage,
  PaginatedResponse,
  PlatformAlert,
  Runbook,
  SLOState,
  SystemStats,
  Tenant,
  TenantSummary,
  User,
} from '../types'
import type { JobListParams } from './jobs'

export interface AdminJobListParams extends JobListParams {
  user_id?: string
  tenant_id?: string
}

/**
 * How many rows the demo page asks for in one page.
 *
 * `/admin/agent-runs` and `/admin/alerts` are both `PaginatedResponse`, so they
 * page like every other list here rather than answering with everything.
 */
const DEMO_PAGE_SIZE = 50

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

  /**
   * Returns the new tenant row only — no `users`/`jobs` rollups and no
   * rate/quota limits, which the list endpoint computes. Typed as
   * TenantSummary so callers cannot mistake it for a list row.
   */
  createTenant: (slug: string, name: string) =>
    api.post<TenantSummary>('/admin/tenants', { slug, name }),

  nlQuery: (question: string) =>
    api.post<{
      spec: Record<string, unknown>
      model: string
      usage: Record<string, number>
      items: Job[]
      total: number
    }>('/admin/query', { question }),

  listDigests: (tenantId?: string, limit = 20) => {
    const params = new URLSearchParams()
    params.set('limit', String(limit))
    if (tenantId) params.set('tenant_id', tenantId)
    return api.get<{ items: IncidentDigest[]; count: number }>(
      `/admin/digests?${params.toString()}`,
    )
  },

  generateDigest: (hours?: number) =>
    api.post<
      | IncidentDigest
      | { summary: null; window_start: string; window_end: string }
    >('/admin/digests/generate', hours ? { hours } : {}),

  updateTenantLimits: (
    id: string,
    body: { rate_limit_per_minute?: number; quota_jobs_per_month?: number },
  ) => api.patch<Tenant>(`/admin/tenants/${id}`, body),

  listAuditLogs: (params: AuditListParams = {}) => {
    const qs = new URLSearchParams()
    if (params.page) qs.set('page', String(params.page))
    if (params.page_size) qs.set('page_size', String(params.page_size))
    if (params.job_id) qs.set('job_id', params.job_id)
    if (params.user_id) qs.set('user_id', params.user_id)
    if (params.action) qs.set('action', params.action)
    // A whole stream (`agent.`, `chaos.`, `job.`), where `action` is one row's
    // exact name. Added to the endpoint by WO-R3-313.
    if (params.action_prefix) qs.set('action_prefix', params.action_prefix)
    if (params.principal_type) qs.set('principal_type', params.principal_type)
    const q = qs.toString()
    return api.get<PaginatedResponse<AuditLog>>(`/audit/logs${q ? `?${q}` : ''}`)
  },

  // ── operator-only readings behind the /demo page (WO-R3-312) ──────────────

  /**
   * The agent runs the commander has reported (ADR 0035), newest first.
   *
   * `active=true` narrows to runs nobody has closed — the console's own query
   * while a demo is running. The agent's principal cannot read any of this:
   * there is no MCP tool for `agent_runs`, deliberately.
   */
  listAgentRuns: (params: { alert_id?: string; active?: boolean } = {}) => {
    const qs = new URLSearchParams()
    qs.set('page_size', String(DEMO_PAGE_SIZE))
    if (params.alert_id) qs.set('alert_id', params.alert_id)
    if (params.active !== undefined) qs.set('active', String(params.active))
    return api.get<PaginatedResponse<AgentRun>>(`/admin/agent-runs?${qs.toString()}`)
  },

  getAgentRun: (id: string) => api.get<AgentRun>(`/admin/agent-runs/${id}`),

  /**
   * Every consumer group's lag in one reading.
   *
   * The whole response, not just `groups`: `live_group` names the one group
   * whose number actually moves, and the others are recorded constants that a
   * console should not present as live measurements.
   */
  consumerLag: () => api.get<ConsumerLagResponse>('/admin/consumer-lag'),

  /**
   * Breaker state as published in Redis (ADR 0030).
   *
   * The whole response, not just `breakers`: a breaker with no record is ABSENT
   * from the list rather than reported closed, and an empty list with
   * `unknown_reason` set is a different finding from an empty list without it.
   * Returning the array alone would throw that distinction away.
   */
  circuitBreakers: () => api.get<CircuitBreakersResponse>('/admin/circuit-breakers'),

  /** `active=true` is the agent's `list_active_alerts` view; omit it to see resolved ones too. */
  listAlerts: (active = true) =>
    api.get<PaginatedResponse<PlatformAlert>>(
      `/admin/alerts?active=${String(active)}&page_size=${DEMO_PAGE_SIZE}`,
    ),
}

export interface AuditListParams {
  page?: number
  page_size?: number
  job_id?: string
  user_id?: string
  action?: string
  action_prefix?: string
  principal_type?: 'user' | 'service_account'
}
