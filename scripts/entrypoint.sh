#!/bin/sh
# Run database migrations then start the application.
# Alembic uses an advisory lock so concurrent task startups are safe.
set -e

# /app/backend is the Python root — both alembic (import app.models.*) and
# uvicorn (from app.config import ...) resolve imports from here.
export PYTHONPATH=/app/backend

echo "Running database migrations..."
alembic upgrade head
echo "Migrations complete."

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --no-access-log \
  --log-level warning
