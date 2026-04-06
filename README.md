# Incident & Workflow Platform

An internal operations tool for submitting and monitoring long-running background jobs — CSV uploads, report generation, bulk API syncs, and document analysis. Teams can track live progress, inspect failures and retries, replay dead-letter jobs, and audit every action end-to-end.

---

## Stack

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI, Uvicorn |
| Database | PostgreSQL 16, SQLAlchemy 2 (async) |
| Queue / Cache | Redis 7 |
| Object Storage | MinIO (S3-compatible) |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS |
| Auth | JWT (access + refresh tokens) |
| Streaming | Server-Sent Events (SSE) |

---

## Features

- **Job submission** — create jobs with type, payload, priority, and idempotency key
- **Background execution** — async I/O tasks, thread-pool adapters, process-pool CPU workers
- **Live progress** — SSE stream with per-event log, auto-reconnect, progress bar
- **Retry & dead-letter** — configurable max retries with backoff; failed jobs land in dead-letter
- **Admin console** — search by user/status/trace ID, replay failed jobs, resolve incidents
- **Audit log** — every significant action recorded with user, job, request, and trace IDs
- **Structured logging** — JSON logs with `request_id`, `trace_id`, `job_id`, `user_id` on every entry
- **Role-based access** — `user`, `support`, `admin` roles with enforced route guards

---

## Local Development

### Prerequisites

- Docker & Docker Compose
- Python 3.12+
- Node.js 18+

### Start infrastructure

```bash
docker compose up postgres redis minio -d
```

### Backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn backend.app.main:app --reload --port 8000
```

API docs available at `http://localhost:8000/api/v1/docs`

### Frontend

```bash
cd frontend
npm install
npm run dev
```

App available at `http://localhost:3000`

### Environment variables

Copy and edit the example file:

```bash
cp frontend/.env.example frontend/.env
```

Backend reads from environment directly. Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | asyncpg connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `SECRET_KEY` | — | JWT signing key |
| `ENVIRONMENT` | `development` | `development` / `production` |

### Full stack via Docker Compose

```bash
docker compose up --build
```

---

## Testing

```bash
# Run all tests
pytest

# With coverage report
pytest --cov=backend/app --cov-report=html

# Specific layer
pytest backend/tests/unit/
pytest backend/tests/integration/
pytest backend/tests/api/
```

Tests use SQLite in-memory and mocked Redis — no running services required.

---

## Project Structure

```
├── backend/
│   ├── app/
│   │   ├── api/           # Route handlers (auth, jobs, admin, audit, streaming)
│   │   ├── core/          # Auth, logging, middleware, exceptions
│   │   ├── models/        # SQLAlchemy models
│   │   ├── repositories/  # Data access layer
│   │   ├── schemas/       # Pydantic request/response DTOs
│   │   ├── services/      # Business logic
│   │   ├── workers/       # Background job processors and dispatcher
│   │   └── main.py        # App factory
│   └── tests/
│       ├── api/
│       ├── integration/
│       └── unit/
├── frontend/
│   └── src/
│       ├── api/           # API client layer
│       ├── components/    # Shared UI components
│       ├── hooks/         # Custom React hooks
│       ├── pages/         # Route-level page components
│       └── utils/         # Formatting helpers
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

---

## API Overview

All routes are prefixed with `/api/v1`.

| Method | Route | Description |
|---|---|---|
| `POST` | `/auth/register` | Register a new user |
| `POST` | `/auth/login` | Login, receive access + refresh tokens |
| `POST` | `/auth/refresh` | Refresh access token |
| `POST` | `/jobs` | Create a new job |
| `GET` | `/jobs` | List jobs (paginated, filterable) |
| `GET` | `/jobs/{id}` | Get job detail |
| `GET` | `/jobs/{id}/stream` | SSE stream for live job progress |
| `GET` | `/admin/jobs` | Admin: search all jobs |
| `POST` | `/admin/jobs/{id}/replay` | Admin: replay a failed job |
| `POST` | `/admin/incidents/{id}/resolve` | Admin: mark incident resolved |
| `GET` | `/audit` | Audit log (admin only) |

---

## Linting & Type Checking

```bash
ruff check backend/
mypy backend/
```
