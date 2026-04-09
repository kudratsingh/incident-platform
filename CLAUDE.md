# CLAUDE.md — Incident & Workflow Platform

## Project Overview

A production-style **Incident & Workflow Platform** — an internal enterprise operations tool where teams submit jobs (CSV upload, report generation, bulk API sync, document analysis), watch live progress, inspect failures/retries/audit history, and where admins can replay failed jobs and inspect request traces.

This is NOT a generic CRUD app. It intentionally forces: concurrency model decisions, structured logging with trace IDs, retry/idempotency patterns, background job orchestration, real debugging workflows, and production deployment concerns.

---

## Stack

### Backend
- **Python 3.12+ / FastAPI** — async API gateway
- **PostgreSQL** — system of record
- **Redis** — cache, locks, queues, rate limits
- **Object storage** (S3 or MinIO locally) — uploaded files and artifacts
- **Worker layer** — asyncio tasks, threading adapters, multiprocessing for CPU-heavy work

### Frontend
- **React** (or Next.js)
- Auth flow, dashboards, upload page, live job progress, admin console

### Infrastructure
- **Docker** / Docker Compose for local dev
- **CI/CD** pipeline with linting, mypy, pytest
- Cloud target: AWS ECS/Fargate or GCP Cloud Run

### Testing
- `pytest` with fixtures, parametrization, factories
- Unit, integration, API contract, worker, WebSocket/SSE, and failure-mode tests
- `mypy` strict mode in CI
- Coverage gates

---

## Architecture

```
Frontend (React/Next)
   |
   v
FastAPI Gateway
   |── Auth / Users / Jobs / Admin / Audit APIs
   |── SSE or WebSocket progress stream
   |
   ├── PostgreSQL (system of record)
   ├── Redis (cache, locks, queues, rate limits)
   ├── Object storage (uploaded files, artifacts)
   ├── Worker layer
   │     ├── asyncio tasks — I/O-heavy workflows
   │     ├── threads — blocking I/O adapters
   │     └── process pool — CPU-heavy transforms
   |
   └── Structured logs / metrics / traces
```

---

## Core Features

### 1. Auth + Session Model
- Login with refresh tokens or secure session cookies
- Roles: `user`, `support`, `admin`
- Audit trail for important actions
- Dependency-based auth guards (FastAPI `Depends`)
- Clean error handling with predictable error shapes

### 2. Job Submission Pipeline
- Validate input → store metadata → queue background work → update progress → write audit logs → return final status or failure
- Async API endpoints for request handling
- Background jobs for long-running work
- Retry logic with configurable backoff
- Idempotency keys on job creation
- Dead-letter / failed-job table

### 3. Live Progress Streaming
- Server-Sent Events (SSE) or WebSockets for job progress
- Live log tail per job
- Reconnection and error edge case handling

### 4. Admin Incident Console
- Search jobs by user, status, trace ID
- Inspect raw request metadata
- Replay failed jobs
- Compare retries
- Mark incidents resolved
- View structured logs per request/job

### 5. Concurrency — Use All Three Models Deliberately

| Model | Use For |
|---|---|
| `asyncio` | API calls to third-party services, high-concurrency I/O, live updates, streaming status |
| `threading` | Blocking SDKs, file upload helpers, log shipping, wrapping legacy blocking functions |
| `multiprocessing` | CPU-heavy CSV parsing, document transformation, PDF/text extraction, data aggregation |

---

## Code Style & Conventions

### General
- Full type hints across all app code — FastAPI derives real value from typing
- `mypy --strict` must pass
- Service/repository layer separation
- Explicit request/response Pydantic models (DTOs)
- Resource-oriented API routes
- Correlation/trace IDs on every request

### Python Patterns to Use Naturally (not artificially)

**Decorators** — auth checks, timing/profiling, retry wrappers, audit logging, feature flags, caching

**Context managers** — DB sessions/transactions, timing blocks, temporary files, distributed lock acquire/release, structured logging scopes

**Dataclasses / Pydantic models** — typed domain models, value objects, job command objects

**Repository/service pattern** — clean separation of data access and business logic

**Custom exception hierarchy** — domain-specific errors with predictable shapes

**Strategy pattern** — pluggable job processors

**Mixins** (limited, one subsystem) — e.g., `AuditMixin`, `RetryMixin`, `TimedMixin`; explicitly reason about MRO

**Descriptors** (one meaningful use) — e.g., validated config field or tracked model attribute that validates/normalizes on assignment

**`**kwargs`** — in configurable base service classes, adapters, logging helpers

---

## Structured Logging

Every log entry should carry:
- `request_id` / `trace_id`
- `job_id`
- `user_id`
- `route`
- `latency`
- `retry_count`

Use Python's logging with structured formatters (JSON). Logs must be queryable by trace ID end-to-end: browser → API → worker → result.

---

## API Design Principles

- Resource-oriented routes
- Explicit request/response schemas (Pydantic)
- Predictable error shapes (consistent error response model)
- Correlation IDs on all responses
- Idempotent job creation (idempotency keys)
- Pagination, filtering, sorting on list endpoints
- Backwards-compatible versioning
- OpenAPI docs auto-generated by FastAPI

---

## Testing Strategy

### Layers
1. **Unit tests** — parsers, services, validators (no I/O)
2. **Integration tests** — DB + Redis + API together
3. **API contract tests** — request/response schema validation
4. **Worker tests** — job execution, retry behavior, failure modes
5. **WebSocket/SSE tests** — live progress streaming
6. **Failure-mode tests** — what happens when Redis is down, DB timeouts, malformed input
7. **Performance smoke tests** — basic latency/throughput sanity checks

### Tooling
- `pytest` fixtures and parametrization
- Factories for test data
- Testcontainers or Docker Compose for dependencies
- Coverage gates in CI

---

## Data Structures & Algorithms (Natural Usage)

| DS/A | Where It Appears |
|---|---|
| Hash maps/sets | Deduplication, membership checks, idempotency keys |
| Queues | Job processing pipeline |
| Priority queues/heaps | Job scheduler priority |
| Sliding window / ring buffer | Rate limiting |
| Sorting | Result ordering, pagination |
| Caching (LRU, TTL) | Redis cache layer |
| Binary search | Time-series pagination helpers |
| Graph thinking | Job dependency/stage workflows |

---

## Performance Tradeoffs to Explore

- Sync vs async endpoints
- Eager vs lazy loading from DB
- Query count vs memory usage (N+1 awareness)
- Batching vs latency
- Caching vs consistency (Redis TTL strategies)
- Process pool overhead vs CPU speedup
- JSON serialization size
- WebSocket vs polling
- Precomputed aggregates vs live queries

---

## Advanced Python Patterns (Senior / Principal Level)

These go beyond Phase 1-5 and should be introduced naturally in Phases 6-9:

**Structured concurrency** — `asyncio.TaskGroup` (Python 3.11+) for fan-out/fan-in; cancel all sibling tasks on first failure; replace bare `gather()` calls

**Protocols + structural subtyping** — replace ABCs with `typing.Protocol` where duck typing is the right model (e.g. storage backends, queue backends); enables easier testing without inheritance

**ParamSpec + Concatenate** — type-safe decorator factories that preserve the wrapped function's full signature (used in retry wrappers, audit decorators)

**`__init_subclass__`** — self-registering plugin pattern for job processors; adding a new processor class auto-registers it without touching the dispatcher

**`tracemalloc` + memory profiling** — instrument long-lived workers to detect leaks; track top allocations per snapshot delta; add to performance test suite

**Slot classes** — `__slots__` on hot-path domain objects (ProgressEvent, QueueMessage) to reduce per-instance memory overhead at scale

**Custom pickling** — `__reduce__` / `__getstate__` / `__setstate__` on objects passed to multiprocessing pool to control serialization explicitly

**`contextlib.AsyncExitStack`** — dynamic composition of async context managers in the worker lifecycle; clean teardown ordering regardless of which resources were acquired

**Generic repositories** — `Repository[ModelT, PKT]` with covariant/contravariant bounds; enforce at type-check time that you can't pass a `JobRepository` where a `UserRepository` is expected

**Descriptor protocol** — validated config fields using `__set_name__`, `__get__`, `__set__`; one meaningful use in the Settings class for fields requiring cross-field validation

---

## System Design Depth

Topics this project should demonstrate end-to-end:

| Concept | Where It Appears |
|---|---|
| At-least-once delivery | Outbox pattern + Redis pub/sub |
| Exactly-once semantics | Idempotency keys + DB unique constraint |
| Backpressure | Queue depth → reject/slow job submission |
| Circuit breaker | External API calls in async_tasks.py |
| Read/write split | CQRS read models for admin queries |
| Consistency vs availability | Redis cache TTL vs DB source of truth |
| Distributed locking | Redis SETNX for job deduplication |
| Saga / compensating txns | Multi-step job workflows |
| Event sourcing | Job state transition log |
| Fan-out / fan-in | asyncio.TaskGroup in bulk_api_sync |
| Connection pool sizing | PgBouncer + SQLAlchemy pool tuning |
| Time-series partitioning | audit_logs partitioned by month |
| DAG scheduling | Job dependency resolution |

---

## Memory & Resource Awareness

- Streaming/chunked processing for large uploads — don't hold entire files in memory
- Ensure large uploaded objects are not accidentally retained by closures or callbacks
- Avoid reference cycles in callback-heavy or closure-heavy worker code
- Monitor memory growth in long-lived workers

---

## Milestone Plan

### Phase 1: Clean Backend Core
- FastAPI app structure, Postgres models, auth, job creation, status endpoints
- Service/repository layers, type hints everywhere, pytest setup
- **Focus:** style, architecture, tests, API design

### Phase 2: Background Execution
- Queue, retries, progress tracking
- Async I/O tasks, one thread-based adapter, one process-based CPU step
- **Focus:** concurrency choices, idempotency, failure handling

### Phase 3: Frontend + Debugging Realism
- Dashboard, job details, live updates, admin incident console
- Request correlation IDs visible in UI
- **Focus:** Network tab debugging, auth bugs, frontend/backend contracts

### Phase 4: Production Deployment
- Docker, cloud deployment, managed Postgres/Redis/storage
- Secrets/config management, structured logging, metrics, alerting, CI/CD
- **Focus:** shipping, runtime debugging, environment parity

### Phase 5: Hardening ✅
- Rate limiting, load testing, caching, test matrix
- Static checks, performance profiling, chaos/failure scenarios
- **Focus:** senior-level polish

### Phase 6: Observability & Reliability
- **OpenTelemetry** distributed tracing — spans across API → worker → DB → Redis, exported to AWS X-Ray or Jaeger
- **Custom metrics** — job throughput, queue depth, p99 latency, worker saturation via CloudWatch or Prometheus
- **SLOs + error budgets** — define availability/latency targets, track burn rate, alert before budget exhausted
- **CloudWatch alarms** — ECS task failure rate, RDS CPU, Redis memory, ALB 5xx rate → SNS → email/PagerDuty
- **Circuit breaker** — wrap external calls (storage, third-party APIs) with a circuit breaker; open on repeated failures, half-open probe, auto-recover
- **Structured runbooks** — machine-readable runbooks attached to alerts; document diagnosis steps for every alarm
- **Focus:** production observability, on-call readiness, failure isolation

### Phase 7: Advanced Architecture Patterns
- **Outbox pattern** — write events to a DB outbox table in the same transaction as job state changes; a separate relay publishes them to Redis/SQS, guaranteeing at-least-once delivery even if the process crashes
- **CQRS** — separate read models (denormalized, Redis-backed) from write models (normalized Postgres); heavy admin queries hit read side without slowing writes
- **Event sourcing** — store job state transitions as an immutable event log rather than mutable rows; replay events to rebuild state; enables full audit trail and time-travel debugging
- **Saga pattern** — multi-step distributed workflows (e.g. upload → validate → transform → notify) with compensating transactions on failure; implemented as choreography (events) or orchestration (central coordinator)
- **Job dependency DAG** — jobs can declare dependencies on other jobs; scheduler resolves the DAG and only runs a job when all dependencies are complete; detect cycles at submission time
- **Backpressure** — workers signal capacity back to the API; reject or slow-down job submission when queue depth exceeds threshold; expose queue depth as a metric
- **Focus:** distributed systems correctness, consistency guarantees, failure recovery

### Phase 8: Platform Engineering & Scale
- **HTTPS + ACM** — add TLS to the ALB with an AWS Certificate Manager cert; redirect HTTP → HTTPS; enforce HSTS
- **Terraform remote state** — S3 bucket + DynamoDB lock table for shared state; enable team collaboration on infra
- **Staging environment** — second Terraform workspace (`staging`) with smaller instance sizes; CI deploys to staging on PR merge, production on manual approval
- **Blue/green deployments** — ECS CodeDeploy integration; shift traffic from blue to green with automatic rollback on health check failure
- **ECS auto-scaling** — scale backend tasks on queue depth (custom CloudWatch metric) and CPU; scale-in protection during active job processing
- **PgBouncer** — connection pooling sidecar in ECS task; tune pool size vs DB max_connections; measure connection wait time
- **Read replicas** — RDS read replica for analytics/admin queries; route read-heavy endpoints to replica via separate DB session
- **Database partitioning** — partition audit_logs table by month (range partitioning); measure query speedup on time-bounded queries
- **Feature flags** — lightweight Redis-backed feature flag system; enable new job types per-user or per-role without deploys
- **Focus:** zero-downtime deployments, horizontal scale, cost optimization

### Phase 9: Security Hardening
- **WAF** — AWS WAF in front of ALB; rate limiting at network layer, SQL injection / XSS rules, geo-blocking
- **Secret rotation** — automatic rotation of DB password and JWT secret in Secrets Manager; app picks up new secrets without restart
- **VPC flow logs + CloudTrail** — log all network traffic and API calls; ship to S3 + Athena for forensic queries
- **Dependency scanning** — `pip-audit` and `npm audit` in CI; fail on high-severity CVEs; auto-PR for patch updates via Dependabot
- **OWASP hardening** — security headers (CSP, X-Frame-Options, HSTS) in Nginx; validate all user input at system boundaries; SQL injection impossible via parameterized queries (verify)
- **mTLS between services** — mutual TLS for backend → RDS and backend → Redis using ACM Private CA; eliminates credential-based auth for internal traffic
- **Least-privilege IAM** — audit and tighten ECS task role to exact S3 paths and exact Secrets Manager ARNs; no wildcard permissions
- **Focus:** defence in depth, compliance readiness, zero-trust networking

---

## Repo Structure

```
├── CLAUDE.md
├── README.md
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── pyproject.toml
├── .github/
│   └── workflows/
│       └── ci.yml               # lint, test, frontend, deploy jobs
├── backend/
│   ├── alembic/                 # DB migrations
│   │   └── versions/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory + health endpoint
│   │   ├── config.py            # Settings / env config (pydantic-settings)
│   │   ├── dependencies.py      # Shared FastAPI dependencies
│   │   ├── api/
│   │   │   ├── auth.py
│   │   │   ├── jobs.py
│   │   │   ├── admin.py
│   │   │   ├── audit.py
│   │   │   └── streaming.py     # SSE progress stream
│   │   ├── models/              # SQLAlchemy models
│   │   ├── schemas/             # Pydantic request/response DTOs
│   │   ├── services/            # Business logic layer
│   │   ├── repositories/        # Data access layer
│   │   ├── workers/
│   │   │   ├── async_tasks.py   # asyncio — bulk API sync
│   │   │   ├── thread_adapters.py  # threading — CSV upload
│   │   │   ├── cpu_processors.py   # multiprocessing — doc analysis, report gen
│   │   │   ├── dispatcher.py    # main worker loop
│   │   │   ├── queue.py         # Redis priority queue
│   │   │   └── progress.py      # Redis pub/sub progress events
│   │   ├── core/                # exceptions, logging, middleware, redis, security
│   │   └── utils/               # rate_limit, cache, decorators, mixins
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── api/
│       ├── load/                # Locust load tests
│       └── conftest.py
├── frontend/
│   ├── Dockerfile               # Node build → Nginx
│   ├── nginx.conf               # SPA serving, no proxy (ALB handles /api/)
│   ├── src/
│   │   ├── pages/               # Login, Register, Dashboard, JobDetail, Admin
│   │   ├── components/          # StatusBadge, ProgressBar, Toast, TraceId, etc.
│   │   ├── hooks/               # useAuth, useJobStream (SSE)
│   │   ├── api/                 # client.ts, auth.ts, jobs.ts, admin.ts
│   │   └── utils/               # tokens, format
│   └── package.json
├── infra/                       # Terraform — full AWS stack
│   ├── main.tf                  # provider, backend config
│   ├── variables.tf
│   ├── outputs.tf
│   ├── ecr.tf                   # ECR repos + lifecycle policies
│   ├── networking.tf            # VPC, subnets, IGW, security groups
│   ├── iam.tf                   # ECS execution + task roles
│   ├── secrets.tf               # Secrets Manager
│   ├── s3.tf                    # Object storage bucket
│   ├── rds.tf                   # RDS Postgres
│   ├── elasticache.tf           # ElastiCache Redis
│   ├── alb.tf                   # ALB, target groups, listener rules
│   └── ecs.tf                   # Cluster, task definitions, Fargate services
└── scripts/                     # seed data, migrations, ops helpers
    └── entrypoint.sh            # runs alembic upgrade head then uvicorn
```

