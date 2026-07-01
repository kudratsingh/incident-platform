# Incident & Workflow Platform

A production-style enterprise operations platform for submitting, orchestrating, and observing background jobs — CSV uploads, report generation, bulk API syncs, document analysis, and multi-step workflows. Multi-tenant, event-sourced, LLM-augmented, and instrumented end-to-end.

Built as an intentional showcase of senior-level distributed-systems patterns: transactional outbox, CQRS, event sourcing, sagas, DAG-based dependencies, backpressure, circuit breakers, JWT + row-level security, SLOs with fast-burn alarms, and prompt-cached LLM features that fail open.

---

## What's inside

- **HTTP API** in FastAPI serving `/api/v1/*` behind an ALB.
- **Worker process** running 8 concurrent Kafka consumer groups + 4 background loops (outbox relay, delayed-retry promote, metrics, LLM digest).
- **React SPA** admin console with live SSE progress, saga DAG timelines, DLQ triage, natural-language admin search, and incident digest cards.
- **Multi-tenancy** with application-layer filtering + Postgres row-level security as defense-in-depth.
- **LLM features** (Claude via Anthropic SDK): DLQ triage, retry-policy advisor, natural-language admin queries, periodic incident summaries — all off-by-default and fail-open.
- **Full AWS deployment** via Terraform: VPC + ECS Fargate + RDS + ElastiCache + MSK + S3 + ALB + CloudWatch alarms with linked runbooks.

Current test suite: **240+ passing tests** across unit, API contract, and Testcontainers-based integration. `mypy --strict` clean; 70% coverage gate.

---

## Architecture at a glance

```
                            ┌───────────────┐
                            │  React SPA    │
                            └──────┬────────┘
                                   │
                            ┌──────▼──────────────┐
                            │  ALB → FastAPI      │
                            └──┬─┬─┬─┬────────────┘
                               │ │ │ │
                    ┌──────────┘ │ │ └──────────────┐
                    ▼            ▼ ▼                ▼
              ┌─────────┐   ┌────────┐         ┌────────┐
              │Postgres │   │ Redis  │         │ Kafka  │
              │ (RDS)   │   │(Elasti)│         │ (MSK)  │
              └────┬────┘   └───┬────┘         └───┬────┘
                   │            │                  │
                   ▼            ▼                  ▼
              ┌───────────────────────────────────────┐
              │  Worker process (one ECS task)        │
              │                                        │
              │  8 Kafka consumer groups:              │
              │    dispatcher, audit, sse, event-log,  │
              │    read-model, dep-resolver,           │
              │    saga-coord, llm-triage              │
              │                                        │
              │  4 background loops:                   │
              │    outbox relay, promote-delayed,      │
              │    metrics, digest                     │
              └───────────────────────────────────────┘
```

Full architecture with request lifecycles, concurrency model, and failure-mode catalog is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Stack

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI, Uvicorn, Pydantic v2 |
| Database | PostgreSQL 16 with row-level security, SQLAlchemy 2 async, Alembic migrations |
| Cache / Locks / Pub-Sub | Redis 7 (ElastiCache in prod) |
| Event log | Kafka (Redpanda locally, Amazon MSK in prod), JSON Schema validation on both producer + consumer |
| Object storage | S3 (MinIO locally) |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, React Router |
| Auth | JWT with tenant_id claim, refresh tokens, 3 role tiers + platform-admin flag |
| Streaming | Server-Sent Events (Kafka → Redis Pub/Sub → browser) |
| Observability | OpenTelemetry (auto-instrumented FastAPI + SQLAlchemy + Redis) → OTLP → X-Ray; CloudWatch metrics + alarms + linked runbooks |
| LLM | Anthropic Python SDK (Claude Opus 4.7 with adaptive thinking + prompt caching) |
| Infrastructure | Terraform (VPC, ECS Fargate, RDS, ElastiCache, MSK, ALB, ACM, IAM, Secrets Manager) |
| CI/CD | GitHub Actions — ruff + mypy + pytest + tsc; Docker build → ECR → ECS deploy |

---

## Feature highlights by phase

Twelve of thirteen planned phases are shipped:

**Phase 1–3 · Foundations** — clean backend, background execution across asyncio / threads / multiprocessing, live SSE progress, React admin console with request-correlation IDs.

**Phase 4 · Production deployment** — Docker, Terraform, ECS Fargate, RDS Postgres, ElastiCache Redis, MSK Kafka, secrets management, CI/CD.

**Phase 5 · Hardening** — sliding-window rate limiting, cache layer, load testing via Locust, `mypy --strict`, 70% coverage gate.

**Phase 6 · Observability & Reliability** — OpenTelemetry distributed tracing (browser → API → worker → DB → external API), custom CloudWatch metrics + 7 alarms, SLOs with 14.4× fast-burn alarms, 7 machine-readable runbooks linked from alarm descriptions, circuit breaker for external calls.

**Phase 7 · Kafka + advanced patterns** — transactional outbox (`outbox_events` table), CQRS read model in Redis, event sourcing (`job_events` immutable log), saga pattern with compensation, job dependency DAGs (`WAITING` → `PENDING` on parent completion), JSON Schema registry, backpressure via consumer-group lag, Testcontainers integration test.

**Phase 10 · AI / LLM integration** (fully shipped)
- **DLQ triage** — Claude classifies dead-lettered jobs (root cause, summary, suggested fix, retryability, confidence). Persisted to `job_triages`; surfaced in the admin DLQ tab.
- **LLM-guided retry policy** — after the first deterministic retry, Claude decides retry-with-backoff vs dead-letter-now. Falls back to deterministic backoff on any LLM error so the worker never blocks.
- **Natural-language admin queries** — `POST /admin/query` translates plain English into a Pydantic `JobFilterSpec` (enum/literal fields only → injection-safe by construction) and runs it through the existing list endpoint.
- **Periodic incident summaries** — background loop aggregates per-tenant failure stats (with digit-normalized error fingerprints) and asks Claude for a one-paragraph digest plus key concerns + recommended actions.

All LLM features use `messages.parse()` with Pydantic schemas, `claude-opus-4-7` with adaptive thinking, prompt caching via `cache_control: ephemeral`, and are off by default. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md) for the fail-open rationale.

**Phase 12 · Multi-tenancy** (fully shipped)
- `tenants` table, `tenant_id` on every domain table, JWT carries `tenant_id` claim.
- Per-tenant rate limits + monthly job quotas configurable per tenant.
- Composite `{tenant_id}:{user_id}` Kafka partition key preserves per-tenant AND per-user ordering ([ADR 0004](docs/ADR/0004-tenant-id-in-kafka-partition-key.md)).
- **Postgres row-level security** on all 6 tenant-scoped tables as defense-in-depth against forgotten `WHERE tenant_id = ?` clauses ([ADR 0003](docs/ADR/0003-rls-as-defense-in-depth.md)).
- `is_platform_admin` role for cross-tenant operators; `?tenant_id=` query-param override on admin endpoints.
- Self-service tenant creation at `/auth/register`.
- Admin Tenants tab with drill-down page, inline rate/quota editors, create-tenant modal.

**Phases 8, 9, 11, 13** — platform engineering & scale, security hardening, real-time stream analytics, disaster recovery & chaos — planned, sized in [`docs/ROADMAP.md`](docs/ROADMAP.md).

---

## Documentation

`CLAUDE.md` is the high-signal index. Everything else lives in `docs/`:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — runtime topology, five annotated request lifecycles, concurrency model, auth & tenant matrix, failure mode catalog, LLM cost model
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — every table, column, index, FK, and constraint with a one-line *why*
- [`docs/KAFKA.md`](docs/KAFKA.md) — topic catalog, schema-evolution rules, 8-consumer-group ops
- [`docs/REDIS.md`](docs/REDIS.md) — key catalog with TTLs; what degrades when Redis dies
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — ~80 categorized extension ideas
- [`docs/ADR/`](docs/ADR/) — five architecture decision records covering outbox vs. CDC, JSON Schema vs. Protobuf, RLS as defense-in-depth, composite partition keys, LLM fail-open policy
- [`runbooks/`](runbooks/) — machine-readable on-call playbooks for every CloudWatch alarm + SLO

---

## Local development

### Prerequisites

- Docker & Docker Compose
- Python 3.12+
- Node.js 18+

### Start infrastructure

```bash
docker compose up postgres redis redpanda minio -d
```

Redpanda is a Kafka-API-compatible broker on `localhost:9092`. Redpanda Console (`docker compose up redpanda-console -d`) gives you a browser UI at `localhost:8080`.

### Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run migrations
cd backend && alembic upgrade head && cd ..

# API + worker (same process for local dev via the app.main lifespan)
uvicorn backend.app.main:app --reload --port 8000
```

API docs: `http://localhost:8000/api/v1/docs`

### Frontend

```bash
cd frontend
npm install
npm run dev
```

App: `http://localhost:3000`

### Environment variables

Backend reads from environment directly. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | asyncpg connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Redpanda / MSK bootstrap |
| `SECRET_KEY` | — | JWT signing key (required) |
| `ANTHROPIC_API_KEY` | — | Required only if enabling LLM features |
| `LLM_TRIAGE_ENABLED` | `False` | DLQ triage feature flag |
| `LLM_RETRY_POLICY_ENABLED` | `False` | Retry policy feature flag |
| `LLM_NL_QUERY_ENABLED` | `False` | Natural-language query feature flag |
| `LLM_DIGEST_ENABLED` | `False` | Incident digest feature flag |
| `BACKPRESSURE_LAG_THRESHOLD` | `1000` | Reject new jobs when consumer lag exceeds this |
| `ENVIRONMENT` | `development` | `development` / `production` |

### Full stack via Docker Compose

```bash
docker compose up --build
```

---

## Testing

```bash
# All layers
pytest backend/tests/

# Fast unit tests only (no I/O, mocked deps)
pytest backend/tests/unit/

# API contract tests (full FastAPI app with dependency overrides)
pytest backend/tests/api/

# Testcontainers integration tests (Docker-gated)
pytest backend/tests/integration/

# With coverage report
pytest --cov=backend/app --cov-report=html
```

Unit + API tests use SQLite in-memory + mocked Redis so they run without any external services. Integration tests spin up real Postgres (for RLS) or Redpanda (for Kafka round-trips) via Testcontainers and are opt-in.

Load tests live in `backend/tests/load/` (Locust).

---

## Repository layout

```
├── CLAUDE.md                       high-signal architecture index (always loaded)
├── docs/                           deep reference (architecture, data model, ADRs, roadmap)
├── runbooks/                       machine-readable on-call runbooks
├── backend/
│   ├── app/
│   │   ├── api/                    HTTP routers (auth, jobs, admin, sagas, audit, streaming)
│   │   ├── core/                   exceptions, logging, middleware, redis, security, tracing, metrics
│   │   ├── models/                 SQLAlchemy models (tenants, users, jobs, sagas, triages, digests, ...)
│   │   ├── repositories/           data access layer
│   │   ├── schemas/                Pydantic DTOs + Kafka JSON Schema files
│   │   ├── services/               business logic (job, saga, triage, retry_policy, nl_query, digest, ...)
│   │   ├── workers/                Kafka consumers, worker loop, processors (async / thread / process)
│   │   ├── utils/                  rate_limit, quota, cache, backpressure, circuit_breaker
│   │   └── main.py                 app factory + lifespan
│   ├── alembic/versions/           11 migrations (through incident_summaries)
│   └── tests/
│       ├── unit/                   ~180 tests
│       ├── api/                    ~50 tests
│       ├── integration/            Testcontainers (Postgres for RLS, Redpanda for Kafka)
│       └── load/                   Locust
├── frontend/
│   └── src/
│       ├── api/                    typed API client (auth, jobs, sagas, admin)
│       ├── components/             Layout, StatusBadge, ProgressBar, JobForm, Toast, ...
│       ├── hooks/                  useAuth, useJobStream (SSE)
│       └── pages/                  Login, Register, Dashboard, JobDetail, Admin, Sagas, TenantDetail
├── infra/                          Terraform for the full AWS stack
├── .github/workflows/              CI: lint, type, test, build, deploy
├── docker-compose.yml
└── pyproject.toml
```

---

## API overview

All routes are prefixed with `/api/v1`. Full OpenAPI spec at `/api/v1/docs` when the API is running.

Core surface:

| Method | Route | Description |
|---|---|---|
| `POST` | `/auth/register` | Register — optionally creates a new tenant workspace |
| `POST` | `/auth/login` | Login (returns access + refresh + tenant_id claim) |
| `POST` | `/auth/refresh` | Refresh access token |
| `GET` | `/auth/me` | Current user + tenant slug |
| `POST` | `/jobs` | Create job (rate-limited + quota-checked + backpressure-gated) |
| `GET` | `/jobs` | List own jobs (paginated + filtered) |
| `GET` | `/jobs/{id}` | Job detail |
| `GET` | `/jobs/{id}/stream` | SSE live-progress stream |
| `POST` | `/sagas` | Create multi-step workflow with dependencies |
| `GET` | `/sagas/{id}` | Saga detail with step chain |
| `GET` | `/admin/jobs` | Admin: search all jobs (accepts `?tenant_id=` for platform admins) |
| `POST` | `/admin/jobs/{id}/replay` | Admin: replay a failed job |
| `POST` | `/admin/incidents/{id}/resolve` | Admin: mark incident resolved |
| `POST` | `/admin/query` | LLM-powered natural-language job search |
| `GET` | `/admin/jobs/{id}/triage` | LLM triage analysis for a dead-lettered job |
| `GET` | `/admin/jobs/{id}/timeline` | Event-sourced Kafka timeline for a job |
| `GET` | `/admin/stats` | Job counts by status (from Redis read-model) |
| `GET` | `/admin/slos` | SLO state, budget remaining, burn rate |
| `GET` | `/admin/runbooks/{id}` | Machine-readable runbook YAML |
| `GET` | `/admin/dlq/stats` | DLQ counts with per-job-type breakdown |
| `GET` | `/admin/digests` | Recent LLM-written incident summaries |
| `POST` | `/admin/digests/generate` | Generate a digest on demand |
| `GET` | `/admin/tenants` | Platform-admin only: list all tenants |
| `POST` | `/admin/tenants` | Platform-admin only: create a tenant |
| `PATCH` | `/admin/tenants/{id}` | Platform-admin only: update rate limits / quota |
| `GET` | `/audit/logs` | Audit trail |

---

## Code quality

```bash
# Lint (fast)
ruff check backend/

# Type check (strict)
cd backend && mypy -p app

# Frontend types
cd frontend && npx tsc --noEmit
```

CI runs all three on every PR alongside the pytest suite. See `.github/workflows/ci.yml`.

---

## Concurrency model

Three Python concurrency primitives used deliberately per workload:

| Model | Use for | Example processor |
|---|---|---|
| `asyncio` | High-concurrency I/O (external APIs, DB, Redis, Kafka) | `bulk_api_sync` in `async_tasks.py` |
| `threading` | Blocking SDKs / synchronous libraries | `csv_upload` in `thread_adapters.py` |
| `multiprocessing` | CPU-heavy work (PDF, transforms, aggregates) | `doc_analysis` / `report_gen` in `cpu_processors.py` |

The dispatcher's `_PROCESSORS` map routes each `JobType` to the right primitive. Details in [`docs/ARCHITECTURE.md#concurrency-model`](docs/ARCHITECTURE.md#concurrency-model).

---

## Deployment

Terraform provisions the full AWS stack in `infra/`:

- VPC with public/private subnets
- ECS Fargate cluster with a service each for API and worker
- RDS Postgres with automated backups
- ElastiCache Redis
- Amazon MSK for Kafka
- S3 for artifacts
- ALB with target-group health checks
- ACM (planned in Phase 8), IAM roles per service, Secrets Manager for DB password + JWT + Anthropic key
- CloudWatch metrics + 7 alarms (5 baseline + 2 SLO fast-burn) with runbook URLs in their descriptions
- SNS topic for alarm delivery

CI builds Docker images, pushes to ECR, and triggers an ECS service update on merge to `master`.

---

## License

MIT
