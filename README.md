# Incident Platform — Event-Driven Workflow & Incident Operations

An **event-driven, multi-tenant workflow and incident platform** built around **Kafka event sourcing, CQRS, a transactional outbox, compensating sagas and dependency DAGs**. FastAPI services, PostgreSQL row-level security, Redis read models and a React operations console connect asynchronous job orchestration with live progress, failure recovery and audited incident control.

**OpenTelemetry tracing, SLO evaluation, circuit breakers, dead-letter triage and scoped MCP remediation tools** make the platform a controlled environment for [autonomous AI SRE agents](https://github.com/kudratsingh/incident-commander). Machine-principal scopes, idempotency keys, fault-injection hooks and independent audit records expose both distributed-system behavior and the consequences of agent actions.

## What it does

- **Submit and track jobs:** asynchronous, thread and process execution, retries, delayed replay, dead-letter handling and SSE progress.
- **Coordinate workflows:** dependency DAGs, waiting/resume transitions and saga compensation.
- **Preserve the event chain:** Postgres transactions write an outbox; Kafka consumers build event history and read models.
- **Isolate tenants:** JWT identity, application filtering, forced PostgreSQL row-level security, quotas and tenant-scoped rate limits.
- **Operate failures:** consumer and background-loop health, traces, outbox status, DLQ triage, circuit breakers, incidents, SLOs and linked runbooks.
- **Expose agent tools:** read diagnostics, bounded Tier-1 actions and write-only run telemetry, with service-account scopes, idempotency and audit.
- **Assist operators with optional LLM features:** DLQ classification, retry advice, natural-language filters and incident digests. They are off by default and degrade to the deterministic path on provider failure.

**The orchestration infrastructure is implemented; job payload processing remains simulated.** CSV/report/document/sync processors use bounded example work and scripted failure paths. That makes failure-injection experiments reproducible; it is not a claim of completed business integrations.

## Architecture

```text
React console → FastAPI → Postgres job state + transactional outbox
                         → outbox relay → Kafka
                           → dispatch / audit / event log / read model
                           → dependencies / sagas / SSE / optional triage
                         → Redis cache, locks and browser progress

External agent → standalone MCP process → scoped service layer
                                      → idempotent action + audit
```

API, worker behavior and MCP handlers share domain services, while the MCP server runs as a separate process. Local Compose supplies Postgres, Redis, Redpanda, MinIO, Jaeger and the frontend. Terraform describes the AWS topology; a production Kafka broker is not provisioned. [Full request lifecycles and failure modes](docs/ARCHITECTURE.md).

## The agent boundary

Tools cover consumer/control-loop health, database/cache health, traces, deploys, DAGs, outbox delivery, SLOs, alerts, incidents and audit events. Tier-1 operations include consumer restart, targeted DLQ replay, DAG pause, cache invalidation and permanent-DLQ classification.

Every call is checked against a machine principal's scopes. Mutations use idempotency keys, and audit-write failure prevents the action from committing. Agent run reporting is separate from planner-selected tools; an agent cannot read its own report back as investigation evidence.

Chaos tools are a separate, environment-gated lab surface. They register in the local development Compose stack, are rejected in production mode, and require a distinct `chaos:invoke` principal. The agent's normal principal cannot invoke them or read the chaos audit stream. See [scope design](docs/ADR/0007-machine-principal-scope-model.md) and [chaos gating](docs/ADR/0008-chaos-gating.md).

## Run locally

Docker Engine and Compose v2:

```bash
docker compose up --build
```

- [Console](http://localhost:3000)
- [API documentation](http://localhost:8000/api/v1/docs)
- [Jaeger traces](http://localhost:16686)

The Compose stack runs its dedicated migration/bootstrap service before the API and MCP processes start. Use a fresh local account through the console. Provider keys are only needed when deliberately enabling the LLM features.

For the agent lab, `make seed-incident-commander` creates scoped service accounts and prints local token configuration; `make seed-eval-fixtures` seeds the diagnostic world. The agent repository's [pinned demo](https://github.com/kudratsingh/incident-commander/tree/main/demo) is the entry point for reproducing its experiments.

### Development checks

Python **3.12+**, [uv](https://docs.astral.sh/uv/) and Node for frontend development:

```bash
uv sync --extra dev
make lint
make typecheck
make lint-imports
make test
```

Unit/API tests use isolated test dependencies. `make test-integration` additionally needs Docker and starts its own Postgres/Redpanda containers. Frontend checks live in [frontend/package.json](frontend/package.json). For host-based development, run migrations from the repository root with `make migrate-local`; the runtime database role deliberately lacks migration privileges.

## Current scope

Implemented phases cover the core platform, deployment artifacts, hardening, observability, Kafka patterns, optional LLM features and multi-tenancy. Platform-scale work, further security/stream analytics/disaster-recovery milestones and real job processors remain planned. Deployment artifacts are not evidence that a public service is running.

Tests cover unit behavior, API contracts and Docker-backed integrations; CI also checks frontend, infrastructure and workflow configuration. Test counts are intentionally left to the current test run.

## Explore the implementation

| Area | Entry point |
|---|---|
| API and domain services | [backend/app](backend/app/) |
| Event processing and control loops | [workers](backend/app/workers/) |
| MCP tools and authorization | [mcp](backend/app/mcp/) |
| Operator console | [frontend](frontend/) |
| Data and event design | [Data model](docs/DATA_MODEL.md), [Kafka](docs/KAFKA.md), [Redis](docs/REDIS.md) |
| Operations | [Runbooks](runbooks/), [architecture](docs/ARCHITECTURE.md) |
| Decisions, limits and next phases | [ADR index](docs/ADR/README.md), [roadmap](docs/ROADMAP.md), [CLAUDE.md](CLAUDE.md) |
