# Convenience targets for local dev + agent-facing eval workflows.
# test/lint/typecheck run on the host venv (.venv), same as CI — no stack
# needed. test-integration also runs on the host venv but needs a reachable
# Docker daemon: Testcontainers starts its own Postgres and Redpanda, so it
# does not need the compose stack either. The container targets
# (up/down/logs/migrate/seed-*/mcp-probe) assume `docker compose up -d`.

.PHONY: help up down logs test test-integration lint typecheck lint-imports \
        seed-incident-commander seed-eval-fixtures mcp-probe migrate

# Overridable so the target works from a git worktree, which has no .venv of
# its own: `make test-integration PYTHON=/path/to/main/.venv/bin/python`.
PYTHON ?= .venv/bin/python

# Every pytest invocation goes through this. Borrowing another checkout's
# interpreter also borrows its *editable install*: the .pth file in that venv
# hardcodes the checkout it was created in, so from a worktree `import app`
# reaches the MAIN tree while pytest reads this tree's test files — a green
# run for a change you never tested. PYTHONPATH is scanned ahead of
# site-packages, which puts the tree this Makefile lives in at the front of
# the `app` namespace package's search path.
#
# Three separate agents hit this before it was fixed (R2-117). The guard is
# backend/tests/unit/test_worktree_import_hygiene.py, which fails loudly if a
# target is ever added that skips this prefix.
PYTEST := PYTHONPATH=$(CURDIR)/backend $(PYTHON) -m pytest

help:  ## Print this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  \033[36m%-28s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up:  ## Bring the whole stack up (rebuild + detach)
	docker compose up --build -d

down:  ## Stop the stack (preserves volumes)
	docker compose down

logs:  ## Tail backend logs (Ctrl-C to exit)
	docker compose logs -f app

test:  ## Run unit + API tests via the host venv (fast, no coverage gate)
	$(PYTEST) backend/tests/unit backend/tests/api --no-cov

# Same command the `integration` CI job runs. Needs a reachable Docker
# daemon — Testcontainers brings up Postgres 16 and Redpanda per module —
# but NOT `docker compose up`: these containers are the tests' own and are
# torn down with them. The three RUN_* gates keep the tier opt-in for
# everyone else, so they are exported here rather than defaulted on.
test-integration:  ## Run the Docker-gated integration tier (real Postgres + Redpanda)
	RUN_RLS_TEST=1 RUN_EVAL_RESET_TEST=1 RUN_MIGRATION_LOCK_TEST=1 \
	  $(PYTEST) backend/tests/integration -v --no-cov $(PYTEST_ARGS)

lint:  ## Run ruff via the host venv (same invocation as CI)
	.venv/bin/ruff check backend/

typecheck:  ## Run mypy strict via the host venv (repo root; mypy_path=backend)
	.venv/bin/mypy -p app

# Contracts are in [tool.importlinter] in pyproject.toml. PYTHONPATH for the
# same reason as PYTEST above: from a worktree, the borrowed venv's editable
# install points at the MAIN tree, so without it this greens the wrong checkout.
lint-imports:  ## Check the ADR 0006 import contracts (app.mcp -> app.services, one way)
	PYTHONPATH=$(CURDIR)/backend .venv/bin/lint-imports

migrate:  ## Apply any pending Alembic migrations (idempotent)
	docker compose exec app alembic -c /app/alembic.ini upgrade head

seed-incident-commander:  ## Create the incident-commander SA + print a fresh token
	docker compose exec app python /app/scripts/seed_incident_commander.py

seed-eval-fixtures:  ## Populate the platform with realistic data for the agent's live eval suite
	docker compose exec app python /app/scripts/seed_eval_fixtures.py

mcp-probe:  ## Smoke-test the MCP surface (requires $$TOKEN set on host)
	./scripts/mcp_probe.sh $${STEP:-initialize}
