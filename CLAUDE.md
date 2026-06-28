# CLAUDE.md — Incident & Workflow Platform

## Project Overview

A production-style **Incident & Workflow Platform** — an internal enterprise operations tool where teams submit jobs (CSV upload, report generation, bulk API sync, document analysis), watch live progress, inspect failures/retries/audit history, and where admins can replay failed jobs and inspect request traces.

This is NOT a generic CRUD app. It intentionally forces: concurrency model decisions, structured logging with trace IDs, retry/idempotency patterns, background job orchestration, event-driven architecture, real debugging workflows, and production deployment concerns.

The project is structured as a sequence of milestone phases (Phase 1 through Phase 13). Each phase ships as one or more pull requests against `master`. The plan is *aspirational on the right-hand side* (Phases 8+ are not yet built) and *historical on the left* (Phases 1–7 are merged and running) — see the per-phase status markers in the milestone plan below.

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
| 10 — AI / LLM Integration | 🟧 In progress | DLQ triage |
| 11 — Real-time Stream Analytics | 🟡 Not started | — |
| 12 — Multi-tenancy | 🟡 Not started | — |
| 13 — Disaster Recovery & Chaos | 🟡 Not started | — |

**Runtime topology that's actually running** (Phase 7 endpoint):

- **One FastAPI app** behind the ALB. `POST /jobs` is rate-limited, backpressure-gated, and writes both the job row and an `outbox_events` row in a single DB transaction.
- **Seven Kafka consumer groups** running concurrently inside the worker process (`worker_loop` in `app/workers/dispatcher.py`):
  1. `worker-dispatcher` — pops `job.submitted`, runs the actual processor
  2. `audit-writer` — appends `event.*` rows to `audit_logs`
  3. `sse-broadcaster` — bridges Kafka events to the Redis pub/sub channel SSE clients read
  4. `event-log` — appends every lifecycle event to the immutable `job_events` table (event sourcing)
  5. `read-model` — maintains Redis-backed denormalized job-status sets (CQRS read side)
  6. `dependency-resolver` — promotes `WAITING` jobs to `PENDING` when their parents complete
  7. `saga-coordinator` — drives saga-level state and compensation on failure
- **One outbox relay loop** inside the same worker process, polling `outbox_events` every second and publishing to Kafka.
- **One delayed-retry promote loop** moving exponentially-backed-off retries from a Redis sorted-set back into Kafka (still through the outbox).
- **One metrics loop** emitting CloudWatch gauges (`QueueDepth`, `InFlightJobs`, `ConsumerLag`) and caching the lag in Redis for the backpressure check.

Per-PR breakdown of Phase 7 specifically (for reference when reading the code):

- **`#28` — foundations**: JSON Schema registry validating every Kafka payload, `BackpressureError` (503) on `POST /jobs` when consumer lag exceeds threshold, Redpanda-via-Testcontainers integration test.
- **`#29` — read side**: `job_events` table + `EventLogConsumer` (`UNIQUE (kafka_topic, kafka_partition, kafka_offset)` for redelivery dedup), `ReadModelProjector` maintaining Redis sets keyed by `job_id` (idempotent under at-least-once), `GET /admin/jobs/{id}/timeline` and `GET /admin/stats`.
- **`#30` — orchestration**: `job_dependencies` table, `JobStatus.WAITING` / `CANCELLED`, `SagaStatus` enum, `POST /sagas` creates a chain of dependent jobs, `SagaCoordinator` cancels downstream and enqueues `{type}.compensate` jobs on dead-letter.
- **`#31` — frontend**: Sagas browse / create / detail pages, Kafka event timeline on `JobDetailPage` (admin only), CQRS stats overview tab, optional dependencies field on the job form, `backpressure` 503 toast.
- **`#32` — Phase 6 gaps**: `GET /admin/slos` with budget-remaining + burn-rate per objective, `runbooks/*.yaml` (7 runbooks for every CloudWatch alarm and SLO), `GET /admin/runbooks/{id}`, SLO scorecards + runbook modal in the admin UI, two new fast-burn alarms in Terraform.

---

## Stack

### Backend
- **Python 3.12+ / FastAPI** — async API gateway
- **PostgreSQL** — system of record. Tables: `users`, `jobs`, `audit_logs`, `outbox_events`, `job_events`, `job_dependencies`, `sagas`, `job_triages` (Phase 10 WIP).
- **Redis** — cache, locks, rate limits, pub/sub progress events, CQRS read-model sets, cached backpressure lag value.
- **Kafka** (Redpanda locally, Amazon MSK in production) — durable event log; decouples job submission from execution, powers event sourcing and fan-out. Topics: `job.submitted` / `job.progress` / `job.completed` / `job.failed` / `job.dlq`.
- **JSON Schema** — every Kafka topic has a schema in `backend/app/schemas/kafka/`; producer and consumer validate on every message.
- **Object storage** — S3 in production, MinIO locally — for uploaded files and artifacts.
- **Worker layer** — asyncio tasks for I/O-heavy work, a threading adapter for blocking SDKs, a multiprocessing pool for CPU-heavy transforms.
- **Anthropic SDK** (Phase 10) — Claude API integration for LLM-driven triage of dead-lettered jobs.

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
- Current test count: **180+** passing.

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
           │   Seven Kafka consumer groups (concurrent):     │
           │     1. worker-dispatcher   → _run_job           │
           │     2. audit-writer        → audit_logs rows    │
           │     3. sse-broadcaster     → Redis pub/sub      │
           │     4. event-log           → job_events rows    │
           │     5. read-model          → Redis sets         │
           │     6. dependency-resolver → promote children   │
           │     7. saga-coordinator    → compensation       │
           │                                                 │
           │   Three supporting loops:                       │
           │     • outbox relay (DB → Kafka)                 │
           │     • delayed-retry promote (Redis → outbox)    │
           │     • metrics loop (gauges + lag cache)         │
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
1. **Unit tests** (`backend/tests/unit/`) — services, processors, validators, repositories, consumers. ~150 tests. No I/O.
2. **API contract tests** (`backend/tests/api/`) — full FastAPI app with dependency overrides; SQLite in-memory DB; mocked Redis. ~30 tests.
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
| **Dead-letter queue** | `job.dlq` topic; `dead_letter` job status; `/admin/dlq/*` endpoints; `LlmTriageConsumer` (Phase 10 WIP) analyses each entry | Admin replay resets `retry_count` |
| **Fan-out / fan-in** | Five Kafka consumer groups subscribed to the lifecycle topics, all processing independently | No coordination needed; each group has its own offset |
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

## LLM Integration (Phase 10 — In Progress)

The project uses the **Anthropic Python SDK** to add LLM-powered features. The first feature is DLQ triage (in progress on `feat/phase10-ai-triage`).

### Conventions

- **Default model**: `claude-opus-4-7` with `thinking: {type: "adaptive"}`. Failure diagnosis is an intelligence-sensitive task and the per-DLQ cost is small.
- **Structured outputs** — every LLM call uses `client.messages.parse()` with a Pydantic schema (see `TriageAnalysis` in `app/services/triage.py`). The model's response is shape-checked before it ever reaches the DB.
- **Prompt caching** — the frozen system prompt (the failure-classification taxonomy + the platform description) is cached. The volatile per-job context goes into the user message, after the cache breakpoint, so the cache hits across triages.
- **Feature flag** — `settings.llm_triage_enabled` defaults to `False`. Without an `ANTHROPIC_API_KEY`, the consumer logs and skips; tests pass without network access.
- **Cost telemetry** — `usage.cache_read_input_tokens` / `cache_creation_input_tokens` / `input_tokens` / `output_tokens` are persisted on each `JobTriage` row so we can see cache hit rates over time.

### Surface

- New Kafka consumer group `llm-triage` subscribed to `job.dlq` (joins the existing seven in `worker_loop`).
- `job_triages` table (Alembic `e7f4c2a91b08`) with `UNIQUE (job_id)` so redelivery is a no-op.
- `GET /admin/jobs/{id}/triage` returns the analysis.
- Admin UI: triage card on `JobDetailPage` showing category, summary, suggested fix, retryability, and confidence; a badge in the DLQ list when a triage row exists.

### Future LLM features (post-Phase 10)

- Natural-language admin queries over the audit + event log (LLM returns a constrained filter spec, not raw SQL).
- LLM-guided retry policy: given the failure, classify "retry now / retry with longer backoff / dead-letter immediately."
- Periodic incident summaries (one paragraph: "what happened, what we did, what we learned") generated from the event log.

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
  - **Consumer groups**: seven, all running concurrently in the worker process. See "Current Implementation Status" above for the full list.
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

### Phase 10: AI / LLM Integration 🟧 (In Progress)
- **LLM-driven DLQ triage** — Anthropic SDK; per dead-lettered job, Claude classifies the root cause, summarises the failure, suggests a fix, and rates retryability + confidence. Persisted to `job_triages`. Surfaced on the DLQ tab and `JobDetailPage`.
  - Schema enforced via `client.messages.parse()` + a Pydantic `TriageAnalysis` model — no raw JSON parsing.
  - Frozen system prompt (~1.5 KB of failure taxonomy + platform description) carries `cache_control: ephemeral` so it caches across triages.
  - `claude-opus-4-7` with `thinking: {type: "adaptive"}`. Per-triage `usage` block stored alongside the analysis for cost telemetry.
  - Feature-flagged off by default (`LLM_TRIAGE_ENABLED=true` to enable). Without an `ANTHROPIC_API_KEY` the consumer logs and skips; tests pass without network.
- **Natural-language admin queries** (future) — admin types "CSV uploads that failed in the last hour with retry_count ≥ 2"; LLM returns a constrained filter object (not raw SQL); we apply via the existing list endpoint. Pre-defined allowed fields + values prevent injection.
- **LLM-guided retry policy** (future) — on failure, Claude sees the error + context and decides "retry / longer backoff / dead-letter now". Falls back to the deterministic policy if the LLM is unavailable.
- **Periodic incident summaries** (future) — one-paragraph daily / weekly digest generated from the event log, posted to the Slack #ops channel.
- **Focus:** structured outputs, prompt caching for cost, graceful degradation when the LLM is offline, observable cost telemetry.

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
│   ├── alembic/                    # DB migrations
│   │   └── versions/
│   │       ├── a01d04e830dc_initial_schema.py
│   │       ├── b2a8f9c7e103_outbox_events.py
│   │       ├── c3e9f1a4d802_job_events.py
│   │       ├── d4b1a8e60305_dag_and_sagas.py
│   │       └── e7f4c2a91b08_job_triages.py
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
│   │   │   ├── user.py
│   │   │   ├── job.py
│   │   │   ├── job_dependency.py   # many-to-many self-join on jobs
│   │   │   ├── audit.py
│   │   │   ├── outbox.py           # outbox_events
│   │   │   ├── event_log.py        # job_events (event sourcing)
│   │   │   ├── saga.py
│   │   │   └── triage.py           # job_triages (Phase 10 WIP)
│   │   │
│   │   ├── schemas/                # Pydantic request/response DTOs
│   │   │   ├── kafka/              # JSON Schema for each Kafka topic
│   │   │   │   ├── job_submitted.schema.json
│   │   │   │   ├── job_progress.schema.json
│   │   │   │   ├── job_completed.schema.json
│   │   │   │   └── job_failed.schema.json
│   │   │   ├── job.py
│   │   │   ├── user.py
│   │   │   └── common.py           # PaginationParams, PaginatedResponse
│   │   │
│   │   ├── repositories/
│   │   │   ├── base.py             # generic BaseRepository[ModelT]
│   │   │   ├── user.py
│   │   │   ├── job.py
│   │   │   ├── job_dependency.py
│   │   │   ├── audit.py
│   │   │   ├── outbox.py
│   │   │   ├── event_log.py
│   │   │   └── saga.py
│   │   │
│   │   ├── services/
│   │   │   ├── auth.py
│   │   │   ├── job.py              # JobService — create_job, replay_job, list_jobs
│   │   │   ├── saga.py             # SagaService — create_saga (chain of jobs)
│   │   │   ├── slo.py              # SLO computation from jobs table
│   │   │   ├── runbooks.py         # YAML loader
│   │   │   └── triage.py           # Phase 10 — Anthropic SDK call with Pydantic schema + caching
│   │   │
│   │   ├── workers/
│   │   │   ├── dispatcher.py       # JobDispatcherConsumer + worker_loop (starts all 7 consumers)
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
│   │   │   ├── read_model.py            # group: read-model
│   │   │   ├── dependency_resolver.py   # group: dependency-resolver
│   │   │   ├── saga_coordinator.py      # group: saga-coordinator
│   │   │   ├── triage_consumer.py       # group: llm-triage (Phase 10 WIP)
│   │   │   │
│   │   │   ├── queue.py            # Redis priority queue (delayed retries; pop_ready_delayed)
│   │   │   └── progress.py         # Redis pub/sub progress events (SSE bridge target)
│   │   │
│   │   ├── core/                   # exceptions, logging, middleware, redis, security, tracing, metrics
│   │   └── utils/                  # rate_limit, cache, decorators, mixins, backpressure, circuit_breaker
│   │
│   └── tests/
│       ├── unit/                   # ~150 tests
│       ├── api/                    # ~30 tests
│       ├── integration/            # Testcontainers (Docker-gated)
│       ├── load/                   # Locust
│       └── conftest.py             # SQLite-in-memory + dependency overrides
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
