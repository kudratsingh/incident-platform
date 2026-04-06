// ---------------------------------------------------------------------------
// Mirrors backend Pydantic schemas — keep in sync with backend/app/schemas/
// ---------------------------------------------------------------------------

export type UserRole = 'user' | 'support' | 'admin'

export type JobType = 'csv_upload' | 'report_gen' | 'bulk_api_sync' | 'doc_analysis'

export type JobStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'dead_letter'

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
  created_at: string
  started_at: string | null
  completed_at: string | null
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
}
