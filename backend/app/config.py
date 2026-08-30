from functools import lru_cache

from pydantic import RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULT_KEY = "change-me-in-production-please-use-a-long-random-string"

# (field name, insecure literal, refusal message) — table-shaped so the next
# secret-with-a-default (e.g. database_url's postgres:postgres) is a one-line
# addition, not a redesign. Checked by _refuse_insecure_production_secrets.
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

    # SSE streaming (see docs/REDIS.md, "SSE progress bridge").
    #
    # The streaming path runs on its OWN Redis pool so it can never starve the
    # worker loops, rate limiter and backpressure check that share the default
    # 20-connection pool. It is small on purpose: the fan-out broker holds one
    # Pub/Sub connection for the whole process regardless of how many viewers
    # are watching, so this is headroom for reconnects, not a per-viewer
    # budget.
    sse_redis_max_connections: int = 5
    # Per-process cap on concurrent open streams. Beyond it a viewer gets 503
    # + Retry-After instead of silently competing for a finite resource.
    # 0 disables the cap.
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

    # Object storage (MinIO locally)
    # No usable credential defaults ship (finding E2-07). Local MinIO users
    # set these via .env; in production the ECS task IAM role provides S3
    # access. The Phase-14 storage client must treat None as "use ambient
    # IAM credentials" and must not invent a fallback literal.
    storage_endpoint: str = "http://localhost:9000"
    storage_access_key: str | None = None
    storage_secret_key: str | None = None
    storage_bucket: str = "incident-platform"

    # Workers
    max_job_retries: int = 3
    job_retry_backoff_base: float = 2.0
    # How long a job may sit in RUNNING before the crash-recovery sweep
    # (`_stale_running_sweep_loop`) treats it as a worker-crash orphan and
    # dead-letters it (E1-17, ADR 0019). Must comfortably exceed the longest
    # legitimate processor runtime — including chaos-injected latency, whose
    # `inject_latency` hook caps at 60s per poll — because the sweep cannot
    # tell a slow job from an abandoned one. In a multi-replica deployment it
    # is the ONLY protection for a sibling replica's long-running jobs: the
    # in-process in-flight exclusion covers this process only, so a sibling's
    # live job looks orphaned here and survives purely on this threshold.
    stale_running_threshold_seconds: int = 900

    # Hard deadline on a single processor execution (WO-R2-07, ADR 0021).
    # `await processor(...)` had none, so one job could hold a concurrency
    # slot forever and the sweep above deliberately skipped it for being
    # in-flight — the one stuck state nothing in the tree could reclaim.
    #
    # The value has to sit inside a window, and both ends are load-bearing:
    #
    #   lower bound — the longest legitimate processor runtime. Every knob a
    #     payload can turn is bounded (`schemas/job.py`), and the slowest
    #     bounded shape is a csv_upload at `_MAX_CSV_CHUNKS` chunks: ~200s on
    #     the 4-thread pool. Chaos `inject_latency` does NOT count against
    #     this — its 60s cap sleeps the consumer's *poll* loop
    #     (`kafka_consumer.run`), delaying dispatch, not execution. It
    #     stretches the gap before `started_at`, never the span this bounds.
    #   upper bound — `stale_running_threshold_seconds` (900s). This must
    #     fire first and by a wide margin, or the two recovery paths race:
    #     the sweep would dead-letter a job whose processor is still running
    #     and whose own terminal write is still coming.
    #
    # 600s sits between them with room at both ends. Raising it past the
    # stale-RUNNING threshold re-opens the finding, so keep the ordering.
    job_execution_timeout_seconds: float = 600.0

    # Worker liveness, as reported by `GET /api/v1/health` (WO-R2-10). The
    # supervisor refreshes the heartbeat on this interval while the worker
    # task is alive; the health check calls the worker dead once the last
    # heartbeat is older than the staleness bound.
    #
    # The bound has to clear several intervals — one missed tick under load is
    # not a dead worker — while staying inside the probes' own 3 × 30s
    # unhealthy window, so a wedged worker is caught within one ECS cycle
    # rather than two. 15s/60s gives four ticks of slack on both counts. Note
    # the staleness bound is only the *backstop*: a worker task that has
    # actually ended is reported unhealthy immediately, not after 60s.
    worker_heartbeat_interval_seconds: float = 15.0
    worker_heartbeat_stale_seconds: float = 60.0

    # How many failed publish attempts an outbox row gets before the relay
    # dead-letters it (ADR 0001 Decision item 3 / its 2026 Q3 addendum).
    # The relay ticks once a second and retries every unpublished row every
    # tick, so `attempts` is really "seconds of continuous failure" — this
    # default is ~15 minutes. That is deliberately generous: a broker outage
    # fails every row in the batch, and quarantining an entire backlog for a
    # blip would be worse than the head-of-line stall this cap exists to
    # stop. Deterministic failures do not wait for the cap — a
    # SchemaValidationError dead-letters on the first attempt — so this is
    # the backstop for failures we cannot classify, not the primary path.
    # The unpublished-age alarm fires long before this does.
    outbox_max_attempts: int = 900

    # Largest job payload we accept at submission, in bytes. The outbox event
    # wrapping a payload must fit Kafka's default 1 MiB `message.max.bytes`,
    # and a record the broker refuses is refused identically forever — a
    # poison row an ordinary user could otherwise create at will. 256 KiB
    # leaves ample room for the envelope fields around the payload.
    max_job_payload_bytes: int = 256 * 1024

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

    # ---- Per-principal / per-identity rate limits (WO-R2-30) ----
    #
    # All three are FIXED windows, so the ceiling actually enforced is
    # 2x the number across a window boundary (see utils/rate_limit.py).
    # Every value below is sized against that doubled figure.
    #
    # MCP: one bucket per service-account principal. Sized to stop a
    # runaway tool-call loop from saturating the MCP process's DB pool
    # (SQLAlchemy defaults: pool_size=5 + max_overflow=10 = 15
    # connections) without ever touching a legitimate eval run, whose
    # calls are paced by the agent's own LLM turn latency. A stuck
    # retry loop does thousands per minute; this stops that decisively
    # and leaves normal investigation untouched.
    mcp_rate_limit_per_principal: int = 120
    mcp_rate_limit_window_seconds: int = 60

    # The two admin endpoints that each make one paid Anthropic call per
    # request. Bounded by spend, not by load: at ~$0.006 a
    # natural-language query and ~$0.018 a digest, the worst-case
    # boundary burst is ~$0.12 and ~$0.18 respectively. Both are
    # human-driven, so these sit far above any real interactive rate.
    admin_nl_query_rate_limit: int = 10
    admin_digest_rate_limit: int = 5
    admin_paid_rate_limit_window_seconds: int = 60

    # LLM-driven DLQ triage. Disabled by default; enabling requires an
    # Anthropic API key (read from ANTHROPIC_API_KEY env var by the SDK).
    llm_triage_enabled: bool = False
    llm_triage_model: str = "claude-opus-4-7"
    # Hard wall-clock limit on the LLM call, per ADR 0005 ("times out —
    # configurable per feature; defaults to 10s"). This bounds the whole call,
    # SDK-internal retries included: the Anthropic client's own `timeout` is
    # per attempt and is retried `max_retries` times, so a 10s client timeout
    # is really up to 30s of wall clock. Only an outer deadline is the
    # deadline the ADR promises.
    llm_triage_timeout_seconds: float = 10.0

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
    # See `llm_triage_timeout_seconds`. A user is waiting on this one, so the
    # deadline is also the worst case for the request's latency.
    llm_nl_query_timeout_seconds: float = 10.0

    # Periodic incident summaries. The digest worker runs every
    # `llm_digest_interval_hours` and writes one row per active tenant
    # summarising the trailing `llm_digest_window_hours` of failures.
    llm_digest_enabled: bool = False
    llm_digest_model: str = "claude-opus-4-7"
    # See `llm_triage_timeout_seconds`. The digest loop runs tenants serially,
    # so this is also the per-tenant ceiling on how long one slow API call can
    # delay every tenant behind it.
    llm_digest_timeout_seconds: float = 10.0
    llm_digest_interval_hours: int = 24
    llm_digest_window_hours: int = 24
    # Cap the number of error_message rows we fingerprint per tenant; the
    # service deduplicates anyway, but pulling 100k rows is wasteful.
    llm_digest_max_error_samples: int = 1000

    # ------------------------------------------------------------------
    # Live-eval fixtures — set True in the incident-commander agent's
    # compose so `docker compose up` produces a platform stack with
    # realistic seed data on every boot. Runs `seed_eval_fixtures.py`
    # from the app lifespan after migrations, before serving requests.
    # Idempotent (all IDs are `uuid5`-derived) so re-boots are safe.
    #
    # Default False — production doesn't want synthetic DLQ jobs.
    # ------------------------------------------------------------------
    seed_eval_fixtures: bool = False

    # ------------------------------------------------------------------
    # Chaos framework — see ADR 0008.
    #
    # Off by default. Terraform validation refuses `chaos_enabled=true`
    # in the production workspace; `assert_chaos_gate()` below enforces
    # the same invariant at boot even if TF were somehow bypassed.
    # ------------------------------------------------------------------
    chaos_enabled: bool = False

    # ------------------------------------------------------------------
    # Alert emission — outbound signed webhook + poll fallback.
    #
    # `alert_webhook_url` is optional. When unset, alerts still get
    # persisted (visible via `list_active_alerts` MCP tool), just not
    # pushed. The secret signs the body with HMAC-SHA256; consumers
    # verify with the same secret.
    # ------------------------------------------------------------------
    alert_webhook_url: str | None = None
    alert_webhook_secret: str | None = None
    alert_webhook_timeout_seconds: float = 5.0

    # ------------------------------------------------------------------
    # Scheduled SLO evaluation (WO-R2-29). `_slo_evaluation_loop` computes
    # the objectives in `services/slo.py` on this interval and raises an
    # Alert on a fast burn — the alert webhook's only non-chaos producer.
    #
    # 300s is well inside the shortest exhaustion time the fast-burn
    # threshold describes (~100 min for the completion objective), so a
    # burn is noticed with plenty of budget left, while costing two
    # aggregate queries over `jobs` per replica per five minutes.
    #
    # The de-dup window is what a sustained burn costs in alerts: one per
    # hour per objective, rather than one per tick. It is a *bucket* width,
    # not a cooldown — see `slo._fast_burn_dedup_key` for why that
    # distinction is what makes de-duplication safe across replicas.
    #
    # 0 disables evaluation entirely, for deployments that drive alerting
    # from CloudWatch alone and want no second producer.
    # ------------------------------------------------------------------
    slo_evaluation_interval_seconds: float = 300.0
    slo_alert_dedup_window_seconds: float = 3600.0

    # Tracing — set to http://localhost:4318 locally (Jaeger), or X-Ray OTLP endpoint in prod
    otlp_endpoint: str | None = None

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None  # e.g. "logs/app.log" — if set, JSON logs are also written here

    @model_validator(mode="after")
    def _refuse_insecure_production_secrets(self) -> "Settings":
        """Fail closed at boot: no secret-with-a-default may reach production.

        Validates against the *parsed* ``self.environment`` — the effective
        value regardless of config source (init kwarg, process env var, or
        the ``.env`` file). The previous field_validator read
        ``os.getenv("ENVIRONMENT")`` and was bypassed whenever production
        was declared only via the ``.env`` file (finding E2-08).

        Fires at Settings() construction, i.e. exactly at boot in
        production (get_settings is lru_cached; main.py builds settings at
        import time).
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

    Called from the main FastAPI lifespan and the MCP standalone
    entrypoint — belt and braces on top of Terraform's own validation
    (see `infra/variables.tf`). Any misconfiguration here should crash
    the process at import/startup, never at first chaos-tool call.
    """
    if settings is None:
        settings = get_settings()
    if settings.chaos_enabled and settings.environment == "production":
        raise RuntimeError(
            "CHAOS_ENABLED is true but ENVIRONMENT is 'production'. "
            "Chaos tools must never be reachable from prod. See ADR 0008."
        )
