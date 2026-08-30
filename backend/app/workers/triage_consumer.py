"""
LLM triage consumer.

Subscribes to `job.dlq` and asks Claude to analyse each dead-lettered job.
The result is persisted to `job_triages` (one row per job_id, unique).

Failure modes (ADR 0005 — every LLM feature fails open):
  - LLM_TRIAGE_ENABLED=false → log + return (no-op). Tests run here.
  - 429 / 5xx from Anthropic → raise, so the offset isn't committed and the
    message is redelivered once the upstream recovers. This is the ONE case
    that blocks, because the same request later is likely to succeed.
  - Anything else — a refusal, a max_tokens truncation, a Pydantic
    validation failure, a timeout, a 4xx (bad model id, oversized payload,
    revoked key), a missing ANTHROPIC_API_KEY → log a WARNING, write no
    triage row, and COMMIT the offset. The admin still sees the job in the
    DLQ with its raw `error_message`; that is the documented fallback.

Why the fallback is not "retry until it works": the base consumer seeks back
to the failed offset and refetches on the next poll, with no attempt counter
and no DLQ-of-the-DLQ anywhere in the class. A deterministic failure
therefore redelivers about once a second forever, and every one of those
deliveries is a full, billed model call with adaptive thinking. One poison
message would head-of-line-block its `job.dlq` partition and spend without
bound — which is exactly what ADR 0005 rejects when it rules out
block-and-retry.

Idempotency: handled at the repository layer via UNIQUE (job_id). Redelivery
of the same DLQ event will overwrite the row with the latest analysis, which
is fine.
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


def _is_transient(status_code: int) -> bool:
    """Is this status worth redelivering the message for?

    429 and 5xx are the upstream saying "not right now" — the same request
    later is likely to succeed. Every other non-2xx is a property of the
    request itself, and `APIStatusError` covers those too: the previous code
    re-raised on all of them while its comment claimed "5xx / 529".
    """
    return status_code == 429 or status_code >= 500


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
                max_retries=int(value.get("max_retries", 0)),
                trace_id=value.get("trace_id"),
            )
        except triage_service.TriageDisabledError:
            return
        except anthropic.APIStatusError as exc:
            if _is_transient(exc.status_code):
                # The upstream is briefly unavailable, not this message being
                # bad. Raising means no commit, so Kafka redelivers once the
                # API recovers — the one carve-out from ADR 0005's fail-open
                # rule, and the only one.
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
            # ADR 0005's stated fallback for DLQ triage: write no triage row
            # and let the admin work from the job's raw error_message — the
            # pre-Phase-10 experience. Returning commits the offset, which is
            # the whole point: a refusal, a max_tokens truncation, a Pydantic
            # validation failure or a timeout is a property of THIS message,
            # and re-delivering it just repeats a billed call at ~1/s forever.
            # `CancelledError` is a BaseException and is deliberately not
            # caught here — worker shutdown must still unwind.
            logger.warning(
                "triage failed — no triage row written",
                extra={
                    "job_id": str(job_id),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:400],
                },
            )
            return

        # The coarse category the DLQ tools filter on, derived from the same
        # analysis (R2-24). None when triage cannot support a claim — see
        # `remediation_hint_for`; NULL is what the tools already read as
        # "unknown, not replay-safe".
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
                # Same transaction as the triage row, deliberately: the
                # analysis and the category derived from it are one fact,
                # and a crash between them would leave the DLQ tools
                # filtering on a category with no analysis behind it (or
                # the reverse — an analysis the agent's categorised-replay
                # path cannot see).
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
