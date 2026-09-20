import logging
from functools import lru_cache

from pydantic import RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

_INSECURE_DEFAULT_KEY = "change-me-in-production-please-use-a-long-random-string"

# (field, insecure literal, message) per secret-with-a-default; read by
# _refuse_insecure_production_secrets.
_INSECURE_PRODUCTION_SECRETS: tuple[tuple[str, str, str], ...] = (
    (
        "secret_key",
        _INSECURE_DEFAULT_KEY,
        "SECRET_KEY must be set to a strong random value in production. "
        "Generate one with: "
        'python -c "import secrets; print(secrets.token_hex(32))"',
    ),
    (
        "storage_access_key",
        "minioadmin",
        "STORAGE_ACCESS_KEY is the weak default MinIO credential and must "
        "never reach production. Production S3 access uses the ECS task IAM "
        "role (see infra/iam.tf); infra injects only STORAGE_BUCKET "
        "(infra/ecs.tf:50).",
    ),
    (
        "storage_secret_key",
        "minioadmin",
        "STORAGE_SECRET_KEY is the weak default MinIO credential and must "
        "never reach production. Production S3 access uses the ECS task IAM "
        "role (see infra/iam.tf); infra injects only STORAGE_BUCKET "
        "(infra/ecs.tf:50).",
    ),
)


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

    # SSE streaming (docs/REDIS.md). Its own Redis pool so viewers cannot
    # starve the worker loops, rate limiter and backpressure check on the
    # default pool; one Pub/Sub connection serves the whole process.
    sse_redis_max_connections: int = 5
    # Per-process cap on open streams; beyond it, 503 + Retry-After. 0 disables.
    sse_max_concurrent_streams: int = 200
    # A stream with no event for this long is closed; the browser's
    # EventSource reconnects if the user is still watching. 0 disables.
    sse_stream_idle_timeout_seconds: int = 300
    # Hard ceiling on one stream's life, resettable by nothing. Bounds the
    # slot a chatty-but-endless job could otherwise hold forever. 0 disables.
    sse_stream_max_duration_seconds: int = 3600
    # Retry-After (seconds) advertised on a capacity refusal.
    sse_retry_after_seconds: int = 5

    # JWT
    secret_key: str = _INSECURE_DEFAULT_KEY
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Object storage (MinIO locally). No credential defaults ship (E2-07) —
    # None means "use ambient IAM credentials", never a fallback literal.
    storage_endpoint: str = "http://localhost:9000"
    storage_access_key: str | None = None
    storage_secret_key: str | None = None
    storage_bucket: str = "incident-platform"

    # Workers
    #
    # Total RUNS a job gets, original plus retries: the dispatcher retries
    # while `retry_count < max_attempts`, so 3 means three runs, two retries.
    # Renamed from `MAX_JOB_RETRIES` for what it counts (WO-R2-172).
    max_job_attempts: int = 3
    # Deprecated alias for `max_job_attempts`, kept one release so
    # `MAX_JOB_RETRIES` deployments keep their ceiling. Nothing reads it:
    # `_resolve_deprecated_max_job_retries` folds it in. `None` means unset.
    max_job_retries: int | None = None
    job_retry_backoff_base: float = 2.0
    # How long a job may sit in RUNNING before `_stale_running_sweep_loop`
    # dead-letters it as a crash orphan (E1-17, ADR 0019). Must exceed the
    # longest legitimate processor runtime — the sweep cannot tell a slow job
    # from an abandoned one, and it is a sibling replica's only protection.
    stale_running_threshold_seconds: int = 900

    # Hard deadline on one processor execution (WO-R2-07, ADR 0021). Must sit
    # above the longest bounded runtime (~200s for a max-chunk csv_upload) and
    # well below `stale_running_threshold_seconds`, or the two recovery paths
    # race.
    job_execution_timeout_seconds: float = 600.0

    # Worker liveness, as reported by `GET /api/v1/health` (WO-R2-10): the
    # worker is called dead once its last heartbeat is older than the stale
    # bound. 15s/60s absorbs missed ticks and still fits the probes' 3 × 30s
    # window; a worker task that has ended is unhealthy immediately.
    worker_heartbeat_interval_seconds: float = 15.0
    worker_heartbeat_stale_seconds: float = 60.0

    # Failed publish attempts an outbox row gets before the relay dead-letters
    # it (ADR 0001 Decision item 3 / its 2026 Q3 addendum). One attempt per
    # second, so this is ~15 minutes: generous, because a broker outage fails
    # the whole batch. Deterministic failures never reach the cap.
    outbox_max_attempts: int = 900

    # Largest payload accepted at submission: the outbox event wrapping it must
    # fit Kafka's 1 MiB `message.max.bytes` or it is a poison row forever.
    max_job_payload_bytes: int = 256 * 1024

    # Kafka / Redpanda
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_job_submitted: str = "job.submitted"
    kafka_topic_job_progress: str = "job.progress"
    kafka_topic_job_completed: str = "job.completed"
    kafka_topic_job_failed: str = "job.failed"
    kafka_topic_job_cancelled: str = "job.cancelled"
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

    # ---- Per-principal / per-identity rate limits (WO-R2-30) ----
    #
    # FIXED windows, so 2x the number is reachable across a boundary (see
    # utils/rate_limit.py); every value below is sized for that.
    #
    # MCP: one bucket per service-account principal, sized to stop a runaway
    # tool-call loop from saturating the MCP DB pool (15 connections).
    mcp_rate_limit_per_principal: int = 120
    mcp_rate_limit_window_seconds: int = 60

    # The two admin endpoints that make one paid Anthropic call per request.
    # Bounded by spend, not load (~$0.006 a query, ~$0.018 a digest).
    admin_nl_query_rate_limit: int = 10
    admin_digest_rate_limit: int = 5
    admin_paid_rate_limit_window_seconds: int = 60

    # LLM-driven DLQ triage. Disabled by default; enabling requires an
    # Anthropic API key (read from ANTHROPIC_API_KEY env var by the SDK).
    llm_triage_enabled: bool = False
    llm_triage_model: str = "claude-opus-4-7"
    # Hard wall-clock limit on the LLM call, per ADR 0005 (defaults to 10s).
    # Bounds the whole call, SDK-internal retries included — the client's own
    # `timeout` is per attempt, so 10s there is really up to 30s.
    llm_triage_timeout_seconds: float = 10.0

    # After the first deterministic retry, ask Claude whether to keep retrying
    # or dead-letter. Off by default; any error falls back to deterministic.
    llm_retry_policy_enabled: bool = False
    llm_retry_policy_model: str = "claude-opus-4-7"
    # Lower bound on retry_count before we consult Claude. First failure is
    # almost always worth retrying; consulting on attempt 0 wastes tokens.
    llm_retry_policy_min_retry_count: int = 1
    # Hard wall-clock limit on the LLM call. The worker would rather use
    # the deterministic backoff than block on a slow API.
    llm_retry_policy_timeout_seconds: float = 10.0

    # Plain-English admin queries → a constrained JobFilterSpec. Off by
    # default; disabled returns 503.
    llm_nl_query_enabled: bool = False
    llm_nl_query_model: str = "claude-opus-4-7"
    # See `llm_triage_timeout_seconds`. A user is waiting on this one, so the
    # deadline is also the worst case for the request's latency.
    llm_nl_query_timeout_seconds: float = 10.0

    # One row per active tenant every `llm_digest_interval_hours`, covering
    # `llm_digest_window_hours`.
    llm_digest_enabled: bool = False
    llm_digest_model: str = "claude-opus-4-7"
    # See `llm_triage_timeout_seconds`. Tenants run serially, so it is also
    # the per-tenant ceiling.
    llm_digest_timeout_seconds: float = 10.0
    llm_digest_interval_hours: int = 24
    llm_digest_window_hours: int = 24
    # Cap the number of error_message rows we fingerprint per tenant; the
    # service deduplicates anyway, but pulling 100k rows is wasteful.
    llm_digest_max_error_samples: int = 1000

    # Live-eval fixtures: runs `seed_eval_fixtures.py` from the lifespan after
    # migrations. Idempotent (uuid5 ids). Default False — production does not
    # want synthetic DLQ jobs.
    seed_eval_fixtures: bool = False

    # Chaos framework (ADR 0008). Off by default; Terraform refuses
    # `chaos_enabled=true` in production and `assert_chaos_gate()` enforces the
    # same at boot.
    chaos_enabled: bool = False

    # The read-only account whose credential may label a call `lab.probe` beside a
    # principal holding `chaos:invoke` (WO-R3-333, ADR 0038). A NAME rather than a
    # scope because this account's scopes are the agent's scopes exactly — read-only
    # is the point of it — so nothing else tells the two apart; `app/mcp/lab_probe.py`
    # re-checks that it holds no write scope before honouring the name. Inert while
    # `chaos_enabled` is false, which is every production deployment.
    lab_probe_smoke_account_name: str = "incident-commander-smoke"

    # Alert emission — signed webhook + poll fallback. With no
    # `alert_webhook_url` alerts are still persisted (readable via
    # `list_active_alerts`), just not pushed. HMAC-SHA256 over the body.
    alert_webhook_url: str | None = None
    alert_webhook_secret: str | None = None
    alert_webhook_timeout_seconds: float = 5.0

    # Scheduled SLO evaluation (WO-R2-29): `_slo_evaluation_loop` raises an
    # Alert on a fast burn, the webhook's only non-chaos producer. The de-dup
    # window is a bucket width, not a cooldown (`slo._fast_burn_dedup_key`),
    # so two replicas cannot both alert. 0 disables evaluation.
    slo_evaluation_interval_seconds: float = 300.0
    slo_alert_dedup_window_seconds: float = 3600.0

    # Tracing — set to http://localhost:4318 locally (Jaeger), or X-Ray OTLP endpoint in prod
    otlp_endpoint: str | None = None

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None  # e.g. "logs/app.log" — if set, JSON logs are also written here

    @model_validator(mode="after")
    def _resolve_deprecated_max_job_retries(self) -> "Settings":
        """Fold `MAX_JOB_RETRIES` into `max_job_attempts` for one release
        (WO-R2-172). The old name alone is used and warns; both set and
        disagreeing refuses to boot, because neither value is defensible."""
        if self.max_job_retries is None:
            return self
        attempts_was_set = "max_job_attempts" in self.model_fields_set
        if attempts_was_set and self.max_job_attempts != self.max_job_retries:
            raise ValueError(
                "MAX_JOB_ATTEMPTS and MAX_JOB_RETRIES are both set and "
                f"disagree ({self.max_job_attempts} vs {self.max_job_retries}). "
                "MAX_JOB_RETRIES is the deprecated name for the same knob — "
                "it caps total runs (original + retries), not retries. Set "
                "MAX_JOB_ATTEMPTS only and remove MAX_JOB_RETRIES."
            )
        if not attempts_was_set:
            self.max_job_attempts = self.max_job_retries
        logger.warning(
            "MAX_JOB_RETRIES is deprecated and will be removed after the "
            "next release — use MAX_JOB_ATTEMPTS. It caps total runs "
            "(the original plus its retries), so %d means %d runs and "
            "%d retries; the name was the only thing wrong with it.",
            self.max_job_attempts,
            self.max_job_attempts,
            max(self.max_job_attempts - 1, 0),
        )
        return self

    @model_validator(mode="after")
    def _refuse_insecure_production_secrets(self) -> "Settings":
        """Fail closed at boot: no secret-with-a-default may reach production.

        Validates the parsed ``self.environment``, so a production declared
        only in the ``.env`` file is caught too (finding E2-08).
        """
        if self.environment == "production":
            for field, insecure_literal, message in _INSECURE_PRODUCTION_SECRETS:
                if getattr(self, field) == insecure_literal:
                    raise ValueError(message)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_chaos_gate(settings: Settings | None = None) -> None:
    """Refuse to boot when chaos is enabled in a production-labelled env.

    Belt and braces over Terraform's own validation (`infra/variables.tf`).
    """
    if settings is None:
        settings = get_settings()
    if settings.chaos_enabled and settings.environment == "production":
        raise RuntimeError(
            "CHAOS_ENABLED is true but ENVIRONMENT is 'production'. "
            "Chaos tools must never be reachable from prod. See ADR 0008."
        )
