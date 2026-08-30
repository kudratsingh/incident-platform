# Data model — table-by-table reference

The system of record is Postgres. Every table is documented here with its purpose, columns, indexes, FKs, and the *why* for each non-obvious choice. Read this when designing a query, adding a migration, or wondering why a particular column exists.

For the Redis side (CQRS read-model, queues, cache), see [`docs/REDIS.md`](REDIS.md).
For the Kafka side (event log, lifecycle topics), see [`docs/KAFKA.md`](KAFKA.md).

---

## Schema diagram

```
                    tenants
                   /       \
                  /         \
              users         jobs ─── job_dependencies (self-join)
                |             |
                |             ├── audit_logs
                |             ├── outbox_events
                |             ├── job_events       (event sourcing)
                |             ├── job_triages
                |             └── (saga_id → sagas)
                |
              (auth)
                |
              sagas
                |
              incident_summaries
```

Every domain table carries `tenant_id` as a FK to `tenants` so tenancy is enforced at the constraint layer (combined with RLS — see [ADR 0003](ADR/0003-rls-as-defense-in-depth.md)).

**RLS coverage** (migrations `c4f8e9a52340` + `a7e3d9c41f28`): all 11 tenant-scoped tables — `jobs`, `audit_logs`, `outbox_events`, `job_events`, `sagas`, `job_triages`, `incident_summaries`, `service_accounts`, `alerts`, `idempotency_records`, `deploy_markers` — carry the `tenant_isolation` policy **and `FORCE ROW LEVEL SECURITY`**, so the policies bind the table owner too (the RDS master — the migration role; since WO-P2-03 the runtime itself connects as the non-owner `incident_app` role, which additionally holds no UPDATE/DELETE grant on `audit_logs`). The `deploy_markers` policy additionally admits `tenant_id IS NULL` rows (platform-wide deploys stay visible under tenant-scoped sessions) — and every writer honours that: `scripts/seed_eval_fixtures.py` used to stamp its six seeded markers with a concrete tenant, which put the eval fixtures on the wrong side of the policy's platform-wide branch, so `get_deploy_history` behaved differently on a seeded stack than on an empty one (WO-R2-69). The seeder now writes NULL and repairs any tenant-stamped row it finds from an older seed. `users` is the single deliberate exclusion — auth reads it before `app.tenant_id` is set (ADR 0003 bootstrap). `audit_logs` also carries RESTRICTIVE deny policies for UPDATE/DELETE, making it immutable at the DB layer while the `ON DELETE SET NULL` FKs keep working (referential-integrity actions bypass RLS). See [ADR 0015](ADR/0015-force-rls-and-nonowner-app-role.md). The unit gate `backend/tests/unit/test_rls_coverage.py` fails CI if a future `tenant_id` table ships without a policy, and the `integration` CI job proves the enforcement itself on a live server: `backend/tests/integration/test_rls_enforcement.py` asserts ENABLE+FORCE on all 11 tables, cross-tenant invisibility, and that `audit_logs` UPDATE/DELETE raise `insufficient_privilege` while the FK `SET NULL` still fires.

---

## `tenants` — the multi-tenancy root

The container that owns users, jobs, sagas, and everything downstream.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `DEFAULT_TENANT_ID = d3fa17de-7a17-de7a-17de-7a17de7a17de` is the bootstrap. Mixed-hex deliberately because SQLite (used in tests) silently coerces all-zero UUIDs to integer 1. |
| `slug` | String(64) UNIQUE | URL-safe identifier. Validated to alphanumerics + `-` + `_` at the API layer. |
| `name` | String(255) | Display name. |
| `is_active` | Boolean | Soft-delete flag. Inactive tenants reject new registrations (`AuthService.register`) but their data stays queryable. |
| `rate_limit_per_minute` | Integer (default 120) | Per-tenant API rate limit. 0 disables. Phase 12 PR C. |
| `quota_jobs_per_month` | Integer (default 100_000) | Monthly job submission quota. 0 disables. Phase 12 PR C. |
| `created_at`, `updated_at` | DateTime | TimestampMixin pattern. |

The `tenants` table is the only table that intentionally does NOT carry a `tenant_id` column (it would be a circular self-reference). Auth reads from `tenants` and `users` before the tenant context is set, which is why neither has RLS — see [ADR 0003](ADR/0003-rls-as-defense-in-depth.md).

---

## `users` — accounts

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id ON DELETE RESTRICT | Every user belongs to exactly one tenant. `RESTRICT` because dropping a tenant with users would orphan FKs in jobs/audits. |
| `email` | String(255) UNIQUE NOT NULL | Globally unique, not per-tenant. Two different tenants' users can't share an email. Simplifies login (one lookup, no tenant disambiguation). The trade-off is that email is a global namespace; if we ever need per-tenant emails (e.g. `admin@` in every tenant), this becomes a migration. |
| `hashed_password` | String(255) | bcrypt hash. Never logged, never returned. |
| `role` | String(50) | One of `user / support / admin`. Validated as `UserRole` enum at the application layer (Postgres stores the string for evolution flexibility — see ADR-style note below on string-vs-enum). |
| `is_active` | Boolean | Disabled accounts can't authenticate. |
| `is_platform_admin` | Boolean (default false) | Cross-tenant operator flag. Granted via Alembic data migration `d9c01a7e4f30` for every existing default-tenant admin; future grants are out-of-band. Phase 12 PR D. |
| `created_at`, `updated_at` | DateTime | TimestampMixin. |

### Indexes

- `ix_users_tenant_id` — covers `WHERE tenant_id = ?` for the admin's user list.
- `ix_users_email` — login path is `SELECT WHERE email = ?`; would be slow without it.

### Why role is a `String(50)`, not a Postgres ENUM type

Adding a new role to a Postgres ENUM requires `ALTER TYPE`, which can't run inside a transaction in some Postgres versions and is a footgun in zero-downtime deploys. Storing as VARCHAR with application-level enum validation gives us all the type safety with none of the operational pain. Same convention for `jobs.status`, `jobs.type`, `sagas.status`.

---

## `jobs` — the main entity

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Generated server-side. Returned to the caller for status tracking. |
| `tenant_id` | UUID NOT NULL FK → tenants.id | Indexed. RLS-policy keyed on this. |
| `user_id` | UUID NOT NULL FK → users.id | Owner of the job. Not the same as the requester (admin Replay reuses the original user). |
| `type` | String(100) | `csv_upload / report_gen / bulk_api_sync / doc_analysis` (see `JobType` enum). Indexed for `?type=` filters. |
| `status` | String(50) | `waiting / pending / running / completed / failed / dead_letter / cancelled`. Indexed for `?status=` filters. |
| `idempotency_key` | String(255) NULLABLE | Optional. UNIQUE constraint is **composite** with `tenant_id` (`UNIQUE (tenant_id, idempotency_key)`), set in Alembic `a9c2d1e83104`. Pre-Phase-12 this was a global UNIQUE, which prevented two tenants from using the same key. |
| `payload` | JSONB NULLABLE | The job's input. Schema enforced by the processor (each `JobType` has its own Pydantic shape). Includes `__traceparent` for OTel context propagation. |
| `result` | JSONB NULLABLE | Set on success. |
| `error_message` | Text NULLABLE | Set on failure. Truncated/sanitized at the worker; not the full traceback. |
| `retry_count` | Integer (default 0) | Incremented on each failure. Reset to 0 on admin Replay (previous value recorded in audit log). |
| `max_retries` | Integer (default `MAX_JOB_RETRIES`, itself 3) | Per-job cap, stamped at INSERT from the setting rather than from a literal, so the knob reaches every writer — REST creation, chaos hooks, eval seeds, saga steps (WO-R2-76). A caller may still name a different ceiling for one job; the saga coordinator does. Changing the setting does **not** re-stamp existing rows: the dispatcher reads the ceiling off the row, so jobs created before the change keep the budget they were admitted with. |
| `dead_lettered_by` | String(32) NULLABLE | Which mechanism forced the dead-letter, when it was not the default one — `llm_retry_policy` is the only value today. NULL = retries simply ran out (or no processor was registered, or the dispatcher's safety net fired), so the admin DLQ tab can badge the LLM rows without inferring it from `retry_count < max_retries`, which mislabelled every compensation job. Reset to NULL on admin Replay. Added in Alembic `c9a3e5d70b12`; not backfilled, so pre-migration rows read NULL. |
| `remediation_hint` | String(32) NULLABLE | The `RemediationHint` category the agent reads to pick a remediation strategy: `replay_safe`, `wait_and_replay`, `human_required`. Never inferred from the error text at read time — this column is the canonical source. Written by the LLM triage consumer (R2-24 — mapped from the `TriageAnalysis` it already computes, in the same transaction as the `job_triages` row, and **only into a NULL**, so it can never lower a fence somebody raised), the `mark_dlq_permanent` MCP action, the eval seed script, and the DLQ-producing chaos hooks. Triage is gated behind `LLM_TRIAGE_ENABLED`, which defaults to false ([ADR 0005](ADR/0005-llm-features-fail-open.md)) — so on a stock deployment the other three are still the only writers, and an organically dead-lettered job carries no category. That gap is the reason this column read as fiction for so long: the enum, this table and the DLQ tool descriptions all named triage as the setter while the consumer wrote a `job_triages` row and never touched the column at all. NULL = not categorised, which means *unknown*, not replay-safe. **Scoped to one dead-letter episode**: reset to NULL on Replay alongside `dead_lettered_by`, so a job that dead-letters again arrives uncategorised and is classified on the new failure's own evidence (R2-23). Before that reset existed, nothing in production could ever clear the column, so one episode's category — including an operator's `human_required` fence — silently governed every later, unrelated dead-letter of the same job. That reset is also what keeps the fence in `replay_dlq_messages` (R2-22) from being permanent: a category that can be raised must be lowerable by the same lifecycle that ends the episode. |
| `priority` | Integer (default 0) | Indexed. Higher = more urgent. Currently informational; the priority-aware queue exists in `app/workers/queue.py` (legacy delayed-retry path) but Kafka itself doesn't respect priority — every event is FIFO within a partition. |
| `trace_id` | String(255) NULLABLE | Indexed. Carries the OTel trace ID so the UI can show "request → API → worker → DB" as one trace. Pasted into the admin filter to find a specific request. |
| `started_at`, `completed_at` | DateTime NULLABLE | Filled by the dispatcher on transition. Used by SLOs. `started_at` is also half of the stale-RUNNING sweep's compare-and-set, so a replayed job's new attempt is distinguishable from the one a sweep pass scanned. |
| `requeued_at` | DateTime NULLABLE | When the stale-PENDING backstop last re-published this job, stamped in the same transaction as the outbox insert. Takes the row out of the backstop's own predicate for one 300s window, which is what stops a dispatcher that is behind from minting one duplicate `job.submitted` per sweep pass indefinitely. Deliberately **not** `updated_at`: that column is the staleness signal and is rendered to operators, so a sweep write must not be able to look like progress. Added in Alembic `b1f39d7c2a84` ([ADR 0023](ADR/0023-dispatcher-sweep-ownership.md)). |
| `heartbeat_at` | DateTime NULLABLE | When the worker executing this job last checked in (every 20s, live for 120s). The cross-replica answer to "is someone running this?", which `in_flight_job_ids` — a set in one process's memory — could not give: without it a second replica read every other replica's running jobs as crash orphans and dead-lettered them. NULL reads as stale, because a crash before the first check-in is exactly what the sweep reclaims. Renewal stops once the job is past `stale_running_threshold_seconds` + grace so a wedged worker cannot defend its own stuck job. Added in Alembic `b1f39d7c2a84`, backfilled to `NOW()` for rows already RUNNING ([ADR 0023](ADR/0023-dispatcher-sweep-ownership.md)). |
| `saga_id` | UUID NULLABLE FK → sagas.id ON DELETE SET NULL | Phase 7. Non-null = this job is a saga step. |
| `saga_step_index` | Integer NULLABLE | 0-based position of this step in its saga's declaration order, stamped once by `SagaService.create_saga`. It exists because `created_at` cannot carry it: `func.now()` is Postgres `transaction_timestamp()`, so every step of a saga — all inserted by one `POST /sagas` — shares an identical value and `ORDER BY created_at` over them is a total tie. The compensation rollback order is this column reversed, which is what makes "undo the most recent success first" a guarantee rather than whatever the planner returned (R2-58). NULL for anything that is not a declared step: ordinary jobs, the `.compensate` rows (ordered by the steps they undo, not by a position of their own), and rows written before Alembic `d1f6a2b940c7`, which backfilled existing sagas from `(created_at, id)` — stable, but no more meaningful than the tie it froze. |
| `created_at`, `updated_at` | DateTime | TimestampMixin. Note `created_at` is the **transaction** timestamp, so it is not unique per row and never a sufficient sort key on its own: every paginated query in `app/repositories/` orders by `(created_at, id)` so OFFSET/LIMIT pages cannot overlap or skip (R2-58). |

### Indexes

- `ix_jobs_tenant_id`, `ix_jobs_user_id`, `ix_jobs_status`, `ix_jobs_type`, `ix_jobs_priority`, `ix_jobs_trace_id` — every column the admin filters on has one.
- `ix_jobs_created_at` (added in Phase 10) — time-window queries (the NL query feature, `created_after` / `created_before`).
- `uq_jobs_tenant_idempotency` — composite UNIQUE.

### Why error_message is Text, not String

Stack traces are typically 1-10KB. `VARCHAR(N)` with a small N would truncate; with a large N it's just Text with extra constraints. Text is what we want.

### Why payload + result are JSONB rather than a typed schema

The Job table holds heterogeneous job types with different input shapes. We could split into per-type tables (`csv_upload_jobs`, `report_gen_jobs`, etc.) — that's the inheritance path. The trade-off:

- **JSONB:** one table, one query path, easy to add new types. Validation lives in the processor layer (Pydantic).
- **Per-type table:** typed columns, indexed fields per type, easy to add per-type constraints. Cost: every new job type is a migration; admin queries need a UNION ALL.

Chose JSONB because the admin filter surface needs to be uniform (`SELECT * FROM jobs WHERE status=…`), and per-type schemas are stable enough to live in code rather than DDL.

---

## `job_dependencies` — DAG edges

Many-to-many self-join on `jobs` capturing parent → child dependencies.

| Column | Type | Notes |
|---|---|---|
| `job_id` | UUID NOT NULL FK → jobs.id | The child. |
| `depends_on_job_id` | UUID NOT NULL FK → jobs.id | The parent. |

PRIMARY KEY: composite `(job_id, depends_on_job_id)`. No row carries a tenant_id directly — both joined columns FK to `jobs` which carries the tenant_id, so RLS on `jobs` covers the access path.

Cycle-free by construction: dependencies can only reference existing jobs, which means the graph is always a DAG. We never let a job reference a child that doesn't exist yet.

`DependencyResolver` consumer reads `job.completed` events and promotes `WAITING` children to `PENDING` when all parents are done. The dispatcher's 10s resume sweep is the backstop for children whose promotion event has already passed — it selects `WAITING` rows with no unmet parent, oldest first, behind a rotating cursor.

When a parent instead reaches a terminal non-`COMPLETED` status (`DEAD_LETTER` or `CANCELLED`), its `WAITING` non-saga descendants cascade to `CANCELLED` rather than waiting forever, recursively, with the reason recorded in `error_message`. Saga steps are excluded — `SagaCoordinator` cancels those by saga membership. `FAILED` is not a cascade source: the retry cycle re-enters from it, so such a parent may still complete. See [ADR 0022](ADR/0022-promotable-only-resume-sweep-and-dependency-cascade.md).

---

## `sagas` — multi-step workflows

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | |
| `name` | String(255) | User-supplied label, e.g. "weekly billing run". |
| `status` | String(50) | `running / completed / failed / compensating / compensated` (SagaStatus enum). |
| `created_at`, `completed_at` | DateTime | |

A saga is a container; the actual steps live in `jobs` with `saga_id` set. Step *execution* order is encoded via `job_dependencies` — step N depends on step N-1. Step *declaration* order is recorded separately in `jobs.saga_step_index`, because the rollback needs it and the dependency edges only give it one hop at a time.

`SagaCoordinator` consumer marks the saga complete when all steps are completed, or kicks off compensation when any step dead-letters. Compensation steps are real `jobs` rows in the same saga (`type = {type}.compensate`, created in reverse `saga_step_index` order), so they appear in the saga's step list. The saga settles when its compensation set is **drained** — every `.compensate` job terminal — to `compensated` (all completed) or `failed` (any dead-lettered/cancelled). Drained includes drained-at-zero: a saga whose *first* step dead-letters has no completed predecessor to undo, mints no compensation jobs, and settles `compensated` in the same transaction that moved it to `compensating` (R2-49) rather than waiting for an event that can never arrive. See ADR 0017.

---

## `audit_logs` — what happened, when, by whom

Append-only log of every meaningful action (job creation, replay, incident resolved, saga created, login, user registration, tenant created, etc.). Append-only is enforced at the DB layer, twice over ([ADR 0015](ADR/0015-force-rls-and-nonowner-app-role.md)): the runtime `incident_app` role holds no UPDATE/DELETE grant on this table (migration `b8e4a1c92f35`), so tampering from the application raises `insufficient_privilege`; and RESTRICTIVE RLS policies deny UPDATE and DELETE for every non-superuser session, owner included (migration `a7e3d9c41f28`) — the back-stop for owner-connected sessions, where the failure mode is a silent `UPDATE 0`. The `ON DELETE SET NULL` FKs below still fire — referential actions execute with the table owner's privileges and bypass row security.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | Indexed. |
| `user_id` | UUID NULLABLE FK → users.id ON DELETE SET NULL | Nullable because some events have no user (e.g. system-emitted events). |
| `job_id` | UUID NULLABLE FK → jobs.id ON DELETE SET NULL | Nullable because not every audit event is job-related. |
| `action` | String(100) NOT NULL | Indexed. Snake_case verb, e.g. `job.created`, `job.replayed`, `saga.completed`, `tenant.created`, `tenant.limits_updated`. |
| `resource_type`, `resource_id` | String(100), String(255) | Free-form. The convention is `resource_type=job, resource_id=<uuid>` etc. |
| `request_id` | String(255) | The HTTP request that triggered the action. Used to correlate audit events across services. Caller-supplied via `X-Request-ID`, so the width is a correctness bound, not a formatting one — a value the column cannot hold makes the row fail to insert, and the MCP audit writer is savepoint-wrapped and silent. `app.core.middleware` validates the header down to 128 chars of a bounded charset before it ever reaches here, and `AuditRepository.log` truncates to `REQUEST_ID_MAX_LENGTH` as a last resort (WO-R2-51). |
| `ip_address` | String(50) | |
| `extra_data` | JSONB NULLABLE | Per-event freeform. e.g. `{retry_count: 5, error: "..."}` for `job.dead_letter`. |
| `kafka_topic` | String(128) NULLABLE | Set only on consumer-written `event.*` rows; NULL for inline application writes. |
| `kafka_partition` | Integer NULLABLE | Same. |
| `kafka_offset` | BigInt NULLABLE | Same. |
| `created_at` | DateTime | When the action happened. |

### Constraints

- **`uq_audit_logs_kafka_coord` — UNIQUE (kafka_topic, kafka_partition, kafka_offset)** — same dedup primitive as `job_events`: without it, Kafka redelivery would append duplicate `event.*` rows to the immutable trail; the `AuditConsumer` swallows the `IntegrityError` and commits the offset. NULLs are distinct under UNIQUE, so inline rows (all-NULL coords) never collide.

### Why audit logs are written by *both* the application and the audit consumer

The application writes audit rows synchronously when it makes the decision (e.g. `AuditRepository.log('job.created', ...)` inside the `POST /jobs` handler). The `audit-writer` Kafka consumer ALSO appends rows for every Kafka lifecycle event (`event.job.completed`, etc.). So the audit log is *both* application-emitted and event-sourced.

This is intentional redundancy. If a future bug means a Kafka event fires without the application path firing (or vice versa), the discrepancy is observable. In normal operation the two streams agree.

---

## `outbox_events` — durable handoff to Kafka

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | Indexed. |
| `topic` | String(255) NOT NULL | Kafka topic name. |
| `key` | String(255) NOT NULL | The composite `{tenant_id}:{user_id}` partition key. |
| `payload` | JSONB NOT NULL | The event body, will be validated against the topic's schema by the relay. |
| `attempts` | Integer NOT NULL (default 0) | Failed publishes so far. Read by both the relay (dead-letter at `outbox_max_attempts`) and `fetch_unpublished`'s predicate. |
| `created_at` | DateTime NOT NULL | |
| `published_at` | DateTime NULLABLE | NULL = still queued. Set when the relay is *done* with the row — delivered **or** dead-lettered. Not proof of delivery on its own; read with `failed_at`. |
| `failed_at` | DateTime NULLABLE | Non-NULL exactly when the row was abandoned without reaching Kafka. `published_at IS NOT NULL AND failed_at IS NULL` is a real publish. |
| `error_message` | Text NULLABLE | Why it was abandoned. Truncated to 900 chars by the writer. |

The dead-letter queue is a query, not a table:

```sql
SELECT id, topic, attempts, error_message, failed_at
  FROM outbox_events WHERE failed_at IS NOT NULL ORDER BY failed_at DESC;
```

Rows are kept with their payloads intact so they can be requeued — see
[`rb-outbox-relay-stalled`](../runbooks/rb-outbox-relay-stalled.yaml).

> `error_message` was listed in this table for two years before the column
> existed, described as something the relay set on permanent failure. It
> did not: there was no failed state and no cap, so a row that could never
> publish was retried every tick forever while holding one of the relay's
> fixed 100 fetch slots. Both columns and both exits are real as of
> migration `e5c93b7a2d18`; see [ADR 0001](ADR/0001-outbox-vs-cdc.md)'s
> 2026 Q3 addendum.

### Indexes

- `ix_outbox_events_unpublished` — **partial index** on `created_at` WHERE `published_at IS NULL`. Critical for the hot polling path; without it, the relay scan would degrade to a full table scan as the table grows. Dead-lettering sets `published_at`, so abandoned rows drop out of this index rather than accumulating in it.

### Why this exists at all

The whole reason for the outbox is to make state mutation and event publication atomic. See [ADR 0001](ADR/0001-outbox-vs-cdc.md).

---

## `job_events` — the immutable event log

Append-only mirror of every Kafka lifecycle event for forensic replay.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | Indexed. |
| `job_id` | UUID NULLABLE FK → jobs.id ON DELETE SET NULL | Nullable because we don't want a job deletion to break the historical log. |
| `event_name` | String(64) NOT NULL | e.g. `job.completed`. |
| `payload` | JSONB NOT NULL | The full Kafka event body, preserved verbatim. |
| `kafka_topic` | String(128) NOT NULL | |
| `kafka_partition` | Integer NOT NULL | |
| `kafka_offset` | BigInt NOT NULL | |
| `recorded_at` | DateTime NOT NULL | When the EventLogConsumer wrote this row (NOT when the event was emitted; that's in the payload). |

### Indexes / constraints

- **`UNIQUE (kafka_topic, kafka_partition, kafka_offset)`** — the dedup primitive that makes EventLogConsumer idempotent under Kafka redelivery. The consumer catches `IntegrityError` and silently commits the offset.
- `ix_job_events_job_id_recorded` — covers the `GET /admin/jobs/{id}/timeline` query.

### Why this isn't just `audit_logs`

Different shape, different consumer, different retention story. `audit_logs` is "what did the application do"; `job_events` is "what events fired on the bus". A future PR could merge them, but the separation matches the read paths (Audit tab vs. Job Timeline tab in the admin UI).

---

## `job_triages` — LLM analyses for dead-lettered jobs

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | |
| `job_id` | UUID UNIQUE NOT NULL FK → jobs.id ON DELETE CASCADE | UNIQUE → one triage per job. CASCADE because a deleted job has no meaningful triage. |
| `root_cause_category` | String(64) NOT NULL | One of the fixed categories in `RootCauseCategory`. |
| `summary` | Text NOT NULL | The LLM's one-sentence what-went-wrong. |
| `suggested_fix` | Text NOT NULL | The LLM's recommended action. |
| `is_retryable` | Boolean NOT NULL | Drives the admin's Replay vs Resolve decision. |
| `confidence` | Float NOT NULL | 0.0–1.0. Surfaced in the UI so admins can weigh the analysis. |
| `model_used` | String(64) NOT NULL | e.g. `claude-opus-4-7`. |
| `usage` | JSONB NULLABLE | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`. |
| `created_at`, `updated_at` | DateTime | |

The UNIQUE constraint is what makes Kafka redelivery a no-op: a second triage for the same job hits the constraint and rolls back (the consumer catches and commits).

---

## `incident_summaries` — periodic LLM digests

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | Indexed (with `window_end DESC` as composite). |
| `window_start`, `window_end` | DateTime NOT NULL | The time range this digest covers. |
| `summary` | Text NOT NULL | The LLM's paragraph. |
| `highlights` | JSONB NULLABLE | `key_concerns`, `recommended_actions`, plus the raw aggregates we fed in. |
| `model_used` | String(64) NOT NULL | |
| `usage` | JSONB NULLABLE | |
| `created_at` | DateTime NOT NULL | |

### Indexes

- `ix_incident_summaries_tenant_window` — composite `(tenant_id, window_end DESC)`. Most queries are "give me the latest 20 digests for this tenant"; this index covers them as a single range scan.

### Why we persist rather than recompute on each admin view

LLM calls are expensive and slow. Every admin opening the Digests tab would re-bill the API. Persisting once + serving many is the right trade.

---

## Cross-cutting conventions

### `tenant_id` everywhere

Every domain table has `tenant_id NOT NULL FK ON DELETE RESTRICT`. The RESTRICT is deliberate — we never want a tenant delete to cascade-delete user data. Tenant deletion is a deliberate, gated admin operation (today: manual; future: a dedicated tombstone migration path).

### TimestampMixin

`Job` and `User` use the `TimestampMixin` which adds `created_at` and `updated_at` with `func.now()` defaults and `onupdate`. Other tables use plain `created_at` (no updated_at) because they're append-only (audit_logs, outbox_events, job_events, incident_summaries).

### `PortableJSON`

`backend/app/models/base.py` defines `PortableJSON` — a SQLAlchemy `TypeDecorator` that renders as JSONB on Postgres and JSON-as-text on SQLite. Tests use SQLite; prod uses JSONB. Without this every JSONB column would error in tests.

### Migrations

Every schema change goes through Alembic. Conventions:

- One migration per logical change.
- Always include `downgrade()` — even if it's `op.execute("...")` for state we can't undo cleanly, the down path must exist so partial deploys can revert.
- Inline rationale at the top of every migration. Future engineers (you, in 6 months) need to understand the *why*, not just the *what*.
- Never hand-edit a generated revision after merging. New change = new revision.

The Alembic chain (current head: `e1d24a8b50c2`):

```
a01d04e830dc  initial schema
b2a8f9c7e103  outbox_events
c3e9f1a4d802  job_events
d4b1a8e60305  DAG + sagas
e7f4c2a91b08  job_triages
f8a1c4e23507  multi-tenancy (tenants table, tenant_id FKs)
a9c2d1e83104  per-tenant idempotency
b3d8e7a52116  tenant quotas
c4f8e9a52340  row-level security
d9c01a7e4f30  platform admin flag
e1d24a8b50c2  incident_summaries  (current head)
```

---

## Pointers

- `backend/app/models/*.py` — the model definitions
- `backend/alembic/versions/*.py` — the migration history
- `backend/app/models/base.py` — `Base`, `TimestampMixin`, `PortableJSON`
- `backend/app/models/enums.py` — `UserRole`, `JobType`, `JobStatus`, `SagaStatus`
- `backend/app/models/__init__.py` — model registry (every model must be imported here for Alembic to see it)
