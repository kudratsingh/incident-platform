# CLAUDE.md — Incident & Workflow Platform

## Project Overview

A production-style **Incident & Workflow Platform** — an internal enterprise operations tool where teams submit jobs (CSV upload, report generation, bulk API sync, document analysis), watch live progress, inspect failures/retries/audit history, and where admins can replay failed jobs and inspect request traces.

This is NOT a generic CRUD app. It intentionally forces: concurrency model decisions, structured logging with trace IDs, retry/idempotency patterns, background job orchestration, event-driven architecture, real debugging workflows, and production deployment concerns.

The project is structured as a sequence of milestone phases (Phase 1 through Phase 13). Each phase ships as one or more pull requests against `master`. The plan is *aspirational on the right-hand side* (Phases 8, 9, 11, 13 are not yet built) and *historical on the left* (Phases 1–7, 10, 12 are merged and running) — see the per-phase status markers in the milestone plan below.

---

## Documentation map

This file (`CLAUDE.md`) is the high-signal index. Treat it as the entry point — everything below points at deeper docs when detail matters.

**Prose conventions** (documented so reviews don't rediscover them as violations):
- Em-dashes ( — ) are used freely across docs, ADRs, and PR bodies. Not stylistic churn — match the pattern.
- ADR status labels appear in the H1 subtitle only. The doc-map above intentionally omits *(proposed)* / *(accepted)* markers to avoid drift when statuses flip.
- Machine-principal identity on `audit_logs` uses `principal_type` (string discriminator) + `principal_id` (plain UUID, no FK). Not `service_account_id` — that shape was rejected because the same column has to reference either `users.id` or `service_accounts.id` depending on the discriminator. See [ADR 0007](docs/ADR/0007-machine-principal-scope-model.md).
- The agent contract with this platform is validated by snapshot-testing the agent repo against the pinned platform image (agent-repo ADR 0007). There is deliberately no `agent-tools.json` artifact — that shape was proposed in early drafts and dropped when [ADR 0006](docs/ADR/0006-mcp-server-standalone-process.md) landed.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — runtime topology, request lifecycles (annotated traces for the 5 most-touched paths), concurrency model, failure mode catalog, auth & tenant matrix, cost model
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — every table, every column, every index, every constraint, with a one-line *why*
- [`docs/KAFKA.md`](docs/KAFKA.md) — topic catalog, schema-evolution rules, partition strategy, consumer-group catalog with failure isolation
- [`docs/REDIS.md`](docs/REDIS.md) — key catalog (writer / reader / TTL / eviction-safe?), what degrades when Redis dies
- [`docs/ADR/`](docs/ADR/) — architecture decision records. Read these to understand *why* the platform looks the way it does:
  - [0001 — Outbox over CDC](docs/ADR/0001-outbox-vs-cdc.md)
  - [0002 — JSON Schema over Protobuf](docs/ADR/0002-json-schema-vs-protobuf.md)
  - [0003 — Postgres RLS as defense-in-depth](docs/ADR/0003-rls-as-defense-in-depth.md)
  - [0004 — Composite tenant_id:user_id Kafka partition key](docs/ADR/0004-tenant-id-in-kafka-partition-key.md)
  - [0005 — LLM features fail open](docs/ADR/0005-llm-features-fail-open.md)
  - [0006 — MCP server as a standalone process from the platform codebase](docs/ADR/0006-mcp-server-standalone-process.md)
  - [0007 — Machine principals with a scope model separate from human roles](docs/ADR/0007-machine-principal-scope-model.md)
  - [0008 — Chaos framework is triple-gated and never in production](docs/ADR/0008-chaos-gating.md)
  - [0010 — Idempotency record lifecycle](docs/ADR/0010-idempotency-record-lifecycle.md)
- [`docs/postmortems/`](docs/postmortems/) — one file per incident (backfilled or written at the time). Format: Impact / Timeline / Root cause / Detection gap / Fix / Prevention rule adopted.
  - [0009 — Consumer lifecycle and supervision](docs/ADR/0009-consumer-lifecycle-and-supervision.md)
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — open extension ideas, sized + categorized
- [`runbooks/`](runbooks/) — machine-readable on-call playbooks for every CloudWatch alarm + SLO

---

## Current Implementation Status

What's actually shipped (as of the most recent merge):

| Phase | Status | Anchor PRs |
|---|---|---|
| 1 — Clean Backend Core | ✅ Complete | early `#1`–`#10` range |
| 2 — Background Execution | ✅ Complete | retry / dispatcher / processors |
| 3 — Frontend + Debugging Realism | ✅ Complete | dashboard, job detail, admin console |
| 4 — Production Deployment | ✅ Complete | Docker, Terraform, ECS, CloudWatch metrics |
| 5 — Hardening | ✅ Complete | rate limit, cache, load tests, mypy strict |
| 6 — Observability & Reliability | ✅ Complete | OTel `#21`, circuit breaker `#23`, SLOs + runbooks `#32` |
| 7 — Kafka + Advanced Patterns | ✅ Complete | foundations `#28`, CQRS + event sourcing `#29`, DAG + sagas `#30`, frontend `#31` |
| 8 — Platform Engineering & Scale | 🟡 Not started | — |
| 9 — Security Hardening | 🟡 Not started | — |
| 10 — AI / LLM Integration | ✅ Complete | DLQ triage `#34`, retry policy `#39`, NL queries `#40`, digests `#41` |
| 11 — Real-time Stream Analytics | 🟡 Not started | — |
| 12 — Multi-tenancy | ✅ Complete | model + auth `#35`, enforcement `#36`, RLS + partitioning + quotas `#37`, platform admin `#38` |
| 13 — Disaster Recovery & Chaos | 🟡 Not started | — |
| 14 — Real Job Processors | 🟡 Deferred (post-agent) | — |

**Runtime topology that's actually running** (post-Phase-12):

- **One FastAPI app** behind the ALB. `POST /jobs` is rate-limited (per-client + per-tenant), backpressure-gated, quota-checked, and writes both the job row and an `outbox_events` row in a single DB transaction.
- **Eight Kafka consumer groups** running concurrently inside the worker process (`worker_loop` in `app/workers/dispatcher.py`):
  1. `worker-dispatcher` — pops `job.submitted`, runs the actual processor
  2. `audit-writer` — appends `event.*` rows to `audit_logs`
  3. `sse-broadcaster` — bridges Kafka events to the Redis pub/sub channel SSE clients read
  4. `event-log` — appends every lifecycle event to the immutable `job_events` table (event sourcing)
  5. `read-model` — maintains Redis-backed denormalized per-tenant + per-user job-status sets (CQRS read side; tenant-keyed since Phase 12 PR D)
  6. `dependency-resolver` — promotes `WAITING` jobs to `PENDING` when their parents complete
  7. `saga-coordinator` — drives saga-level state and compensation on failure
  8. `llm-triage` (Phase 10) — calls Claude on every `job.dlq` to write a `JobTriage` row
- **Four background loops** also running in the same process:
  - **Outbox relay** — polls `outbox_events` every second and publishes to Kafka
  - **Delayed-retry promote** — moves exponentially-backed-off retries from a Redis sorted-set back into Kafka via the outbox
  - **Metrics loop** — emits CloudWatch gauges (`QueueDepth`, `InFlightJobs`, `ConsumerLag`) and caches the lag in Redis for the backpressure check
  - **Digest loop** (Phase 10) — every `LLM_DIGEST_INTERVAL_HOURS` (default 24), generates a per-tenant incident summary via Claude and persists it to `incident_summaries`

See [`docs/KAFKA.md`](docs/KAFKA.md) for the full consumer-group catalog (failure isolation, partition strategy, schema-evolution rules) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#per-task-responsibilities) for the worker-loop responsibilities table.

Per-PR breakdown of Phase 7 specifically (for reference when reading the code):

- **`#28` — foundations**: JSON Schema registry validating every Kafka payload, `BackpressureError` (503) on `POST /jobs` when consumer lag exceeds threshold, Redpanda-via-Testcontainers integration test.
- **`#29` — read side**: `job_events` table + `EventLogConsumer` (`UNIQUE (kafka_topic, kafka_partition, kafka_offset)` for redelivery dedup), `ReadModelProjector` maintaining Redis sets keyed by `job_id` (idempotent under at-least-once), `GET /admin/jobs/{id}/timeline` and `GET /admin/stats`.
- **`#30` — orchestration**: `job_dependencies` table, `JobStatus.WAITING` / `CANCELLED`, `SagaStatus` enum, `POST /sagas` creates a chain of dependent jobs, `SagaCoordinator` cancels downstream and enqueues `{type}.compensate` jobs on dead-letter.
- **`#31` — frontend**: Sagas browse / create / detail pages, Kafka event timeline on `JobDetailPage` (admin only), CQRS stats overview tab, optional dependencies field on the job form, `backpressure` 503 toast.
- **`#32` — Phase 6 gaps**: `GET /admin/slos` with budget-remaining + burn-rate per objective, `runbooks/*.yaml` (7 runbooks for every CloudWatch alarm and SLO), `GET /admin/runbooks/{id}`, SLO scorecards + runbook modal in the admin UI, two new fast-burn alarms in Terraform.

Phase 10 — AI / LLM Integration:

- **`#34` — DLQ triage**: `app/services/triage.py` calls Claude on every `job.dlq`. Pydantic-typed analysis (root_cause_category, summary, suggested_fix, is_retryable, confidence) persisted to `job_triages`. Admin UI shows the analysis on the DLQ row + the job detail page.
- **`#39` — LLM-guided retry policy**: after the first deterministic retry, the worker consults Claude to decide retry-with-backoff (with recommended seconds) vs dead-letter-now. Falls back to deterministic on any error. Audit log records `dead_lettered_by: llm_retry_policy` + reasoning. Small purple LLM badge in the admin DLQ tab.
- **`#40` — Natural-language admin queries**: `POST /admin/query` translates plain English into a constrained `JobFilterSpec` Pydantic model, then runs through `JobService.list_jobs`. Injection-safe by construction (the LLM can only fill enum/literal fields). Off-by-default + 503 on any failure.
- **`#41` — Periodic incident summaries**: `_digest_loop` runs every N hours, aggregates per-tenant failure stats (counts + top recurring error fingerprints), asks Claude for a one-paragraph narrative + key concerns + recommended actions, persists to `incident_summaries`. New admin Digests tab.

Phase 12 — Multi-tenancy:

- **`#35` — model + auth context**: `tenants` table, `tenant_id` on every domain table, `DEFAULT_TENANT_ID` bootstrap (mixed-hex UUID for SQLite compat). JWT carries `tenant_id` claim. `tenant_id_var` contextvar logged in every structured entry.
- **`#36` — enforce tenant_id everywhere**: every repository / service / outbox call site threads tenant_id through. Per-tenant composite UNIQUE on `(tenant_id, idempotency_key)` replaces the global UNIQUE. Cascading signature changes across ~30 call sites.
- **`#37` — RLS + Kafka partition key + quotas**: Postgres row-level security policy on 6 tables; `get_current_user` sets `app.tenant_id` via `set_config`; Kafka partition key changes to composite `{tenant_id}:{user_id}` across all 9 producer call sites; `tenants.rate_limit_per_minute` + `tenants.quota_jobs_per_month` columns; `check_tenant_limits` runs at top of `POST /jobs`. Header chip + admin Tenants tab. Testcontainers Postgres integration test.
- **`#38` — platform admin role**: `users.is_platform_admin` boolean (data migration backfills for default-tenant admins); `require_platform_admin` dependency; `?tenant_id=` cross-tenant scope override on list endpoints; CQRS read-model keyed by tenant_id (fixed a Phase 12 leak); self-service tenant creation at `/auth/register` via `new_tenant_name`; admin Tenants tab with create-modal + drill-down page.

---

## Agent-facing surface

The platform exposes an MCP server for machine principals such as `incident-commander`. The code lives at `backend/app/mcp/` and deploys as a standalone process from the same image, with handlers calling the service layer directly. Every MCP request authenticates as a scoped service account, is rate-limited per principal, and writes an immutable audit record. Topology rationale and rejected alternatives (mounted sub-app, API-proxy à la Sentry, separate repo) are in [ADR 0006](docs/ADR/0006-mcp-server-standalone-process.md). Tool changes always start with a PR here, never in the agent repo.

### Repo boundary

- **This repo (`incident-platform`)** owns everything on the platform side of the wire: backend, frontend, service layer, `backend/app/mcp/` server code, chaos hooks, approvals subsystem, audit log, infra. The MCP server is part of the lock, not the visitor.
- **Agent repo (`incident-commander`)** owns the MCP *client* and the orchestrator around it: hypothesis engine, memory, skills, evals, demo compose that pulls the platform image by digest. It never imports platform code — it talks to `PLATFORM_MCP_URL` with a bearer token.

Same mental model as Sentry / GitHub / Stripe / Linear: the MCP server ships inside the org whose data it fronts; callers live wherever their builders keep them.

### Where it fits

- **Step 0 (merged, PR #51)** — ADRs 0006–0008, agent-facing surface section, naming locked in.
- **Wave 1 (~6 PRs, blocking):** machine principals + scoped tokens *(PR #52, merged)*; operator audit log; MCP scaffold at `backend/app/mcp/` + `get_consumer_lag` only; chaos framework + `kill_consumer` (`CHAOS_ENABLED=false` by default); alert emission (HMAC-signed webhook + `list_active_alerts` poll fallback); release engineering — pinned platform image (`ghcr.io/kudratsingh/incident-platform`) on tag, agent repo consumes by digest.
- **Wave 2 (lands during agent Phases 1–3):** full read tool set (`list_dlq_messages`, `get_trace` / `search_traces`, `get_deploy_history`, `get_dag_state`, `get_redis_health`, `get_postgres_health`, `get_incident` / `list_incidents`); remaining chaos hooks (`poison_message`, `saturate_redis`, `inject_latency`, `bad_deploy`) added JIT per scenario family.
- **Wave 3 (before agent Phase 6):** Tier 1 actions (`restart_consumer_group`, `replay_dlq_messages`, `pause_dag`, `invalidate_cache_key`) with `Idempotency-Key`; approvals subsystem (propose / approve / execute state machine with param-hash binding, expiry, single-use); approvals inbox view in the existing frontend; Tier 2 actions (`scale_service`, `rollback_deploy`, `modify_retry_policy`, `trigger_saga_compensation`) requiring approval reference + global kill switch on the agent principal.

### Design decisions locked in Step 0

- [ADR 0006 — MCP server as a standalone process from the platform codebase](docs/ADR/0006-mcp-server-standalone-process.md) — code at `backend/app/mcp/`, standalone process built from the same image, handlers call the service layer directly. Import-linter rule enforces `app.mcp → app.services` one-directional. Revisit trigger: collapse to mounted if operating two services proves to be real friction (one-line change).
- [ADR 0007 — Machine principals with a scope model separate from human roles](docs/ADR/0007-machine-principal-scope-model.md) — `service_accounts` table; opaque `sa_<random>` bearer tokens; five fixed scopes, non-hierarchical, additive, orthogonal to the human role enum. Shipped in PR #52.
- [ADR 0008 — Chaos framework is triple-gated and never in production](docs/ADR/0008-chaos-gating.md) — `CHAOS_ENABLED` env flag + `chaos:invoke` scope + per-tool blast-radius check; Terraform validation refuses `CHAOS_ENABLED=true` in the production workspace.

### Naming conventions (normative)

**Tool names** — verb-first `snake_case`, one function per tool. Examples: `get_consumer_lag`, `list_dlq_messages`, `list_audit_events`, `restart_consumer_group`, `replay_dlq_messages`, `pause_dag`, `invalidate_cache_key`, `kill_consumer`, `poison_message`, `saturate_redis`, `inject_latency`, `bad_deploy`. `snake_case` matches Pydantic field style and serializes cleanly through the MCP `tools/list` response.

**Scopes** — `<domain>:<verb>`, fixed enum. Adding a scope is a decision; renaming or splitting one is a token migration. The five scopes:

| Scope | Grants |
|---|---|
| `telemetry:read` | Observability read surface — consumer lag, queue depth, in-flight counts, traces, health snapshots. |
| `incidents:read` | Incident-response read surface — DLQ contents, incident summaries, saga state, per-job history. |
| `actions:propose` | Create a proposal for a Tier 1 or Tier 2 action; does not execute. |
| `actions:execute` | Execute an approved proposal (Tier 1 idempotent, Tier 2 requires an approval reference). |
| `chaos:invoke` | Invoke chaos framework tools. Additionally gated by `CHAOS_ENABLED`. |

The seed principal `incident-commander` gets `telemetry:read + incidents:read` and nothing else.

**Audit events** — same `<resource>.<verb>` snake-case shape as existing events. Every machine-principal action carries `principal_type='service_account'` on the audit row:

- `service_account.created` / `service_account.token_minted` / `service_account.token_revoked`
- `agent.tool_invoked` — every MCP tool call; `extra_data` carries `tool_name`, `arguments`, `scope_used`, `latency_ms`, `outcome`.
- `agent.action_proposed` / `agent.action_approved` / `agent.action_executed` / `agent.action_rejected`
- `chaos.tool_invoked` / `chaos.tool_denied` — chaos activity is a separate stream from `agent.tool_invoked` so it filters cleanly on the Audit tab.

### Runtime shape

- Two deployables from one image: `api` (the existing FastAPI app) and `mcp` (the ASGI entrypoint at `backend/app/mcp/standalone.py`). Same commit, same schemas, same service layer.
- Each process gets its own DB pool sized to its rate limits — the MCP process runs a small pool.
- The agent points at `PLATFORM_MCP_URL` for tools and `PLATFORM_REST_URL` for anything else (there shouldn't be much — everything the agent needs should surface as an MCP tool over time).
- Contract stability between agent and platform is verified by contract snapshot testing against the pinned image, per agent-repo ADR 0007.

---

## Stack

### Backend
- **Python 3.12+ / FastAPI** — async API gateway
- **PostgreSQL** — system of record. Tables: `users`, `tenants`, `jobs`, `audit_logs`, `outbox_events`, `job_events`, `job_dependencies`, `sagas`, `job_triages`, `incident_summaries`. Full reference in [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).
- **Redis** — cache, locks, rate limits, pub/sub progress events, CQRS read-model sets, cached backpressure lag value.
- **Kafka** (Redpanda locally, Amazon MSK in production) — durable event log; decouples job submission from execution, powers event sourcing and fan-out. Topics: `job.submitted` / `job.progress` / `job.completed` / `job.failed` / `job.dlq`.
- **JSON Schema** — every Kafka topic has a schema in `backend/app/schemas/kafka/`; producer and consumer validate on every message.
- **Object storage** — S3 in production, MinIO locally — for uploaded files and artifacts.
- **Worker layer** — asyncio tasks for I/O-heavy work, a threading adapter for blocking SDKs, a multiprocessing pool for CPU-heavy transforms.
- **Anthropic SDK** (Phase 10, complete) — Claude API for DLQ triage, LLM-guided retry policy, natural-language admin queries, and periodic incident summaries. All features off-by-default; fail open on any API issue. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).

### Frontend
- **React + Vite + TypeScript + Tailwind**
- Pages: `LoginPage`, `RegisterPage`, `DashboardPage` (job list + create form), `JobDetailPage` (live SSE progress + Kafka event timeline), `AdminPage` (overview / jobs / DLQ / runbooks / users / audit tabs), `SagasPage` / `SagaNewPage` / `SagaDetailPage` (multi-step workflow management).
- Shared components: `Layout`, `StatusBadge`, `ProgressBar`, `TraceId`, `Toast`, `JobForm`, `ProtectedRoute`, `ErrorBoundary`, `Skeleton`.

### Infrastructure
- **Docker / Docker Compose** for local dev (Postgres + Redis + Redpanda + MinIO + backend + frontend).
- **Terraform** for the full AWS stack in `infra/` — VPC, ECS Fargate, RDS, ElastiCache, ALB, ECR, IAM, Secrets Manager, S3, MSK, CloudWatch alarms with runbook URLs in their descriptions.
- **CI/CD** — `.github/workflows/ci.yml`: frontend tsc + tests, ruff + mypy on backend, pytest with coverage gate, Docker build + push to ECR, ECS service update.
- Cloud target: **AWS ECS/Fargate**; **Amazon MSK** for Kafka; **RDS Postgres**; **ElastiCache Redis**; **S3** for artifacts.

### Testing
- `pytest` with fixtures, parametrization, factories.
- Layers: **unit** (`backend/tests/unit/`), **API contract** (`backend/tests/api/`), **integration** (`backend/tests/integration/` — Testcontainers, Docker-gated). Load tests in `backend/tests/load/` (Locust).
- `mypy --strict` in CI on the `app` package.
- `ruff check backend/` in CI.
- Coverage gate at 70% (see `pyproject.toml`).
- Current test count: **240+** passing (161 unit + 82 API + 3 gated integration).

---

## Architecture

The actual runtime topology after Phase 7:

```
                    Frontend (React)
                          │
                          ▼
           ┌──────── FastAPI Gateway ────────┐
           │  POST /jobs                     │
           │   ├─ rate limit (Redis)         │
           │   ├─ check_backpressure (Redis) │
           │   └─ tx: jobs row + outbox row  │
           │  GET  /jobs/{id}/stream  (SSE)  │
           │  GET  /admin/stats, /slos, …    │
           └────────────────┬────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ▼                   ▼                   ▼
   PostgreSQL          Redis (cache,        Kafka (Redpanda / MSK)
   ─────────────       pub/sub, sets,       ─────────────────────
   users               rate limits)         Topics:
   jobs                                       job.submitted
   audit_logs          Keys:                  job.progress
   outbox_events         jobs:status:*        job.completed
   job_events            jobs:user:*:…        job.failed
   job_dependencies      job:progress:{id}    job.dlq
   sagas                 kafka:consumer_lag…
   job_triages           jobs:create:* (RL)
                            ▲
                            │ (CQRS read model)
                            │
           ┌────────────────┼────────────────────────────────┐
           │   Worker process (single ECS task)              │
           │                                                 │
           │   Eight Kafka consumer groups (concurrent):     │
           │     1. worker-dispatcher   → _run_job           │
           │     2. audit-writer        → audit_logs rows    │
           │     3. sse-broadcaster     → Redis pub/sub      │
           │     4. event-log           → job_events rows    │
           │     5. read-model          → Redis sets         │
           │     6. dependency-resolver → promote children   │
           │     7. saga-coordinator    → compensation       │
           │     8. llm-triage          → job_triages rows   │
           │                                                 │
           │   Four supporting loops:                        │
           │     • outbox relay (DB → Kafka)                 │
           │     • delayed-retry promote (Redis → outbox)    │
           │     • metrics loop (gauges + lag cache)         │
           │     • digest loop (per-tenant LLM summary)      │
           │                                                 │
           │   Three concurrency models:                     │
           │     • asyncio  → bulk_api_sync                  │
           │     • thread   → csv_upload                     │
           │     • process  → doc_analysis, report_gen       │
           └─────────────────────────────────────────────────┘

   Object storage (S3 / MinIO)          OpenTelemetry → AWS X-Ray
   Structured JSON logs → CloudWatch    CloudWatch alarms → SNS → email
```

---

## Core Features

### 1. Auth + Session Model
- Login with access + refresh tokens (JWT).
- Roles: `user`, `support`, `admin`. Support and admin share the privileged-read path; admin alone can list users.
- Audit trail for important actions (job creation, replay, incident resolved, saga created); the audit consumer adds a second `event.*` row from Kafka so the audit log is also event-sourced.
- Dependency-based auth guards via FastAPI `Depends`.
- Clean error handling via a custom `AppError` hierarchy with predictable JSON shape (`error_code`, `message`, `details`, `request_id`).

### 2. Job Submission Pipeline
- `POST /jobs` validates input → checks backpressure → writes job row + outbox row in one DB transaction → outbox relay publishes `job.submitted` to Kafka → dispatcher consumer runs the processor → state transitions emit `job.progress` / `job.completed` / `job.failed` (also via outbox).
- Async API endpoints throughout.
- Strategy-pattern processor map (`async_tasks.py`, `thread_adapters.py`, `cpu_processors.py`).
- Retry with exponential backoff. Failed jobs stay in the system; exhausted jobs go to `DEAD_LETTER` and `job.dlq`.
- Idempotency keys on job creation (DB unique constraint).
- Dependencies: jobs can declare parent jobs; the DependencyResolver consumer promotes them.

### 3. Live Progress Streaming
- Server-Sent Events at `GET /jobs/{id}/stream`.
- Worker publishes only to Kafka; SSE consumer bridges Kafka → Redis pub/sub → SSE clients. UI shows real-time progress bar + scrolling event log.
- Non-DLQ `job.failed` events render as `retrying` (with backoff countdown text); DLQ as `dead_letter`. This distinction comes from the SSE consumer mapping, not the worker.

### 4. Admin Incident Console
- Tabs: **Overview** (CQRS stats cards + SLO scorecards), **Jobs** (filter by status / trace ID / type), **DLQ** (with per-type breakdown pills and per-row Replay/Resolve), **Runbooks** (clickable list with a modal showing the diagnosis steps), **Users**, **Audit** (clickable rows opening a metadata modal).
- Replay (`/admin/jobs/{id}/replay`) resets `retry_count` to 0 and records the previous values in the audit log's `extra_data`.
- Event-sourced **job timeline** on `JobDetailPage` (admin only) hits `/admin/jobs/{id}/timeline` and renders every Kafka event for that job in offset order with topic/partition/offset metadata.

### 5. Sagas — Multi-Step Workflows
- `POST /sagas` creates a saga + an ordered chain of jobs, each depending on the previous.
- `SagaDetailPage` polls every 2s and shows the step chain as a vertical timeline with status dots that go green as each step completes.
- On dead-letter of any step: saga goes `COMPENSATING`, downstream waiting jobs are cancelled, and `{type}.compensate` jobs are enqueued for already-completed prior steps in reverse order.

### 6. Concurrency — Use All Three Models Deliberately

| Model | Use For |
|---|---|
| `asyncio` | API calls to third-party services, high-concurrency I/O, live updates, streaming status |
| `threading` | Blocking SDKs, file upload helpers, log shipping, wrapping legacy blocking functions |
| `multiprocessing` | CPU-heavy CSV parsing, document transformation, PDF/text extraction, data aggregation |

The processor map in `app/workers/dispatcher.py` routes each job type to the right concurrency model.

---

## Code Style & Conventions

### General
- Full type hints across all app code — FastAPI derives real value from typing.
- `mypy --strict` must pass.
- Service / repository layer separation.
- Explicit request/response Pydantic models (DTOs in `app/schemas/`).
- Resource-oriented API routes.
- Correlation/trace IDs on every request via `RequestContextMiddleware`.

### Python Patterns to Use Naturally (not artificially)

- **Decorators** — auth checks, timing/profiling, retry wrappers, audit logging, feature flags, caching.
- **Context managers** — DB sessions/transactions, timing blocks, temporary files, distributed lock acquire/release, structured logging scopes.
- **Dataclasses / Pydantic models** — typed domain models (`TriageAnalysis`, `SagaStep`, `SLOState`), value objects, job command objects.
- **Repository / service pattern** — `JobRepository`, `JobService`, `SagaService`, `OutboxRepository`, etc.
- **Custom exception hierarchy** — `AppError` → `NotFoundError`, `AuthorizationError`, `JobError`, `RateLimitError`, `BackpressureError`, etc.
- **Strategy pattern** — pluggable job processors via `_PROCESSORS` dict in `dispatcher.py`.
- **Mixins** (limited, one subsystem) — `TimestampMixin` on models; explicitly reason about MRO if more added.
- **Descriptors** (one meaningful use) — open opportunity; not yet used.
- **`**kwargs`** — in configurable base service classes, adapters, logging helpers.

---

## Structured Logging

Every log entry should carry:
- `request_id` / `trace_id`
- `job_id`
- `user_id`
- `route`
- `latency`
- `retry_count`

Implementation: `app/core/logging.py` uses `python-json-logger`. `app/core/middleware.py` sets `request_id_var` and `trace_id_var` (contextvars) on each request; the formatter pulls them in.

OTel auto-instrumentation is enabled for FastAPI, SQLAlchemy, and Redis. Spans propagate from API → worker via the OTel `traceparent` carrier serialized into the job payload at create time and re-extracted in `_run_job`.

Logs must be queryable by trace ID end-to-end: browser → API → worker → result.

---

## API Design Principles

- Resource-oriented routes.
- Explicit request/response schemas (Pydantic).
- Predictable error shapes (`AppError` produces a consistent envelope).
- Correlation IDs on all responses (`X-Request-ID`, `X-Trace-ID` headers).
- Idempotent job creation via `idempotency_key`.
- Pagination, filtering, sorting on list endpoints (`PaginationParams` base).
- Backwards-compatible versioning (everything under `/api/v1/`).
- OpenAPI docs auto-generated by FastAPI at `/api/v1/docs`.

---

## Testing Strategy

### Layers
1. **Unit tests** (`backend/tests/unit/`) — services, processors, validators, repositories, consumers. 161 tests. No I/O.
2. **API contract tests** (`backend/tests/api/`) — full FastAPI app with dependency overrides; SQLite in-memory DB; mocked Redis. 82 tests.
3. **Integration tests** (`backend/tests/integration/`) — Testcontainers with Redpanda. Docker-gated; opt-in via `pytest backend/tests/integration/`.
4. **Load tests** (`backend/tests/load/`) — Locust scenarios for the job submission path.
5. **Failure-mode tests** — circuit-breaker open/close, schema validation rejecting bad payloads, redelivery dedup via unique constraints.

### Tooling
- `pytest` fixtures and parametrization.
- Factories for test data (inline `_make_*` helpers; could grow into proper factories later).
- Testcontainers for the Redpanda integration test (Docker availability is skip-gated).
- Coverage gate at 70% in `pyproject.toml`.
- All unit + API tests run on every PR via `ci.yml`.

---

## Data Structures & Algorithms (Natural Usage)

| DS/A | Where It Appears |
|---|---|
| Hash maps/sets | Deduplication, membership checks, idempotency keys, **CQRS read-model sets in Redis** |
| Queues | Job processing pipeline (Kafka topics), outbox |
| Priority queues / heaps | Redis sorted set for the priority queue (legacy path, still used by delayed-retry) |
| Sliding window / ring buffer | Rate limiting (Redis INCR + EX) |
| Sorting | Result ordering, pagination, ordering events by `(recorded_at, kafka_offset)` |
| Caching (LRU, TTL) | Redis cache layer (`JobCache`) |
| Binary search | Time-series pagination helpers (`Job.created_at` indexed) |
| **Graph thinking** | **Job dependency DAG** (Phase 7) — children, transitively unmet count, cycle-free by construction |
| **Topological order** | Saga step chain (each depends on the previous) |

---

## Performance Tradeoffs to Explore

- Sync vs async endpoints.
- Eager vs lazy loading from DB (`lazy="noload"` on relationships; we explicitly fetch when needed).
- Query count vs memory usage (N+1 awareness).
- Batching vs latency.
- Caching vs consistency (Redis TTL strategies, CQRS eventual consistency).
- Process pool overhead vs CPU speedup.
- JSON serialization size.
- WebSocket vs polling (we use SSE).
- Precomputed aggregates vs live queries (the CQRS overview tab uses precomputed sets).

---

## Advanced Python Patterns (Senior / Principal Level)

These go beyond Phase 1–5 and should be introduced naturally in later phases.

- **Structured concurrency** — `asyncio.TaskGroup` (Python 3.11+) for fan-out/fan-in; cancel all sibling tasks on first failure. The `worker_loop` currently uses `asyncio.gather` + per-task `cancel()`; converting to `TaskGroup` is a clean refactor.
- **Protocols + structural subtyping** — replace ABCs with `typing.Protocol` where duck typing is the right model (e.g. storage backends, queue backends).
- **ParamSpec + Concatenate** — type-safe decorator factories preserving the wrapped function's full signature (retry wrappers, audit decorators).
- **`__init_subclass__`** — self-registering plugin pattern for job processors; adding a new processor class auto-registers it without touching the dispatcher.
- **`tracemalloc` + memory profiling** — instrument long-lived workers to detect leaks; track top allocations per snapshot delta.
- **Slot classes** — `__slots__` on hot-path domain objects (`ProgressEvent`) to reduce per-instance memory at scale.
- **Custom pickling** — `__reduce__` / `__getstate__` / `__setstate__` on objects passed to the multiprocessing pool.
- **`contextlib.AsyncExitStack`** — dynamic composition of async context managers in the worker lifecycle.
- **Generic repositories** — `Repository[ModelT, PKT]` with bounded type vars; we already have `BaseRepository[ModelT]` — could tighten the bound.
- **Descriptor protocol** — validated config fields using `__set_name__` / `__get__` / `__set__`; open opportunity.

---

## System Design Patterns Demonstrated

Concrete implementation pointers for each pattern this project demonstrates end-to-end.

| Concept | Where It Lives | Notes |
|---|---|---|
| **At-least-once delivery** | `BaseKafkaConsumer._process_one` commits offset only after `handle_message` returns | Combined with idempotency keys to avoid double-execution |
| **Exactly-once dedup via unique constraint** | `job_events.uq_job_events_kafka_coord` on `(topic, partition, offset)` | Kafka redelivery → `IntegrityError` → consumer swallows + commits |
| **Backpressure** | `app/utils/backpressure.py` checks Redis-cached `ConsumerLag` from the dispatcher; `POST /jobs` raises `BackpressureError` (503) | Threshold in settings; metrics loop populates the cache |
| **Circuit breaker** | `app/utils/circuit_breaker.py` wraps external API calls | Open / half-open / closed states; metrics emitted |
| **Read/write split (CQRS)** | `app/workers/read_model.py` (write path) + `GET /admin/stats` / `/admin/users/{id}/stats` (read path) | Sets keyed by `job_id` are idempotent under at-least-once |
| **Outbox pattern** | `outbox_events` table; written in same tx as `jobs` mutation; `_outbox_relay_loop` publishes to Kafka | Survives crash between DB commit and broker publish |
| **Event sourcing** | `job_events` table; `EventLogConsumer` appends every lifecycle event; `GET /admin/jobs/{id}/timeline` replays | Immutable; `recorded_at` order preserved by per-partition serial processing |
| **Saga pattern** | `app/services/saga.py` (creation) + `app/workers/saga_coordinator.py` (lifecycle + compensation) | `{type}.compensate` jobs enqueued in reverse order on DLQ |
| **Job dependency DAG** | `job_dependencies` table; `DependencyResolver` consumer; `JobStatus.WAITING` | Cycle-free by construction (deps reference only existing jobs) |
| **Schema evolution** | `backend/app/schemas/kafka/*.schema.json` validated on both producer and consumer | `additionalProperties: true` for backward compatibility |
| **Dead-letter queue** | `job.dlq` topic; `dead_letter` job status; `/admin/dlq/*` endpoints; `LlmTriageConsumer` (Phase 10) analyses each entry | Admin replay resets `retry_count`; unregistered `.compensate` types also route here |
| **Fan-out / fan-in** | Seven Kafka consumer groups subscribed to the lifecycle topics, all processing independently | No coordination needed; each group has its own offset |
| **Consumer group isolation** | Each consumer in `worker_loop` is its own group; failure of one doesn't affect others | `started: list[BaseKafkaConsumer]` filters out failed starters |
| **Distributed locking** | Redis `SETNX` for job deduplication (open opportunity in the rate-limit code path) | Idempotency key is the primary dedup mechanism today |
| **Connection pool sizing** | SQLAlchemy `pool_pre_ping=True`; pool tuning is a Phase 8 item (PgBouncer) | — |
| **Time-series partitioning** | Phase 8 item: partition `audit_logs` by month | — |
| **SLOs + error budgets** | `app/services/slo.py` computes from `jobs` table; `GET /admin/slos` returns budget remaining + burn rate | 14.4× fast-burn alarms in `infra/cloudwatch.tf` |
| **Structured runbooks** | `runbooks/*.yaml` at repo root; `GET /admin/runbooks{,/{id}}`; CloudWatch alarm descriptions reference runbook URLs | Admin UI surfaces them next to the SLO scorecards |

---

## Memory & Resource Awareness

- Streaming / chunked processing for large uploads — don't hold entire files in memory.
- Ensure large uploaded objects are not accidentally retained by closures or callbacks.
- Avoid reference cycles in callback-heavy or closure-heavy worker code.
- Monitor memory growth in long-lived workers (Phase 8: `tracemalloc` snapshots in performance tests).

---

## LLM Integration (Phase 10 — Complete)

The project uses the **Anthropic Python SDK** to add four LLM-powered features. All shipped, all off by default, all fail open (the platform runs fine without an `ANTHROPIC_API_KEY`).

### The four features

1. **DLQ triage** (PR #34) — `LlmTriageConsumer` subscribes to `job.dlq`; Claude classifies the failure. Persisted to `job_triages`; surfaced on the admin DLQ tab and `JobDetailPage`.
2. **LLM-guided retry policy** (PR #39) — after the first deterministic retry, the worker asks Claude retry-with-backoff vs dead-letter-now. Falls back to deterministic on any error; audit log records `dead_lettered_by: llm_retry_policy` + reasoning.
3. **Natural-language admin queries** (PR #40) — `POST /admin/query` translates plain English into a Pydantic `JobFilterSpec` (enum/literal fields only → injection-safe by construction).
4. **Periodic incident summaries** (PR #41) — the `_digest_loop` runs every `LLM_DIGEST_INTERVAL_HOURS` and writes a one-paragraph per-tenant digest to `incident_summaries`.

### Shared conventions

- **Default model**: `claude-opus-4-7` with `thinking: {type: "adaptive"}`. Failure diagnosis and retry classification are intelligence-sensitive; the per-call cost is small.
- **Structured outputs** — every LLM call uses `client.messages.parse()` with a Pydantic `output_format`. The response is shape-checked before it ever reaches the DB.
- **Prompt caching** — every service caches its frozen system prompt via `cache_control: {"type": "ephemeral"}`. Volatile per-call context goes into the user message, after the cache breakpoint.
- **Feature flags** — `LLM_TRIAGE_ENABLED`, `LLM_RETRY_POLICY_ENABLED`, `LLM_NL_QUERY_ENABLED`, `LLM_DIGEST_ENABLED`. All default `False`. Tests pass without network access.
- **Fail open** — see [ADR 0005](docs/ADR/0005-llm-features-fail-open.md). Any API error / timeout / schema mismatch falls back to a deterministic non-LLM path (or 503 for NL queries, or a skipped digest).
- **Cost telemetry** — `usage.cache_read_input_tokens` / `cache_creation_input_tokens` / `input_tokens` / `output_tokens` are persisted on each row for cache-hit visibility.

### SDK surface (verified against `anthropic 0.112.0`)

All four services use `client.messages.parse(..., output_format=SomePydanticModel, thinking={"type":"adaptive"})` and read `response.parsed_output` on model `claude-opus-4-7`. Verified against the installed SDK: `messages.parse` accepts all these kwargs; `ThinkingConfigAdaptiveParam` accepts `{"type":"adaptive"}`; `ParsedMessage.parsed_output` exists; `claude-opus-4-7` is in the model literal. Cache-hit fields (`cache_read_input_tokens`, `cache_creation_input_tokens`) are `Optional[int]` on `Usage` — the shared `_llm_usage.extract_usage` helper coerces `None` to `0` so downstream aggregation works.

---

## Milestone Plan

### Phase 1: Clean Backend Core ✅
- FastAPI app structure, Postgres models, auth, job creation, status endpoints.
- Service / repository layers, type hints everywhere, pytest setup.
- **Focus:** style, architecture, tests, API design.

### Phase 2: Background Execution ✅
- Queue, retries, progress tracking.
- Async I/O tasks, one thread-based adapter, one process-based CPU step.
- **Focus:** concurrency choices, idempotency, failure handling.

### Phase 3: Frontend + Debugging Realism ✅
- Dashboard, job details, live updates, admin incident console.
- Request correlation IDs visible in the UI.
- **Focus:** Network-tab debugging, auth bugs, frontend/backend contracts.

### Phase 4: Production Deployment ✅
- Docker, cloud deployment, managed Postgres / Redis / storage.
- Secrets / config management, structured logging, metrics, alerting, CI/CD.
- **Focus:** shipping, runtime debugging, environment parity.

### Phase 5: Hardening ✅
- Rate limiting, load testing, caching, test matrix.
- Static checks, performance profiling, chaos / failure scenarios.
- **Focus:** senior-level polish.

### Phase 6: Observability & Reliability ✅
- **OpenTelemetry** distributed tracing — spans across API → worker → DB → Redis, exported to OTLP (AWS X-Ray or Jaeger).
- **Custom metrics** — `JobCompleted`, `JobFailed`, `JobDeadLettered`, `QueueDepth`, `InFlightJobs`, `ConsumerLag` on the `IncidentPlatform` CloudWatch namespace.
- **SLOs + error budgets** ✅ — `job_completion_rate` ≥ 99% and `job_dispatch_latency` ≥ 95% within 30 s, both over rolling 24h. `GET /admin/slos` returns current state, budget remaining %, and burn rate.
- **CloudWatch alarms** ✅ — five baseline alarms (`alb-5xx`, `backend-tasks-low`, `rds-cpu-high`, `redis-memory-low`, `queue-depth-high`) plus two SLO fast-burn alarms (14.4× over 1h windows). All notify via SNS topic `${app_name}-alarms`.
- **Circuit breaker** ✅ — `app/utils/circuit_breaker.py` wraps external API calls; opens on N consecutive failures, half-open probe, auto-recover.
- **Structured runbooks** ✅ — `runbooks/*.yaml` at repo root; one per alarm + one per SLO breach (7 total). Each has summary, symptoms, diagnosis steps (with copy-pasteable shell commands), mitigation, escalation, related dashboards. Alarm descriptions reference `/admin/runbooks/{id}` so on-call has a one-click path from PagerDuty.
- **Focus:** production observability, on-call readiness, failure isolation.

### Phase 7: Kafka + Advanced Architecture Patterns ✅
- **Kafka integration (end-to-end)** ✅
  - **Producer**: `app/workers/kafka_producer.py` publishes lifecycle events; `publish_raw` propagates schema-validation errors so the outbox can mark rows failed.
  - **Consumer groups**: eight, all running concurrently in the worker process (seven were shipped in Phase 7; `llm-triage` joined in Phase 10). See "Current Implementation Status" above for the full list.
  - **Partitioning strategy**: every event keyed by `user_id` so per-user ordering is preserved within each consumer group.
  - **Offset management**: committed only after `handle_message` returns successfully — at-least-once. Combined with idempotency keys (jobs) and a unique constraint (event log) to avoid double effects.
  - **Dead-letter topic**: `job.dlq`. Admin UI inspects (with per-type breakdown) and replays. Replay resets `retry_count` (a bug we fixed in `#27`).
  - **Schema Registry** ✅ — file-based JSON Schema in `backend/app/schemas/kafka/`; format checker on (enforces `uuid` etc.); producer + consumer validate on every message.
  - **Local dev**: Redpanda in `docker-compose.yml`; MSK in production via `infra/msk.tf`.
  - **Testing** ✅ — Testcontainers-based integration test in `backend/tests/integration/test_kafka_e2e.py` spins up Redpanda on a pre-allocated host port and verifies producer ↔ consumer round-trip with schema validation on both ends. Skipped if Docker isn't available.
- **Outbox pattern** ✅ — `outbox_events` table; written in same transaction as job state changes; `_outbox_relay_loop` polls every second and publishes. Partial index on `published_at IS NULL` for hot-path scan.
- **CQRS** ✅ — `ReadModelProjector` maintains Redis sets per status (global + per-user); `GET /admin/stats` reads only those, no SQL aggregate on `jobs`. Sets keyed by `job_id` are idempotent under at-least-once delivery.
- **Event sourcing** ✅ — `job_events` table appended by `EventLogConsumer`; `GET /admin/jobs/{id}/timeline` replays. Frontend renders a vertical timeline with topic/partition/offset per row.
- **Saga pattern** ✅ — `POST /sagas` creates a saga + chain of dependent jobs (sharing `saga_id`). `SagaCoordinator` marks the saga complete when all steps finish; on dead-letter it cancels downstream and enqueues `{type}.compensate` jobs for completed prior steps in reverse order. Compensation processors are application responsibility — an unregistered `*.compensate` will dead-letter, which is the intended forcing function.
- **Job dependency DAG** ✅ — `job_dependencies` (many-to-many self-join); `JobStatus.WAITING` for jobs with unmet deps; `DependencyResolver` consumer promotes children when all parents complete. Cycles impossible by construction.
- **Backpressure** ✅ — dispatcher consumer exposes `consumer_lag()`; metrics loop emits `ConsumerLag` and caches the value in Redis with TTL; `check_backpressure` reads the cache and raises `BackpressureError` (503) when lag exceeds threshold. API never queries Kafka directly.
- **Focus:** Kafka end-to-end (produce → consume → DLQ → replay), distributed systems correctness, event-driven architecture.

### Phase 8: Platform Engineering & Scale 🟡
- **HTTPS + ACM** — add TLS to the ALB with an ACM cert; redirect HTTP → HTTPS; enforce HSTS.
- **Terraform remote state** — S3 bucket + DynamoDB lock table for shared state; enable team collaboration on infra.
- **Staging environment** — second Terraform workspace (`staging`) with smaller instance sizes; CI deploys to staging on PR merge, production on manual approval.
- **Blue/green deployments** — ECS CodeDeploy integration; shift traffic from blue to green with automatic rollback on health check failure.
- **ECS auto-scaling** — scale backend tasks on queue depth (custom metric) and CPU; scale-in protection during active job processing.
- **PgBouncer** — connection pooling sidecar in ECS task; tune pool size vs DB `max_connections`; measure connection wait time.
- **Read replicas** — RDS read replica for analytics / admin queries; route read-heavy endpoints to replica via separate DB session.
- **Database partitioning** — partition `audit_logs` and `job_events` by month (range partitioning); measure query speedup on time-bounded queries.
- **Feature flags** — lightweight Redis-backed feature flag system; enable new job types per-user or per-role without deploys.
- **Focus:** zero-downtime deployments, horizontal scale, cost optimization.

### Phase 9: Security Hardening 🟡
- **WAF** — AWS WAF in front of ALB; rate limiting at the network layer, SQL injection / XSS rules, geo-blocking.
- **Secret rotation** — automatic rotation of DB password and JWT secret in Secrets Manager; app picks up new secrets without restart.
- **VPC flow logs + CloudTrail** — log all network traffic and API calls; ship to S3 + Athena for forensic queries.
- **Dependency scanning** — `pip-audit` and `npm audit` in CI; fail on high-severity CVEs; auto-PR for patch updates via Dependabot.
- **OWASP hardening** — security headers (CSP, X-Frame-Options, HSTS) in Nginx; validate all user input at system boundaries; SQL injection impossible via parameterized queries (verify with sqlmap).
- **mTLS between services** — mutual TLS for backend → RDS and backend → Redis using ACM Private CA; eliminates credential-based auth for internal traffic.
- **Least-privilege IAM** — audit and tighten ECS task role to exact S3 paths and exact Secrets Manager ARNs; no wildcard permissions.
- **Focus:** defence in depth, compliance readiness, zero-trust networking.

### Phase 10: AI / LLM Integration ✅
- **LLM-driven DLQ triage** (PR #34) — Anthropic SDK; per dead-lettered job, Claude classifies the root cause, summarises the failure, suggests a fix, and rates retryability + confidence. Persisted to `job_triages`. Surfaced on the DLQ tab and `JobDetailPage`.
- **LLM-guided retry policy** (PR #39) — after the first deterministic retry, Claude sees the error + context and decides "retry with backoff" (with recommended seconds) vs "dead-letter now". Falls back to deterministic on any error. Audit log records `dead_lettered_by: llm_retry_policy` + reasoning.
- **Natural-language admin queries** (PR #40) — `POST /admin/query` translates plain English into a constrained Pydantic `JobFilterSpec`; the model can only fill enum/literal fields so the query is injection-safe by construction. Off by default → 503 on failure.
- **Periodic incident summaries** (PR #41) — the `_digest_loop` runs every `LLM_DIGEST_INTERVAL_HOURS`, aggregates per-tenant failure stats (digit-normalized fingerprints, top 5 recurring errors), asks Claude for a one-paragraph narrative + key concerns + recommended actions, persists to `incident_summaries`.
- **Shared conventions**: `client.messages.parse()` with a Pydantic `output_format` (no raw JSON); frozen system prompt with `cache_control: ephemeral`; `claude-opus-4-7` with adaptive thinking; usage block persisted for cost telemetry; every feature off by default.
- **Focus:** structured outputs, prompt caching for cost, graceful degradation when the LLM is offline, observable cost telemetry. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).

### Phase 11: Real-time Stream Analytics 🟡
- **Kafka Streams or Flink** topology consuming the lifecycle topics; materialized views for live customer-facing dashboards (per-tenant throughput, latency percentiles).
- **ClickHouse** for historical OLAP (millions of events queryable in under a second).
- **Pre-aggregated time-series materialized views** — 1-minute, 1-hour, 1-day rollups for the dashboard; backfilled from the immutable Kafka log.
- **Customer-facing analytics API** — query parameters validated; tenant isolation enforced at the storage layer.
- **Sub-second p99** target on the live dashboard endpoints.
- **Focus:** streaming semantics (windowing, watermarks), low-latency reads at scale, OLTP / OLAP separation.

### Phase 12: Multi-tenancy 🟡
- **Tenant model** — `tenants` table; every existing tenant-scoped table gets a `tenant_id` FK; all queries scoped by tenant.
- **Postgres row-level security** — RLS policies on every tenant-scoped table; backend connects with a tenant-scoped role; impossible to leak data across tenants even with a query bug.
- **Per-tenant Kafka partitioning** — change the partition key from `user_id` to `tenant_id` (or hash of `(tenant_id, user_id)`); per-tenant ordering guaranteed.
- **Per-tenant rate limits and quotas** — Redis-backed counters keyed by `tenant_id`; quotas configurable per plan.
- **Per-tenant billing telemetry** — emit a `usage.*` event per chargeable action; aggregate by tenant for monthly invoices.
- **Tenant admin UI** — root admins can list tenants, switch contexts, view per-tenant SLO scorecards.
- **Focus:** isolation guarantees (data, compute, blast radius), per-tenant observability, fair-share scheduling.

### Phase 13: Disaster Recovery & Chaos 🟡
- **Multi-region active-passive** — primary in `us-east-1`, warm standby in `us-west-2`; failover via Route 53 health checks.
- **Cross-region Kafka MirrorMaker 2** — `job.*` topics mirrored continuously; consumer offsets translated.
- **RDS cross-region read replica** + automated promotion runbook.
- **S3 cross-region replication** for artifacts.
- **RPO / RTO SLOs** — RPO ≤ 60 seconds, RTO ≤ 15 minutes; verified quarterly via a game-day exercise.
- **Chaos tests in CI** — `litmus` or `gremlin` injects controlled failures: kill the dispatcher mid-job, drop the broker, partition the DB, simulate DNS failure. Each test asserts the system recovers within an expected window.
- **Backup restore drill** — automated weekly job that restores from snapshot to a scratch RDS instance, runs a smoke test, and reports.
- **Focus:** real distributed systems chops — verifying assumptions about failure modes rather than just talking about them.

### Phase 14: Real Job Processors 🟡

Deferred until the incident-commander agent is wired up and driving eval scenarios against the platform. Today's processors (`app/workers/async_tasks.py`, `thread_adapters.py`, `cpu_processors.py`) simulate work with sleeps + progress updates + deliberate failure paths — intentional for the agent story (deterministic behaviour, easy failure injection, no external side effects during eval runs). Once the agent's Phase 6 remediation loop is stable, these upgrade to do real work so the platform demo has substance beyond the infrastructure story.

- **`csv_upload`** — accept a real file upload via `POST /jobs` multipart, stream to MinIO/S3, parse rows with progress updates every 1000 rows, write parse results back to storage, emit row counts to CloudWatch.
- **`bulk_api_sync`** — hit a real third-party HTTP API (Anthropic, GitHub, or a stub echo service), page through results with rate-limit backoff, persist to the DB. Exercises the circuit-breaker from Phase 6.
- **`report_gen`** — generate a real PDF from job data (matplotlib or WeasyPrint), upload to storage, emit a signed URL back on completion. Demonstrates the multiprocessing pool actually doing CPU-heavy work.
- **`doc_analysis`** — extract text from an uploaded PDF (PyMuPDF or pdfplumber), run a real Anthropic call for summarisation when `LLM_TRIAGE_ENABLED=true`, persist the summary.
- **Real failure modes** — each processor gains realistic failure surfaces: partial file corruption in `csv_upload`, 429s in `bulk_api_sync`, OOM guards in `doc_analysis`. These become new eval-scenario fodder for the agent.
- **Focus:** turning the platform demo from "look at the infrastructure" into "look at the infrastructure moving real work through it" — mostly UX polish for stakeholders who don't read the audit log. Zero impact on the agent's investigation surface (observability is already there).

---

## Repo Structure

```
├── CLAUDE.md                       # this file
├── README.md
├── docker-compose.yml              # postgres + redis + redpanda + minio + backend + frontend
├── Dockerfile                      # backend image (Python 3.12 + uvicorn)
├── alembic.ini
├── pyproject.toml                  # deps, mypy, ruff, pytest config
│
├── .github/
│   └── workflows/
│       └── ci.yml                  # lint, type, test, frontend, build, deploy
│
├── runbooks/                       # machine-readable runbooks (one per alarm + SLO)
│   ├── rb-alb-5xx.yaml
│   ├── rb-ecs-tasks-low.yaml
│   ├── rb-rds-cpu-high.yaml
│   ├── rb-redis-memory-low.yaml
│   ├── rb-queue-depth-high.yaml
│   ├── rb-slo-job-completion.yaml
│   └── rb-slo-dispatch-latency.yaml
│
├── backend/
│   ├── alembic/                    # DB migrations (11, head: e1d24a8b50c2)
│   │   └── versions/
│   │       ├── a01d04e830dc_initial_schema.py
│   │       ├── b2a8f9c7e103_outbox_events.py
│   │       ├── c3e9f1a4d802_job_events.py
│   │       ├── d4b1a8e60305_dag_and_sagas.py
│   │       ├── e7f4c2a91b08_job_triages.py
│   │       ├── f8a1c4e23507_multi_tenancy.py
│   │       ├── a9c2d1e83104_per_tenant_idempotency.py
│   │       ├── b3d8e7a52116_tenant_quotas.py
│   │       ├── c4f8e9a52340_row_level_security.py
│   │       ├── d9c01a7e4f30_platform_admin.py
│   │       └── e1d24a8b50c2_incident_summaries.py
│   ├── app/
│   │   ├── main.py                 # FastAPI app factory + lifespan (start_producer, worker_loop)
│   │   ├── config.py               # Settings / env config (pydantic-settings)
│   │   ├── dependencies.py         # Shared FastAPI dependencies (get_db, get_current_user, …)
│   │   │
│   │   ├── api/                    # HTTP routers
│   │   │   ├── auth.py
│   │   │   ├── jobs.py
│   │   │   ├── sagas.py            # POST /sagas, GET /sagas, GET /sagas/{id}
│   │   │   ├── admin.py            # /stats /slos /runbooks /dlq/* /jobs/{id}/{timeline,triage,replay}
│   │   │   ├── audit.py
│   │   │   └── streaming.py        # SSE progress stream
│   │   │
│   │   ├── models/                 # SQLAlchemy models
│   │   │   ├── base.py             # Base + PortableJSON + TimestampMixin
│   │   │   ├── enums.py            # UserRole, JobType, JobStatus, SagaStatus
│   │   │   ├── tenant.py           # Phase 12 — tenants table + DEFAULT_TENANT_ID
│   │   │   ├── user.py             # + is_platform_admin (Phase 12 PR D)
│   │   │   ├── job.py
│   │   │   ├── job_dependency.py   # many-to-many self-join on jobs
│   │   │   ├── audit.py
│   │   │   ├── outbox.py           # outbox_events
│   │   │   ├── event_log.py        # job_events (event sourcing)
│   │   │   ├── saga.py
│   │   │   ├── triage.py           # job_triages (Phase 10)
│   │   │   └── digest.py           # incident_summaries (Phase 10 PR #41)
│   │   │
│   │   ├── schemas/                # Pydantic request/response DTOs
│   │   │   ├── kafka/              # JSON Schema for each Kafka topic
│   │   │   │   ├── job_submitted.schema.json
│   │   │   │   ├── job_progress.schema.json
│   │   │   │   ├── job_completed.schema.json
│   │   │   │   └── job_failed.schema.json    # also used by job.dlq
│   │   │   ├── job.py
│   │   │   ├── user.py
│   │   │   ├── auth.py
│   │   │   ├── audit.py
│   │   │   └── common.py           # PaginationParams, PaginatedResponse
│   │   │
│   │   ├── repositories/
│   │   │   ├── base.py             # generic BaseRepository[ModelT]
│   │   │   ├── tenant.py
│   │   │   ├── user.py
│   │   │   ├── job.py
│   │   │   ├── job_dependency.py
│   │   │   ├── audit.py
│   │   │   ├── outbox.py
│   │   │   ├── event_log.py
│   │   │   ├── saga.py
│   │   │   ├── triage.py
│   │   │   └── digest.py
│   │   │
│   │   ├── services/
│   │   │   ├── auth.py             # + self-service tenant creation (Phase 12 PR D)
│   │   │   ├── job.py              # JobService — create_job, replay_job, list_jobs
│   │   │   ├── saga.py             # SagaService — create_saga (chain of jobs)
│   │   │   ├── slo.py              # SLO computation from jobs table
│   │   │   ├── runbooks.py         # YAML loader
│   │   │   ├── triage.py           # Phase 10 — DLQ triage LLM service
│   │   │   ├── retry_policy.py     # Phase 10 — LLM-guided retry policy
│   │   │   ├── nl_query.py         # Phase 10 — NL admin queries → JobFilterSpec
│   │   │   └── incident_digest.py  # Phase 10 — periodic per-tenant digests
│   │   │
│   │   ├── workers/
│   │   │   ├── dispatcher.py       # JobDispatcherConsumer + worker_loop (starts all 8 consumers + 4 loops)
│   │   │   ├── async_tasks.py      # asyncio — bulk_api_sync
│   │   │   ├── thread_adapters.py  # threading — csv_upload
│   │   │   ├── cpu_processors.py   # multiprocessing — doc_analysis, report_gen
│   │   │   │
│   │   │   ├── kafka_producer.py   # publish_* + publish_raw (validation surfaces errors)
│   │   │   ├── kafka_consumer.py   # BaseKafkaConsumer — schema validation, offset commit
│   │   │   ├── schema_registry.py  # JSON Schema loader + validate()
│   │   │   │
│   │   │   ├── audit_consumer.py        # group: audit-writer
│   │   │   ├── sse_consumer.py          # group: sse-broadcaster
│   │   │   ├── event_log_consumer.py    # group: event-log
│   │   │   ├── read_model.py            # group: read-model (per-tenant keys since PR #38)
│   │   │   ├── dependency_resolver.py   # group: dependency-resolver
│   │   │   ├── saga_coordinator.py      # group: saga-coordinator
│   │   │   ├── triage_consumer.py       # group: llm-triage (Phase 10)
│   │   │   │
│   │   │   ├── queue.py            # Redis priority queue (delayed retries; pop_ready_delayed)
│   │   │   └── progress.py         # Redis pub/sub progress events (SSE bridge target)
│   │   │
│   │   ├── core/                   # exceptions, logging, middleware, redis, security, tracing, metrics
│   │   └── utils/                  # rate_limit, quota, cache, decorators, mixins, backpressure, circuit_breaker
│   │
│   └── tests/
│       ├── unit/                   # 161 tests
│       ├── api/                    # 82 tests
│       ├── integration/            # Testcontainers (Docker-gated: Redpanda, Postgres for RLS)
│       ├── load/                   # Locust
│       └── conftest.py             # SQLite-in-memory + dependency overrides + default_tenant fixture
│
├── frontend/
│   ├── Dockerfile                  # Node build → Nginx
│   ├── nginx.conf                  # SPA serving, no proxy (ALB handles /api/)
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx                 # Router + routes
│       ├── main.tsx
│       ├── types.ts                # Mirror of backend Pydantic schemas
│       ├── pages/
│       │   ├── LoginPage.tsx
│       │   ├── RegisterPage.tsx
│       │   ├── DashboardPage.tsx
│       │   ├── JobDetailPage.tsx   # SSE progress + Kafka event timeline (admin)
│       │   ├── AdminPage.tsx       # tabs: overview / jobs / dlq / runbooks / users / audit
│       │   ├── SagasPage.tsx
│       │   ├── SagaNewPage.tsx
│       │   └── SagaDetailPage.tsx
│       ├── components/             # Layout, StatusBadge, ProgressBar, Toast, TraceId, JobForm, …
│       ├── hooks/                  # useAuth, useJobStream (SSE)
│       ├── api/                    # client.ts, auth.ts, jobs.ts, sagas.ts, admin.ts
│       └── utils/                  # tokens, format (status colors, job-type labels)
│
├── infra/                          # Terraform — full AWS stack
│   ├── main.tf                     # provider, backend config
│   ├── variables.tf
│   ├── outputs.tf
│   ├── ecr.tf
│   ├── networking.tf               # VPC, subnets, IGW, security groups
│   ├── iam.tf                      # ECS execution + task roles
│   ├── secrets.tf
│   ├── s3.tf
│   ├── rds.tf
│   ├── elasticache.tf
│   ├── msk.tf                      # Amazon MSK + topics
│   ├── alb.tf
│   ├── ecs.tf                      # Cluster, task definitions, Fargate services
│   └── cloudwatch.tf               # SNS topic + 7 alarms (5 baseline + 2 SLO fast-burn)
│
└── scripts/                        # seed data, migrations, ops helpers
    ├── entrypoint.sh               # runs alembic upgrade head then uvicorn
    └── seed_load_test_users.py
```

---

## Working with This Project — A Few Practical Notes

- **Run the backend locally:** `docker compose up postgres redis redpanda minio -d`, then `./.venv/bin/uvicorn app.main:app --reload --app-dir backend`. Set `KAFKA_BOOTSTRAP_SERVERS=localhost:9092` and run the worker as a separate process (the same `app.main` lifespan starts both, so for local dev you typically just run the API and the worker fires in the same process).
- **Run the frontend:** `cd frontend && npm run dev` — proxies `/api` to `http://localhost:8000`.
- **Run tests:** `cd backend && ../.venv/bin/python -m pytest tests/` (skips integration unless you explicitly target `tests/integration/`). CI runs `mypy -p app`, `ruff check backend/`, and `pytest` on every PR.
- **Add a migration:** `cd backend && ../.venv/bin/alembic revision --autogenerate -m "describe change"` — but always **read the generated file** before committing; autogenerate misses things like enum updates and partial indexes.
- **Add a Kafka consumer group:** subclass `BaseKafkaConsumer`, implement `handle_message`, instantiate in `worker_loop` in `app/workers/dispatcher.py`. The base class does schema validation, offset management, and per-message error handling.
- **Add a CloudWatch alarm:** add it to `infra/cloudwatch.tf`, then add the matching `runbooks/rb-*.yaml` file and reference its `/admin/runbooks/{id}` URL in the alarm description.
- **Add an SLO:** declare in `SLOS` in `app/services/slo.py`, write a `runbooks/rb-slo-*.yaml`, and (optionally) add a fast-burn alarm in `infra/cloudwatch.tf`.
- **Memory:** the user's auto-memory directory at `~/.claude/projects/.../memory/MEMORY.md` carries durable preferences across sessions — including branching convention (always feature branch, open PR, let user review), no Claude co-authoring on commits, and `.venv/bin/python` for everything.

---

## Glossary

Terms used throughout this codebase. When in doubt, use these exact words.

- **Backpressure** — the API's rejection of new job submissions when the dispatcher's Kafka consumer group is more than `Settings.backpressure_lag_threshold` messages behind. Raises `BackpressureError` (503). Lag is cached in Redis with TTL 90s by the metrics loop; the API never round-trips to Kafka for this check.
- **Burn rate** — the multiplier of SLO error budget being consumed. 1× = budget burns at the rate that exhausts it exactly at the end of the window; 14.4× = budget exhausted in 1 hour out of a 24h window. Fast-burn alarms fire at 14.4×.
- **Compensation** — saga rollback action. When a saga step dead-letters, the coordinator enqueues `{type}.compensate` jobs for already-completed prior steps in reverse order. Application is responsible for registering processors for `*.compensate` types — an unregistered compensation job will dead-letter, which is the intended forcing function.
- **CQRS read model** — Redis-backed denormalized job-status sets keyed by `(tenant_id, status)` and `(user_id, status)`. The `read-model` Kafka consumer projects writes from the lifecycle topics; `GET /admin/stats` reads via `SCARD` with no SQL aggregate.
- **Dead-letter** — a job's terminal failure state after exhausting retries (or after the LLM-guided retry policy decides not to retry). Distinct from `failed`; jobs in `failed` will retry, jobs in `dead_letter` won't. Surfaced in `job.dlq` topic and on the admin DLQ tab.
- **Dispatch latency** — wall-clock time from `pending` to `running`. The `job_dispatch_latency` SLO targets 95% within 30s.
- **Error budget** — the inverse of the SLO. 99% SLO → 1% error budget. The Overview tab shows budget remaining %; fast-burn alarms fire when burn rate threatens to consume the budget early.
- **Event-sourced** — describes the `audit_logs` and `job_events` tables. Every Kafka lifecycle event is appended as a row, in arrival order, immutably. The mutable `jobs.status` is a *projection* of these events.
- **Fail open** — when a non-critical dependency is unavailable, allow the request through rather than blocking it. Rate limits fail open on Redis outage; LLM features fail open on Anthropic outage. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).
- **Fast-burn alarm** — CloudWatch alarm watching SLO burn rate over a short window. Fires before the budget is fully consumed so the team can react.
- **Fingerprint (digest)** — the digit-normalized truncation of an error message used by the incident-summary feature to bucket recurring errors. "attempt 1" and "attempt 27" fingerprint to "attempt #" — same bucket.
- **Idempotency key** — caller-supplied string on `POST /jobs`. Composite UNIQUE on `(tenant_id, idempotency_key)`. Re-submitting with the same key returns the existing job rather than creating a duplicate.
- **In-flight** — describes a job that's been popped from Kafka and is currently executing in the dispatcher's task set. Tracked in `consumer.in_flight`; emitted as the `InFlightJobs` CloudWatch gauge.
- **JobFilterSpec** — the constrained Pydantic shape Claude returns from the NL query feature. Enum/literal fields only; the model can never smuggle SQL.
- **LLM badge** — small purple `LLM` tag on the admin DLQ row indicating the LLM-guided retry policy forced the dead-letter before retries were exhausted.
- **Outbox** — the transactional handoff between DB state changes and Kafka publication. Same transaction writes the state change and the `outbox_events` row; a background relay publishes within ~1s. See [ADR 0001](docs/ADR/0001-outbox-vs-cdc.md).
- **Partition key** — `{tenant_id}:{user_id}` composite string. Preserves per-tenant + per-user ordering. See [ADR 0004](docs/ADR/0004-tenant-id-in-kafka-partition-key.md).
- **Platform admin** — `users.is_platform_admin = true`. Cross-tenant operator who can list/create tenants and pass `?tenant_id=` to scope list/stats endpoints to any tenant. Distinct from a `role=admin` (tenant admin).
- **RLS policy** — Postgres row-level security; the second line of defense against cross-tenant data leaks. Defense in depth on top of the application-layer `tenant_id` filter. See [ADR 0003](docs/ADR/0003-rls-as-defense-in-depth.md).
- **Saga step** — a job inside a saga (`saga_id IS NOT NULL`). Steps are linearly ordered via `job_dependencies` (step N depends on step N-1). Saga lifecycle is driven by the `saga-coordinator` consumer.
- **Tenant admin** — `role=admin` without the platform flag. Can manage everything within their own tenant; cannot see sibling tenants.
- **Trace ID** — the OTel trace identifier, propagated from browser → API → worker → DB. Logged on every entry, stored on `jobs.trace_id`, paste-filterable on the admin Jobs tab.
- **Triage analysis** — the LLM-produced classification of a dead-lettered job. Persisted to `job_triages`; one row per job (UNIQUE constraint makes Kafka redelivery a no-op).

---

## Auth & tenant matrix (quick reference)

Three role tiers, with `is_platform_admin` as an additive cross-tenant flag. Full matrix and per-tab permissions in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#auth--tenant-matrix). Quick read:

| Capability | `user` | `support` | `admin` (tenant) | `+is_platform_admin` |
|---|---|---|---|---|
| Create + see own jobs | ✓ | ✓ | ✓ | ✓ |
| See other users' jobs (own tenant) | — | ✓ | ✓ | ✓ |
| Replay / resolve | — | ✓ | ✓ | ✓ |
| Admin Tenants tab | — | — | — | ✓ |
| Cross-tenant `?tenant_id=` | — | — | — | ✓ |
| Manage tenant limits | — | — | — | ✓ |

Enforcement is layered: application-layer filter in `JobService.list_jobs` + Postgres RLS via `set_config('app.tenant_id', …)` in `get_current_user`. RLS catches the bug class "forgot a WHERE clause".

---

## Failure mode catalog (quick reference)

What degrades when a component dies. Full detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#failure-mode-catalog).

| Component down | What still works | What degrades |
|---|---|---|
| Postgres | Nothing | All API requests 500 |
| Redis | API, DB writes, workers | Rate limits / backpressure / cache / SSE updates fail open |
| Kafka (MSK) | API accepts new jobs (outbox queues them) | New job execution stalls; SSE updates stop |
| Anthropic API | Everything | DLQ triages absent; retry policy → deterministic; NL queries return 503; digests stall |
| Worker process | API accepts new jobs (outbox queues them) | No job execution; restart resumes from committed offsets |
| API process | Worker, other replicas | New HTTP requests fail until replica restarts |

The truth lives in Postgres + Kafka. Redis and Anthropic are performance + UX dependencies, never correctness ones.

---

## LLM cost model (quick reference)

Approximate per-call costs at Opus 4.7 pricing. Full breakdown + cache-hit telemetry shape in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#cost-model-llm-features).

| Feature | Per call | Typical cadence | Per month (representative) |
|---|---|---|---|
| DLQ triage | ~$0.012 | every dead-letter | ~$3 |
| Retry policy | ~$0.005 | every retry past first | ~$15 |
| NL admin query | ~$0.006 | per admin search | ~$5 |
| Incident digest | ~$0.018 | per tenant per day | ~$2 |

Total Phase-10 LLM spend: <$30/mo in a representative setup. Cache hit rates and token counts are stored on every record (`usage` JSONB column on `job_triages` and `incident_summaries`).

---

## Conventions (expanded)

### Errors

Custom exception hierarchy rooted at `AppError` (`backend/app/core/exceptions.py`). Each subclass declares `status_code` and `error_code`. The middleware catches `AppError` and produces a uniform JSON envelope:

```json
{
  "error_code": "quota_exceeded",
  "message": "Monthly job quota reached for tenant acme (100000 / 100000).",
  "details": {},
  "request_id": "..."
}
```

When adding a new error: subclass `AppError`, set `status_code` + `error_code`, raise from the service layer. Don't catch and re-wrap arbitrary exceptions — the middleware turns uncaught exceptions into 500s with `error_code: internal_error`.

### Migrations

- One migration per logical change.
- Always include `downgrade()`. Even if it's `op.execute("...")` with state we can't fully undo, the down path must exist.
- Inline a rationale at the top of every migration. Six months from now, the *why* is the most valuable thing in the file.
- Never edit a generated revision after it's merged. New change = new revision.
- After `alembic revision --autogenerate`, **read the diff** — autogenerate misses enum updates, partial indexes, and data-only changes.

### Tests

Three layers, each with a clear purpose. Picking the right one matters:

- **Unit (`backend/tests/unit/`)** — services, processors, validators, repositories, consumers. **No I/O**. SQLite in-memory if a DB is needed (via the `db_session` fixture); mocks for Redis/Kafka/Anthropic. 161 tests.
- **API contract (`backend/tests/api/`)** — full FastAPI app via httpx ASGITransport, dependency overrides swap in SQLite + mock Redis. Tests request/response shape + auth + error envelope. 82 tests.
- **Integration (`backend/tests/integration/`)** — real Postgres or Redpanda via Testcontainers. Docker-gated (skipped on `RUN_RLS_TEST=1` for the RLS test, etc.). Tests the things only a real DB / broker can prove (RLS enforcement, Kafka redelivery, schema validation end-to-end).

When in doubt: write a unit test. Move up only when you need the real thing.

### Audit log entries

The convention is `<resource>.<verb>` snake-case: `job.created`, `job.replayed`, `saga.completed`, `tenant.created`, `user.registered`, `incident.resolved`. Resource-only events (`job.dead_letter`) drop the verb when the action *is* the state change.

`extra_data` carries event-specific freeform JSON. e.g. `job.dead_letter` includes `{error, retry_count}` plus `{dead_lettered_by, reasoning}` when the LLM forced it.

### Structured logs

Every entry carries `request_id` / `trace_id` / `tenant_id` / `user_id` / `job_id` as context vars. Logger names are module-scoped (`app.workers.dispatcher`). Levels: `DEBUG` for chatter, `INFO` for state transitions, `WARNING` for fall-through behavior (LLM fell back to deterministic; rate limit failed open), `ERROR` for things that need investigation.

### What goes in the audit log vs. structured logs vs. metrics

- **Audit log**: who did what, when. Always tied to a user/tenant/resource. Queryable from the admin Audit tab. The historical record.
- **Structured logs**: ephemeral operational signal. What the system is doing. CloudWatch Logs. Hot for ~30 days.
- **Metrics**: numerical aggregates over time. Time-series. CloudWatch Metrics. Drives alarms + dashboards.

A job creation gets all three: audit log row (`job.created`), structured log entry (`INFO: job created`), metric (`JobCreated` counter).

---

## Common pitfalls

- **Adding a new endpoint that lists rows without filtering by `tenant_id`.** RLS will catch it for tenant admins, but platform admins implicitly bypass via `set_config('app.tenant_id', other)`. Always filter explicitly at the application layer; let RLS be the safety net.
- **Forgetting to seed the default tenant in a test fixture.** The `default_tenant` fixture in `conftest.py` is the source of truth; new fixtures that create users must depend on it.
- **Adding a Kafka producer without going through the outbox.** Direct publish (`publish_*`) skips the atomicity guarantee. Only `job.progress` uses the direct path; everything else routes through `outbox_events`.
- **Renaming a Kafka field.** Backward-incompatible. Add a new field, deprecate the old, drop after every consumer reads the new one. See the rules in [`docs/KAFKA.md`](docs/KAFKA.md#schema-evolution-rules).
- **Mutating an event log row.** `job_events` is immutable. The `UNIQUE (kafka_topic, kafka_partition, kafka_offset)` constraint is what makes redelivery idempotent.
- **Calling Anthropic from a request handler synchronously.** All LLM features are async. Wrap with `asyncio.wait_for` if you need a timeout; never block the worker indefinitely.
- **Reading from `jobs:status:*` (pre-Phase-12 keys).** They don't exist anymore. Use the per-tenant keys `jobs:tenant:{tid}:status:{status}`. See [Phase 12 PR D](https://github.com/kudratsingh/incident-platform/pull/38).
- **Catching `AppError` and re-raising as a different type.** The middleware needs the type to know the status code. Re-raise the same instance or let it propagate.
- **Hand-editing a generated Alembic revision after merging.** Future migrations chain off the revision ID; changing it breaks the chain. Make a new revision.
- **Skipping the schema check on a new Kafka topic.** Producers without validation send malformed events; consumers without validation accept them. Every topic in `Settings.kafka_topic_*` must have a matching `.schema.json`.
- **Calling `JobType(job.type)` outside a try/except.** `JobType` is a `StrEnum` with no `_missing_` hook. Saga compensation types (`csv_upload.compensate`) are NOT valid enum members and coercion raises `ValueError`. A historical bug had `_run_job` doing exactly this — see `test_run_job_dead_letters_compensation_when_no_processor`. If you need to coerce a job type string safely, wrap the call and route unknowns to the DEAD_LETTER path.
- **Fire-and-forget `asyncio.create_task` without exception handling.** The dispatcher spawns `_run_job` this way. Its `_run_and_release` wrapper has a `try/except` safety net that logs + force-dead-letters on escape; anything else you spawn similarly needs its own guard, or exceptions vanish silently.
