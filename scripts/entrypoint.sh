#!/bin/sh
# Run database migrations then start the application.
# Migrations are serialized by a session-level Postgres advisory lock taken
# in backend/alembic/env.py (see app/core/migration_lock.py) — concurrent
# task startups are safe.
set -e

# /app/backend is the Python root — both alembic (import app.models.*) and
# uvicorn (from app.config import ...) resolve imports from here.
export PYTHONPATH=/app/backend

echo "Running database migrations..."
alembic upgrade head
echo "Migrations complete."

# Sync the incident_app role's password before the app connects as it.
# Runs on the owner URL (ALEMBIC_DATABASE_URL, falling back to
# DATABASE_URL — same resolution as alembic/env.py) and is a strict
# no-op until INCIDENT_APP_DB_PASSWORD exists, i.e. on phase-1 deploys
# that predate the Terraform secret. See ADR 0015's rollout section.
echo "Syncing incident_app role password..."
python -m app.core.db_bootstrap
echo "DB bootstrap complete."

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log \
  --log-level warning
