"""
LLM triage consumer.

Subscribes to `job.dlq` and asks Claude to analyse each dead-lettered job.
The result is persisted to `job_triages` (one row per job_id, unique).

Failure modes:
  - LLM_TRIAGE_ENABLED=false → log + return (no-op). Tests run here.
  - No ANTHROPIC_API_KEY → Anthropic SDK raises at first call; we let the
    base-class `handle_message` raise so the offset isn't committed and the
    message is re-delivered after fixing the credential.
  - Transient Anthropic API errors → same: raise, don't commit.
  - Schema validation failure on the returned payload → also raises out of
    `messages.parse()`; same retry behavior.

Idempotency: handled at the repository layer via UNIQUE (job_id). Redelivery
of the same DLQ event will overwrite the row with the latest analysis, which
is fine.
"""

import uuid
from typing import Any

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from app.repositories.triage import TriageRepository
from app.services import triage as triage_service
from app.workers.kafka_consumer import BaseKafkaConsumer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


class LlmTriageConsumer(BaseKafkaConsumer):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        settings = get_settings()
        super().__init__(
            topics=[settings.kafka_topic_job_dlq],
            group_id=settings.kafka_consumer_group_triage,
        )
        self.session_factory = session_factory

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        **_kafka_meta: Any,
    ) -> None:
        if not triage_service.is_enabled():
            # Disabled by config — skip silently. Tests and local dev without
            # an API key land here.
            return

        if not isinstance(value, dict):
            return
        if value.get("event") != "job.failed" or value.get("dead_lettered") is not True:
            return

        job_id_str = value.get("job_id")
        if not job_id_str:
            return
        try:
            job_id = uuid.UUID(job_id_str)
        except ValueError:
            logger.warning("triage skipping invalid job_id", extra={"job_id": job_id_str})
            return

        try:
            analysis, usage, model_used = await triage_service.triage_failure(
                job_type=str(value.get("job_type", "")),
                payload=value.get("payload"),
                error_message=str(value.get("error", "")),
                retry_count=int(value.get("retry_count", 0)),
                max_retries=int(value.get("max_retries", 0)),
                trace_id=value.get("trace_id"),
            )
        except triage_service.TriageDisabledError:
            return
        except anthropic.APIStatusError as exc:
            # 5xx / 529 — let the consumer commit fail so we re-deliver later.
            logger.warning(
                "triage Anthropic API error — will retry",
                extra={"job_id": str(job_id), "status": exc.status_code},
            )
            raise

        async with self.session_factory() as session:
            async with session.begin():
                await TriageRepository(session).upsert(
                    job_id=job_id,
                    root_cause_category=analysis.root_cause_category,
                    summary=analysis.summary,
                    suggested_fix=analysis.suggested_fix,
                    is_retryable=analysis.is_retryable,
                    confidence=analysis.confidence,
                    model_used=model_used,
                    usage=usage,
                )

        logger.info(
            "triage stored",
            extra={
                "job_id": str(job_id),
                "category": analysis.root_cause_category,
                "model": model_used,
                "cache_read": usage.get("cache_read_input_tokens", 0),
            },
        )
