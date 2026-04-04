from functools import lru_cache

from pydantic import RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Database — full DSN as a plain string so asyncpg driver prefix works
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/incident_platform"
    )

    # Redis
    redis_url: RedisDsn = "redis://localhost:6379/0"  # type: ignore[assignment]

    # JWT
    secret_key: str = "change-me-in-production-please-use-a-long-random-string"
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

    # Logging
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
