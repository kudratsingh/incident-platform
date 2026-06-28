// ---------------------------------------------------------------------------
// Mirrors backend Pydantic schemas — keep in sync with backend/app/schemas/
// ---------------------------------------------------------------------------

export type UserRole = 'user' | 'support' | 'admin'

export type JobType = 'csv_upload' | 'report_gen' | 'bulk_api_sync' | 'doc_analysis'

export type JobStatus =
  | 'waiting'
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'dead_letter'
  | 'cancelled'

export type SagaStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'compensating'
  | 'compensated'

export interface User {
  id: string
  email: string
  role: UserRole
  is_active: boolean
  created_at: string
}

export interface Job {
  id: string
  user_id: string
  type: JobType
  status: JobStatus
  idempotency_key: string | null
  payload: Record<string, unknown> | null
  result: Record<string, unknown> | null
  error_message: string | null
  retry_count: number
  max_retries: number
  priority: number
  trace_id: string | null
  saga_id?: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface Saga {
  id: string
  name: string
  status: SagaStatus
  created_at: string
  completed_at: string | null
  steps: Job[]
}

export interface JobEvent {
  id: string
  event_name: string
  recorded_at: string
  kafka_topic: string
  kafka_partition: number
  kafka_offset: number
  payload: Record<string, unknown>
}

export interface JobTimeline {
  job_id: string
  count: number
  events: JobEvent[]
}

export interface SystemStats {
  by_status: Record<string, number>
}

export interface SLOState {
  id: string
  name: string
  description: string
  target: number
  window_hours: number
  runbook_id: string
  total: number
  failed: number
  current: number
  budget_remaining_pct: number
  burn_rate: number | null
  healthy: boolean
}

export type TriageCategory =
  | 'external_api_failure'
  | 'validation_error'
  | 'infrastructure'
  | 'data_corruption'
  | 'configuration'
  | 'transient'
  | 'unknown'

export interface JobTriage {
  id: string
  job_id: string
  root_cause_category: TriageCategory
  summary: string
  suggested_fix: string
  is_retryable: boolean
  confidence: number
  model_used: string
  usage: Record<string, number> | null
  created_at: string
  updated_at: string
}

export interface Runbook {
  id: string
  title: string
  severity?: string
  alarm?: string
  summary?: string
  symptoms?: string[]
  diagnosis_steps?: Array<{ id?: string; description?: string; command?: string }>
  mitigation?: string[]
  escalation?: string[]
  related_dashboards?: string[]
}

export interface AuditLog {
  id: string
  user_id: string | null
  job_id: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  request_id: string | null
  ip_address: string | null
  extra_data: Record<string, unknown> | null
  created_at: string
}

export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_next: boolean
}

export interface ApiError {
  error_code: string
  message: string
  details?: unknown
  request_id?: string
}

export interface ProgressEvent {
  job_id: string
  status: JobStatus | 'retrying' | 'dead_letter'
  progress: number
  message: string
  retry_count: number
  timestamp: string
}

export interface JobCreateRequest {
  type: JobType
  payload?: Record<string, unknown>
  idempotency_key?: string
  priority?: number
  dependencies?: string[]
}

export interface SagaStepRequest {
  type: JobType
  payload?: Record<string, unknown>
  priority?: number
}

export interface SagaCreateRequest {
  name: string
  steps: SagaStepRequest[]
}
