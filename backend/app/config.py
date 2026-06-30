from functools import lru_cache

from pydantic import RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULT_KEY = "change-me-in-production-please-use-a-long-random-string"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_name: str = "Incident Platform"
    environment: str = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # CORS — comma-separated list of allowed origins.
    # In production, set CORS_ORIGINS to include the ALB DNS name.
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Database — full DSN as a plain string so asyncpg driver prefix works
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform"
    )

    # Redis
    redis_url: RedisDsn = "redis://localhost:6379/0"  # type: ignore[assignment]

    # JWT
    secret_key: str = _INSECURE_DEFAULT_KEY

    @field_validator("secret_key")
    @classmethod
    def secret_key_must_be_set(cls, v: str, info: object) -> str:
        # Delay the import to avoid circular dependency at module load time
        import os
        if os.getenv("ENVIRONMENT", "development") == "production" and v == _INSECURE_DEFAULT_KEY:
            raise ValueError(
                "SECRET_KEY must be set to a strong random value in production. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Object storage (MinIO locally)
    storage_endpoint: str = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_bucket: str = "incident-platform"

    # Workers
    max_job_retries: int = 3
    job_retry_backoff_base: float = 2.0

    # Kafka / Redpanda
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_job_submitted: str = "job.submitted"
    kafka_topic_job_progress: str = "job.progress"
    kafka_topic_job_completed: str = "job.completed"
    kafka_topic_job_failed: str = "job.failed"
    kafka_topic_job_dlq: str = "job.dlq"
    kafka_consumer_group_worker: str = "worker-dispatcher"
    kafka_consumer_group_audit: str = "audit-writer"
    kafka_consumer_group_sse: str = "sse-broadcaster"
    kafka_consumer_group_event_log: str = "event-log"
    kafka_consumer_group_read_model: str = "read-model"
    kafka_consumer_group_dependency: str = "dependency-resolver"
    kafka_consumer_group_saga: str = "saga-coordinator"
    kafka_consumer_group_triage: str = "llm-triage"
    kafka_max_poll_interval_ms: int = 300_000
    kafka_session_timeout_ms: int = 30_000

    # Backpressure — reject new job submissions when the dispatcher's Kafka
    # consumer group is more than this many messages behind. 0 disables.
    backpressure_lag_threshold: int = 1000

    # LLM-driven DLQ triage. Disabled by default; enabling requires an
    # Anthropic API key (read from ANTHROPIC_API_KEY env var by the SDK).
    llm_triage_enabled: bool = False
    llm_triage_model: str = "claude-opus-4-7"

    # LLM-guided retry policy. When enabled, after the first deterministic
    # retry the worker asks Claude whether to keep retrying (with what
    # backoff) or to dead-letter immediately. Off by default; any error /
    # timeout from the LLM call falls back to the deterministic policy so
    # the worker never blocks waiting for the API.
    llm_retry_policy_enabled: bool = False
    llm_retry_policy_model: str = "claude-opus-4-7"
    # Lower bound on retry_count before we consult Claude. First failure is
    # almost always worth retrying; consulting on attempt 0 wastes tokens.
    llm_retry_policy_min_retry_count: int = 1
    # Hard wall-clock limit on the LLM call. The worker would rather use
    # the deterministic backoff than block on a slow API.
    llm_retry_policy_timeout_seconds: float = 10.0

    # Natural-language admin queries. Translates a plain-English question into
    # a constrained JobFilterSpec the platform applies to /admin/jobs. Off by
    # default. When disabled, the API returns 503.
    llm_nl_query_enabled: bool = False
    llm_nl_query_model: str = "claude-opus-4-7"

    # Periodic incident summaries. The digest worker runs every
    # `llm_digest_interval_hours` and writes one row per active tenant
    # summarising the trailing `llm_digest_window_hours` of failures.
    llm_digest_enabled: bool = False
    llm_digest_model: str = "claude-opus-4-7"
    llm_digest_interval_hours: int = 24
    llm_digest_window_hours: int = 24
    # Cap the number of error_message rows we fingerprint per tenant; the
    # service deduplicates anyway, but pulling 100k rows is wasteful.
    llm_digest_max_error_samples: int = 1000

    # Tracing — set to http://localhost:4318 locally (Jaeger), or X-Ray OTLP endpoint in prod
    otlp_endpoint: str | None = None

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None  # e.g. "logs/app.log" — if set, JSON logs are also written here


@lru_cache
def get_settings() -> Settings:
    return Settings()
