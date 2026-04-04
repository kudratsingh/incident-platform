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

The key learning is deciding **which work belongs where**.

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

### What to Avoid
- Do NOT stuff every advanced Python concept into every file
- Do NOT spam inheritance everywhere
- Mostly clean, boring, readable code with a few well-chosen advanced techniques where they truly help
- That restraint is part of seniority

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

### Phase 5: Hardening
- Rate limiting, load testing, caching, test matrix
- Static checks, performance profiling, chaos/failure scenarios
- **Focus:** senior-level polish

---

## Repo Structure (Suggested)

```
├── CLAUDE.md
├── README.md
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .github/
│   └── workflows/
│       └── ci.yml
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app factory
│   │   ├── config.py            # Settings / env config
│   │   ├── dependencies.py      # Shared FastAPI dependencies
│   │   ├── api/
│   │   │   ├── auth.py
│   │   │   ├── jobs.py
│   │   │   ├── admin.py
│   │   │   ├── audit.py
│   │   │   └── streaming.py     # SSE / WebSocket endpoints
│   │   ├── models/              # SQLAlchemy / DB models
│   │   ├── schemas/             # Pydantic request/response DTOs
│   │   ├── services/            # Business logic layer
│   │   ├── repositories/        # Data access layer
│   │   ├── workers/             # Background job processors
│   │   │   ├── async_tasks.py
│   │   │   ├── thread_adapters.py
│   │   │   └── cpu_processors.py
│   │   ├── core/                # Auth, logging, middleware, exceptions
│   │   └── utils/               # Decorators, mixins, descriptors, helpers
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── api/
│       └── conftest.py
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   ├── components/
│   │   ├── hooks/
│   │   ├── api/                 # API client layer
│   │   └── utils/
│   └── package.json
└── scripts/                     # Dev helpers, seed data, migrations
```

---

## Key Interview Talking Points This Project Enables

- Why async for request handling, threads for one subsystem, processes for another
- How to trace a failed job from browser request → backend logs → worker state
- Idempotency and retry design decisions
- Code structure for readability and maintainability
- Layered test strategy
- Bottleneck identification and what changes at 10x scale
- Observability and incident response workflow
