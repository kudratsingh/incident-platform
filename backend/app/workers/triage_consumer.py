"""
LLM triage consumer: asks Claude to analyse each `job.dlq` event and writes `job_triages` (UNIQUE
job_id, so a redelivery overwrites). Fails open per ADR 0005, and `LLM_TRIAGE_ENABLED=false` is a
no-op. A 429/5xx from Anthropic raises so the offset is not committed and Kafka redelivers — the
ONE blocking case. Everything else logs a WARNING, writes no row and COMMITS: with no attempt
counter and no DLQ-of-the-DLQ, a deterministic failure would redeliver a billed call ~1/s forever.
"""

import uuid
from typing import Any

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from app.repositories.job import JobRepository
from app.repositories.triage import TriageRepository
from app.services import triage as triage_service
from app.workers.kafka_consumer import BaseKafkaConsumer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


def _attempt_ceiling(value: dict[str, Any]) -> int:
    """The job's run budget off a `job.dlq` event: `max_attempts` since WO-R2-172, falling back to
    the old `max_retries` so an in-flight event is not the `0` behind "retry 3 of 0"."""
    raw = value.get("max_attempts")
    if raw is None:
        raw = value.get("max_retries", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _is_transient(status_code: int) -> bool:
    """Is this status worth redelivering the message for? 429 and 5xx are the upstream saying "not
    right now"; every other non-2xx is a property of the request."""
    return status_code == 429 or status_code >= 500


class LlmTriageConsumer(BaseKafkaConsumer):
    """Consumer group `llm-triage` — asks Claude to explain each dead-lettered
    job and stores the answer."""

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
        """Analyse one dead-letter event and write its triage row, plus the
        coarse remediation category derived from it, in one transaction."""
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

        tenant_id_str = value.get("tenant_id")
        if not isinstance(tenant_id_str, str):
            logger.warning(
                "triage skipping event without tenant_id",
                extra={"job_id": str(job_id)},
            )
            return
        try:
            tenant_id = uuid.UUID(tenant_id_str)
        except ValueError:
            return

        try:
            analysis, usage, model_used = await triage_service.triage_failure(
                job_type=str(value.get("job_type", "")),
                payload=value.get("payload"),
                error_message=str(value.get("error", "")),
                retry_count=int(value.get("retry_count", 0)),
                max_attempts=_attempt_ceiling(value),
                trace_id=value.get("trace_id"),
            )
        except triage_service.TriageDisabledError:
            return
        except anthropic.APIStatusError as exc:
            if _is_transient(exc.status_code):
                # The upstream, not this message: no commit, so Kafka redelivers (ADR 0005's only
                # carve-out).
                logger.warning(
                    "triage Anthropic API error — will retry",
                    extra={"job_id": str(job_id), "status": exc.status_code},
                )
                raise
            # A 4xx is deterministic: a bad model id, an oversized payload, a
            # revoked key. Redelivering re-sends the identical request and
            # re-bills the identical failure, forever.
            logger.warning(
                "triage failed — no triage row written",
                extra={
                    "job_id": str(job_id),
                    "status": exc.status_code,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                },
            )
            return
        except Exception as exc:
            # ADR 0005's fallback: no triage row, and the admin works from the raw error_message.
            # Returning commits the offset, which is the point — this failure is THIS message's.
            logger.warning(
                "triage failed — no triage row written",
                extra={
                    "job_id": str(job_id),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                },
            )
            return

        # The coarse category the DLQ tools filter on (R2-24). None reads as "not replay-safe".
        hint = triage_service.remediation_hint_for(analysis)

        async with self.session_factory() as session:
            async with session.begin():
                await TriageRepository(session).upsert(
                    job_id=job_id,
                    tenant_id=tenant_id,
                    root_cause_category=analysis.root_cause_category,
                    summary=analysis.summary,
                    suggested_fix=analysis.suggested_fix,
                    is_retryable=analysis.is_retryable,
                    confidence=analysis.confidence,
                    model_used=model_used,
                    usage=usage,
                )
                # Same transaction as the triage row deliberately: the analysis and the category
                # derived from it are one fact.
                hint_written = (
                    await JobRepository(session).set_remediation_hint_if_unset(
                        job_id=job_id, tenant_id=tenant_id, hint=hint
                    )
                    if hint is not None
                    else False
                )

        logger.info(
            "triage stored",
            extra={
                "job_id": str(job_id),
                "category": analysis.root_cause_category,
                "remediation_hint": hint,
                # False covers three different things — no hint derived, a
                # category already present, or the job gone — so the log
                # says which of "we had one" and "we wrote it" held.
                "remediation_hint_written": hint_written,
                "model": model_used,
                "cache_read": usage.get("cache_read_input_tokens", 0),
            },
        )
