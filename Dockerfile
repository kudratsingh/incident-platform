# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
# Install only production deps (no [dev] extras) into an isolated prefix
RUN pip install --no-cache-dir --prefix=/install .

# ── Stage 2: runtime image ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Runtime system deps only (libpq for asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY backend/ ./backend/
COPY alembic.ini ./
COPY backend/alembic/ ./backend/alembic/
COPY scripts/entrypoint.sh ./entrypoint.sh

# Non-root user for security
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chmod +x /app/entrypoint.sh
USER appuser

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
