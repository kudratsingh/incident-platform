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
  tenant_id: string
  tenant_slug: string | null
  email: string
  role: UserRole
  is_active: boolean
  is_platform_admin: boolean
  created_at: string
}

/**
 * What `POST /admin/tenants` actually returns — the tenant row and nothing
 * else. The rollup counts and the limits are computed by the list endpoint,
 * so a created tenant is NOT a `Tenant` and must not be spliced into the
 * tenants list as though it were; refetch the list instead.
 */
export interface TenantSummary {
  id: string
  slug: string
  name: string
  is_active: boolean
  created_at: string
}

export interface Tenant extends TenantSummary {
  users: number
  jobs: number
  rate_limit_per_minute: number
  quota_jobs_per_month: number
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
  // Total runs this job may have — the original plus its retries. 3 means
  // three runs and two retries (WO-R2-172).
  max_attempts: number
  /** @deprecated Alias of `max_attempts` carrying the identical value. The
   *  API sends both for one release and then drops this one — optional so
   *  nothing here can come to depend on it. Read `max_attempts`. */
  max_retries?: number
  // Which mechanism forced this job into the DLQ, when it was not the
  // default one. `llm_retry_policy` is the only value today; null means
  // retries simply ran out (or the job never dead-lettered at all).
  dead_lettered_by: string | null
  priority: number
  trace_id: string | null
  saga_id?: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null

  // ── Dead-letter detail (WO-R3-312, additive) ──────────────────────────────
  // These five are populated for dead-lettered jobs and absent everywhere else,
  // so they are optional here rather than nullable-required: a `Job` built from
  // an older response, or from a non-DLQ list, simply does not carry them, and
  // a required field would have made every existing caller wrong.
  //
  // `null` means "not known", never "no". A row with `remediation_hint: null`
  // has not been categorised — it is emphatically NOT replay-safe (the
  // platform's triage is off by default, so organic dead-letters stay null).
  /** Coarse routing class set by triage or by a fence. See RemediationHint. */
  remediation_hint?: string | null
  /** When the job entered the DLQ — distinct from `completed_at`. */
  dead_lettered_at?: string | null
  /** Set when someone declared this row unreplayable. */
  fenced_at?: string | null
  /** Who fenced it (a principal name), not why. */
  fenced_by?: string | null
  /** The LLM triage row for this job, when one exists. */
  triage?: JobTriageSummary | null
}

/** The part of a `job_triages` row the DLQ views need. */
export interface JobTriageSummary {
  root_cause_category: string | null
  summary: string
  suggested_fix?: string | null
  is_retryable?: boolean | null
  confidence?: number | null
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

export interface IncidentDigest {
  id: string
  tenant_id: string
  window_start: string
  window_end: string
  summary: string
  highlights: {
    key_concerns?: string[]
    recommended_actions?: string[]
    by_status?: Record<string, number>
    failed_by_type?: Record<string, number>
  }
  model_used: string
  usage: Record<string, number>
  created_at: string
}

export interface AuditLog {
  id: string
  user_id: string | null
  principal_type: 'user' | 'service_account'
  principal_id: string | null
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
  // Provenance the backend attaches so it can order the retained snapshot
  // (backend/app/workers/progress.py). `source` is the Kafka topic and
  // `sequence` the offset within it. They are the SERVER's ordering keys —
  // offsets from different topics are not comparable, so the client must not
  // use them to sort or dedupe. Carried here only so the type matches the
  // wire. Absent on events published outside the consumer.
  source?: string
  sequence?: number | null
}

// ---------------------------------------------------------------------------
// The agent's own run, as the platform records it (WO-R3-312, ADR 0035).
//
// The commander writes these over two `[commander: telemetry]` MCP tools; the
// platform stores them; human operators read them here. The agent's own
// principal cannot read any of it — the console is the only reader, which is
// the whole point of ADR 0035.
// ---------------------------------------------------------------------------

/**
 * The nine states, which are the commander's own `IncidentState` values —
 * character for character, with no mapping layer on either side of the wire.
 *
 * That is deliberate (platform `AgentRunState`): a state the responder reaches
 * cannot be lost in translation on its way to this console, and a member added
 * in one repository and not the other is a refusal at the wire rather than a
 * silently dropped state. Only `resolved` / `escalated` / `failed` close a run.
 *
 * The console still renders an unrecognised value verbatim rather than guessing
 * a station for it, because this list can only ever be one release behind.
 */
export type AgentRunState =
  | 'triage'
  | 'investigating'
  | 'planning'
  | 'awaiting_approval'
  | 'remediating'
  | 'verifying'
  | 'resolved'
  | 'escalated'
  | 'failed'

/** One append-only entry of `phase_history`. */
export interface AgentRunPhase {
  state: AgentRunState
  at: string
}

export interface AgentRunHypothesis {
  name: string
  category: string
  confidence: number
}

export interface AgentRunStep {
  kind: 'read' | 'action'
  tool: string
  at: string
}

// ---------------------------------------------------------------------------
// What a run record carries since WO-R3-328 (→ platform v0.6.16).
//
// The first live take failed because none of this existed: `report_agent_run`
// sent `current_hypothesis: null` and `last_step: null` on every transition, so
// the console's agent panel was empty for the whole run, and `agent.tool_invoked`
// audit rows carry no result, so "what the agent saw" could not be shown at all.
//
// Every field below is ADDITIVE and optional here on purpose. A stack older than
// v0.6.16 omits them, and a commander older than WO-R3-329 sends them empty on a
// current stack — two different absences, and the page says which it is looking
// at rather than rendering a blank panel. `?? []` / `?? null` at every read.
// ---------------------------------------------------------------------------

/** One entry of the ranked list, which is what a hypothesis panel is about. */
export interface AgentRunRankedHypothesis {
  name: string
  category: string
  confidence: number
  /** The responder's own reasoning, truncated to 280 chars by the reporter. */
  reasoning_excerpt?: string | null
}

/** The action the run decided on, written when it enters `remediating`. */
export interface AgentRunPlan {
  action_tool: string
  action_arguments: Record<string, unknown>
  /** The hypothesis this action is aimed at, by name. */
  target_hypothesis?: string | null
  rationale_excerpt?: string | null
}

/**
 * One verify poll's verdict.
 *
 * The four values the commander writes today are `verified`, `not_verified`,
 * `verified_stabilizer` and `verified_unresolved`; the type stays open because
 * this console can only ever be one commander release behind, and an unknown
 * verdict must render verbatim rather than as a guess.
 */
export interface AgentRunVerification {
  verdict: string
  reasoning_excerpt?: string | null
  /** Which poll this was, of how many the run allows. */
  attempt?: number | null
  of?: number | null
  at?: string | null
}

/**
 * One step: a single tool call, in order, with what came back.
 *
 * `result_excerpt` is the whole reason this shape exists — the audit log records
 * that a call happened and with what arguments, never what it answered.
 */
export interface AgentRunStepRecord {
  seq: number
  kind: 'read' | 'action' | 'report'
  tool: string
  arguments?: Record<string, unknown> | null
  /** Truncated to 400 chars by the reporter. Null when nothing came back. */
  result_excerpt?: string | null
  outcome?: string | null
  latency_ms?: number | null
  at: string
}

/** What the run has spent. `null` where the ledger has no reading. */
export interface AgentRunBudget {
  tool_calls_used?: number | null
  tool_calls_max?: number | null
  tokens_used?: number | null
  usd_used?: number | null
  wall_seconds?: number | null
}

/**
 * ADR 0071's structural verdict on the recovery the run read.
 *
 * Carried on the briefing (`EscalationBriefing.attribution`) rather than left to
 * prose, so the handoff states whether the run may claim the recovery.
 */
export interface AgentBriefingAttribution {
  /** `attributed` | `cleared_on_its_own` | `cannot_attribute`. */
  verdict: string
  /** The resource the verdict is about. */
  resource: string
  /** The read tool whose readings answered it. */
  probe_tool: string
  /** Whether a Tier-1 action in this run touched that resource. */
  acted: boolean
  /** The readings behind the verdict, in one sentence. */
  detail: string
}

/** One cause the run named (ADR 0065's slot shape). */
export interface AgentBriefingSlot {
  category: string
  name: string
  confidence: number
  addressed: boolean
}

/**
 * Primary / secondary / unresolved-extra, ADR 0065.
 *
 * `unresolved_extra` is the remainder: every cause the run still asserts and
 * took no action on. A briefing card that dropped it would let the run look
 * complete when it is not.
 */
export interface AgentBriefingSlots {
  primary: AgentBriefingSlot | null
  secondary: AgentBriefingSlot[]
  unresolved_extra: AgentBriefingSlot[]
}

export interface AgentBriefingAction {
  tool: string
  arguments: Record<string, unknown>
}

/** The commander's `EscalationBriefing` dump, plus the writer's prose. */
export interface AgentBriefing {
  incident_id: string
  final_state: string
  alert_summary: string
  escalation_reason?: string
  attempted_action?: AgentBriefingAction | null
  incidents?: AgentBriefingSlots
  findings?: string
  recommendation?: string
  /** ADR 0071. Null on a run whose trajectory predates the projection. */
  attribution?: AgentBriefingAttribution | null
  /** Present only when the run was enriched (live); null on a canned run. */
  prose?: string | null
}

export interface AgentRun {
  id: string
  tenant_id: string
  alert_id: string | null
  /** The principal that wrote every report in this run. Always present. */
  service_account_id: string
  /**
   * The responder's own short name for the run.
   *
   * The MCP write side calls this field `run_label`, because ADR 0012's registry
   * screen bans the lab's word for it from a non-chaos tool's `tools/list`
   * surface. It lands in `agent_runs.scenario` and reaches the console under
   * that name — one wire name, two spellings, on purpose.
   */
  scenario: string | null
  state: AgentRunState
  phase_history: AgentRunPhase[]
  current_hypothesis: AgentRunHypothesis | null
  last_step: AgentRunStep | null
  briefing: AgentBriefing | null
  started_at: string
  updated_at: string
  finished_at: string | null
  /** Computed server-side: `finished_at === null`. One fact, not two. */
  active: boolean

  // ── WO-R3-328, all additive and all absent on an older stack ──────────────
  /** The ranked list. `current_hypothesis` is its top entry, kept as it was. */
  hypotheses?: AgentRunRankedHypothesis[]
  plan?: AgentRunPlan | null
  /** The latest verdict; `verifications` is every one, in order. */
  verification?: AgentRunVerification | null
  verifications?: AgentRunVerification[]
  /** Append-only, capped at 200 by the platform. */
  steps?: AgentRunStepRecord[]
  /** How many steps the cap dropped. Non-zero means the ledger is incomplete. */
  steps_dropped?: number
  budget?: AgentRunBudget | null
}

/** `GET /admin/agent-runs/{id}/steps?after_seq=` — incremental polling. */
export interface AgentRunStepsResponse {
  run_id: string
  items: AgentRunStepRecord[]
  /** The highest `seq` the run holds, so a caller knows it is caught up. */
  max_seq?: number | null
  steps_dropped?: number
}

// ---------------------------------------------------------------------------
// Operator-only readings that used to exist only as MCP tools (WO-R3-312).
// ---------------------------------------------------------------------------

export interface LagSample {
  lag: number
  measured_at: string
}

/**
 * One consumer group's lag.
 *
 * `lag_known: false` with `lag: null` is a real answer and must render as
 * unknown-with-a-reason. Rendering it as 0 is the bug ADR 0030 is about: an
 * absent reading is not a healthy one — hence `lag_unknown_reason`, which is
 * null exactly when `lag_known` is true so a blank cell always has an
 * explanation beside it.
 *
 * `recent_samples` arrives **newest first**, which any chart has to reverse.
 * It is empty both for a group nothing measures and before the first window is
 * recorded: an empty list is missing history, not a flat line.
 */
export interface ConsumerLagReading {
  consumer_group: string
  lag: number | null
  lag_known: boolean
  source: 'live' | 'static' | 'unrecognized'
  lag_unknown_reason: string | null
  measured_at: string | null
  age_seconds: number | null
  recent_samples: LagSample[]
}

export interface ConsumerLagResponse {
  measured_at: string
  groups: ConsumerLagReading[]
  total: number
  /** The one group whose number actually moves; the rest are recorded constants. */
  live_group: string
}

export interface CircuitBreakerReading {
  name: string
  state: string
  failure_count: number
  failure_threshold: number
  recovery_timeout_s: number
  last_state_change_at: string | null
  seconds_since_state_change: number | null
  last_failure_at: string | null
  last_failure_reason_class: string | null
  recorded_at: string
  /** Not a heartbeat: a large age on a closed breaker means nothing called it. */
  reported_age_s: number
}

export interface CircuitBreakersResponse {
  measured_at: string
  breakers: CircuitBreakerReading[]
  total: number
  /**
   * Set when the platform could say nothing at all. An empty list with this set
   * is not the same finding as an empty list without it — so the console has to
   * carry the whole response, not just the array.
   */
  unknown_reason: string | null
}

export interface PlatformAlert {
  id: string
  tenant_id: string
  severity: string
  source: string
  title: string
  description: string | null
  fired_at: string
  /** Null while the alert is active. */
  resolved_at: string | null
  extra_data: Record<string, unknown> | null
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
