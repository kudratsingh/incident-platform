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

    # Tracing — set to http://localhost:4318 locally (Jaeger), or X-Ray OTLP endpoint in prod
    otlp_endpoint: str | None = None

    # Logging
    log_level: str = "INFO"
    log_file: str | None = None  # e.g. "logs/app.log" — if set, JSON logs are also written here


@lru_cache
def get_settings() -> Settings:
    return Settings()
