# Convenience targets for local dev + agent-facing eval workflows.
# test/lint/typecheck run on the host venv (.venv), same as CI — no stack
# needed. The container targets (up/down/logs/migrate/seed-*/mcp-probe)
# assume `docker compose up -d` has been run.

.PHONY: help up down logs test lint typecheck seed-incident-commander \
        seed-eval-fixtures mcp-probe migrate

help:  ## Print this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up:  ## Bring the whole stack up (rebuild + detach)
	docker compose up --build -d

down:  ## Stop the stack (preserves volumes)
	docker compose down

logs:  ## Tail backend logs (Ctrl-C to exit)
	docker compose logs -f app

test:  ## Run unit + API tests via the host venv (fast, no coverage gate)
	.venv/bin/python -m pytest backend/tests/unit backend/tests/api --no-cov

lint:  ## Run ruff via the host venv (same invocation as CI)
	.venv/bin/ruff check backend/

typecheck:  ## Run mypy strict via the host venv (repo root; mypy_path=backend)
	.venv/bin/mypy -p app

migrate:  ## Apply any pending Alembic migrations (idempotent)
	docker compose exec app alembic -c /app/alembic.ini upgrade head

seed-incident-commander:  ## Create the incident-commander SA + print a fresh token
	docker compose exec app python /app/scripts/seed_incident_commander.py

seed-eval-fixtures:  ## Populate the platform with realistic data for the agent's live eval suite
	docker compose exec app python /app/scripts/seed_eval_fixtures.py

mcp-probe:  ## Smoke-test the MCP surface (requires $$TOKEN set on host)
	./scripts/mcp_probe.sh $${STEP:-initialize}
