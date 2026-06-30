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
| `max_retries` | Integer (default 3) | Per-job cap. |
| `priority` | Integer (default 0) | Indexed. Higher = more urgent. Currently informational; the priority-aware queue exists in `app/workers/queue.py` (legacy delayed-retry path) but Kafka itself doesn't respect priority — every event is FIFO within a partition. |
| `trace_id` | String(255) NULLABLE | Indexed. Carries the OTel trace ID so the UI can show "request → API → worker → DB" as one trace. Pasted into the admin filter to find a specific request. |
| `started_at`, `completed_at` | DateTime NULLABLE | Filled by the dispatcher on transition. Used by SLOs. |
| `saga_id` | UUID NULLABLE FK → sagas.id ON DELETE SET NULL | Phase 7. Non-null = this job is a saga step. |
| `created_at`, `updated_at` | DateTime | TimestampMixin. |

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

`DependencyResolver` consumer reads `job.completed` events and promotes `WAITING` children to `PENDING` when all parents are done.

---

## `sagas` — multi-step workflows

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | |
| `name` | String(255) | User-supplied label, e.g. "weekly billing run". |
| `status` | String(50) | `running / completed / failed / compensating / compensated` (SagaStatus enum). |
| `created_at`, `completed_at` | DateTime | |

A saga is a container; the actual steps live in `jobs` with `saga_id` set. Step ordering is encoded via `job_dependencies` — step N depends on step N-1.

`SagaCoordinator` consumer marks the saga complete when all steps are completed, or kicks off compensation (`{type}.compensate` jobs in reverse order) when any step dead-letters.

---

## `audit_logs` — what happened, when, by whom

Append-only log of every meaningful action (job creation, replay, incident resolved, saga created, login, user registration, tenant created, etc.).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID NOT NULL FK → tenants.id | Indexed. |
| `user_id` | UUID NULLABLE FK → users.id ON DELETE SET NULL | Nullable because some events have no user (e.g. system-emitted events). |
| `job_id` | UUID NULLABLE FK → jobs.id ON DELETE SET NULL | Nullable because not every audit event is job-related. |
| `action` | String(100) NOT NULL | Indexed. Snake_case verb, e.g. `job.created`, `job.replayed`, `saga.completed`, `tenant.created`. |
| `resource_type`, `resource_id` | String(100), String(255) | Free-form. The convention is `resource_type=job, resource_id=<uuid>` etc. |
| `request_id` | String(255) | The HTTP request that triggered the action. Used to correlate audit events across services. |
| `ip_address` | String(50) | |
| `extra_data` | JSONB NULLABLE | Per-event freeform. e.g. `{retry_count: 5, error: "..."}` for `job.dead_letter`. |
| `created_at` | DateTime | When the action happened. |

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
| `attempts` | Integer NOT NULL (default 0) | Incremented each time the relay tries to publish; surfaces stuck rows. |
| `created_at` | DateTime NOT NULL | |
| `published_at` | DateTime NULLABLE | NULL = unpublished. Filled by the relay on success or on permanent failure (with `error_message`). |
| `error_message` | Text NULLABLE | Set when the row is permanently dropped (schema validation failure). Distinguishes "stuck retrying" from "given up." |

### Indexes

- `ix_outbox_events_published_at_null` — **partial index** WHERE `published_at IS NULL`. Critical for the hot polling path; without it, the relay scan would degrade to a full table scan as the table grows.

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
