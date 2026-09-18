"""
Periodic incident summaries.

Collects per-tenant failure stats for a window and asks Claude for a one-paragraph
narrative plus `key_concerns` and `recommended_actions`; persisted to `incident_summaries`
for the admin Digests tab. An LLM rather than a template because the value is the pattern
across hundreds of events, not the count. Same shape as the other LLM services here.
"""

import asyncio
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import anthropic
from app.config import get_settings
from app.core.logging import get_logger
from app.models.digest import IncidentDigest as DigestRow
from app.models.tenant import Tenant
from app.repositories.digest import DigestRepository
from app.services._llm_usage import extract_usage
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)


class IncidentDigest(BaseModel):
    """Pydantic shape Claude fills in."""

    summary: str = Field(
        description=(
            "One short paragraph (2-4 sentences) describing what happened in "
            "the window, what (if anything) we did about it, and what we "
            "should learn from it. Plain prose — admins read this in a card."
        ),
        max_length=1500,
    )
    key_concerns: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 5 short bullet points — failure patterns that recurred or "
            "stand out. Empty list if the window was clean."
        ),
        max_length=5,
    )
    recommended_actions: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 5 short, concrete next steps for the on-call admin. Empty "
            "list if no action is needed."
        ),
        max_length=5,
    )


class DigestDisabledError(RuntimeError):
    """Raised when digests are requested but the feature flag is off."""


_SYSTEM_PROMPT = """\
You write short ops digests for an internal job platform.

You're given aggregate failure stats for one tenant over one time window:
counts of jobs that completed / failed / dead-lettered, broken down by
job type, plus the top recurring error messages. You produce an
IncidentDigest.

Guidance:

  * The summary paragraph is for an on-call admin reading the digest
    over coffee. Tell them what mattered, not what was numerically
    largest. "5 dead-letters all from the same upstream timeout" is more
    useful than "we dead-lettered 5 jobs".

  * key_concerns surface PATTERNS, not single events. If the same error
    text appears 4+ times, that's a concern. If one bulk_api_sync job
    failed once with a unique 502, that's not.

  * recommended_actions must be concrete and actionable. "Check the
    upstream API for the bulk_api_sync route" beats "investigate
    failures." If you have no concrete suggestion, leave the list empty.

  * If the window had zero failures, say so plainly and skip the
    concerns/actions lists.

  * Don't editorialize about whether the platform "worked" — admins
    don't need reassurance, they need a fast read.
"""


def is_enabled() -> bool:
    return bool(get_settings().llm_digest_enabled)


def _top_errors(error_messages: list[str], n: int = 5) -> list[dict[str, Any]]:
    """Bucket error messages by fingerprint; return the top N with counts.

    Truncates to 120 chars and collapses digit runs to `#`, so "attempt 1" and
    "attempt 17" bucket together.
    """
    import re

    def _fingerprint(msg: str) -> str:
        s = (msg or "")[:120].strip()
        return re.sub(r"\d+", "#", s)

    fingerprints: Counter[str] = Counter()
    for msg in error_messages:
        if not msg or not msg.strip():
            continue
        fingerprints[_fingerprint(msg)] += 1
    return [
        {"sample": fp, "count": count}
        for fp, count in fingerprints.most_common(n)
    ]


async def generate_digest(
    tenant_slug: str,
    window_start: datetime,
    window_end: datetime,
    by_status_count: dict[str, int],
    by_type_failed_count: dict[str, int],
    error_messages: list[str],
) -> tuple[IncidentDigest, dict[str, Any], str]:
    """Call Claude and return (digest, usage, model_id).

    `error_messages` is fingerprinted internally. Raises DigestDisabledError when off;
    anthropic errors and `llm_digest_timeout_seconds` timeouts propagate (ADR 0005).
    """
    settings = get_settings()
    if not settings.llm_digest_enabled:
        raise DigestDisabledError(
            "Incident digests disabled (set LLM_DIGEST_ENABLED=1)"
        )

    client = anthropic.AsyncAnthropic()

    aggregates = {
        "tenant": tenant_slug,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "by_status": by_status_count,
        "failed_by_type": by_type_failed_count,
        "top_errors": _top_errors(error_messages),
    }

    async def _call() -> Any:
        return await client.messages.parse(
            model=settings.llm_digest_model,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Write a digest for this window. Respond with an "
                        "IncidentDigest.\n\n"
                        f"```json\n{json.dumps(aggregates, indent=2, default=str)}\n```"
                    ),
                }
            ],
            output_format=IncidentDigest,
        )

    response = await asyncio.wait_for(
        _call(), timeout=settings.llm_digest_timeout_seconds
    )

    digest = response.parsed_output
    if digest is None:
        raise RuntimeError(
            f"digest parse returned no output (stop_reason={response.stop_reason})"
        )
    return digest, extract_usage(response), settings.llm_digest_model


async def collect_window_stats(
    session: AsyncSession,
    tenant: Tenant,
    window_start: datetime,
    window_end: datetime,
) -> tuple[dict[str, int], dict[str, int], list[str]] | None:
    """The read half: aggregate the window; None when there are no jobs to summarize.

    Split out so the transaction closes before the Anthropic round-trip.
    """
    by_status, failed_by_type, errors = await DigestRepository(session).window_stats(
        tenant.id, window_start, window_end
    )
    if sum(by_status.values()) == 0:
        logger.info(
            "digest skipped — empty window",
            extra={"tenant_id": str(tenant.id), "tenant_slug": tenant.slug},
        )
        return None
    return by_status, failed_by_type, errors


async def persist_digest(
    session: AsyncSession,
    tenant: Tenant,
    window_start: datetime,
    window_end: datetime,
    by_status: dict[str, int],
    failed_by_type: dict[str, int],
    digest_obj: IncidentDigest,
    usage: dict[str, Any],
    model: str,
) -> DigestRow:
    """The write half. Caller owns the transaction boundary."""
    row = DigestRow(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        window_start=window_start,
        window_end=window_end,
        summary=digest_obj.summary,
        highlights={
            "key_concerns": digest_obj.key_concerns,
            "recommended_actions": digest_obj.recommended_actions,
            "by_status": by_status,
            "failed_by_type": failed_by_type,
        },
        model_used=model,
        usage=usage,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "digest persisted",
        extra={
            "tenant_id": str(tenant.id),
            "tenant_slug": tenant.slug,
            "window_start": window_start.isoformat(),
            "total_jobs": sum(by_status.values()),
            "usage_input_tokens": usage["input_tokens"],
            "usage_cache_read": usage["cache_read_input_tokens"],
        },
    )
    return row


async def run_digest_for_tenant(
    session: AsyncSession,
    tenant: Tenant,
    window_start: datetime,
    window_end: datetime,
) -> DigestRow | None:
    """Generate and persist one tenant's digest on one session; None for an empty window.

    **Deletion candidate, not a supported entry point** — WO-R2-127 moved its last caller to
    the read/call/write split, and only `tests/unit/test_incident_digest.py` still needs it.
    Never call it from a request path: it holds the transaction across the LLM round-trip.
    """
    stats = await collect_window_stats(session, tenant, window_start, window_end)
    if stats is None:
        return None
    by_status, failed_by_type, errors = stats

    digest_obj, usage, model = await generate_digest(
        tenant_slug=tenant.slug,
        window_start=window_start,
        window_end=window_end,
        by_status_count=by_status,
        by_type_failed_count=failed_by_type,
        error_messages=errors,
    )
    return await persist_digest(
        session,
        tenant,
        window_start,
        window_end,
        by_status,
        failed_by_type,
        digest_obj,
        usage,
        model,
    )


async def run_digest_for_all_active_tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Iterate active tenants and generate a digest each. Returns the count
    of digests actually written (skipped tenants don't count). Best-effort:
    a failure for one tenant doesn't stop the others."""
    settings = get_settings()
    window_end = datetime.now(UTC)
    window_start = window_end - timedelta(hours=settings.llm_digest_window_hours)

    written = 0
    async with session_factory() as session:
        active = (
            await session.execute(select(Tenant).where(Tenant.is_active.is_(True)))
        ).scalars().all()

    for tenant in active:
        try:
            # Three phases, deliberately not one transaction: the Anthropic
            # round-trip sits between them holding no connection, where the old
            # shape pinned one per tenant for as long as the API took.
            async with session_factory() as session:
                async with session.begin():
                    stats = await collect_window_stats(
                        session, tenant, window_start, window_end
                    )
            if stats is None:
                continue
            by_status, failed_by_type, errors = stats

            digest_obj, usage, model = await generate_digest(
                tenant_slug=tenant.slug,
                window_start=window_start,
                window_end=window_end,
                by_status_count=by_status,
                by_type_failed_count=failed_by_type,
                error_messages=errors,
            )

            async with session_factory() as session:
                async with session.begin():
                    await persist_digest(
                        session,
                        tenant,
                        window_start,
                        window_end,
                        by_status,
                        failed_by_type,
                        digest_obj,
                        usage,
                        model,
                    )
            written += 1
        except DigestDisabledError:
            # Feature got toggled off mid-run; stop early.
            logger.info("digest run aborted — feature disabled")
            return written
        except Exception as exc:
            # Don't let one tenant's API blip kill the batch.
            logger.warning(
                "digest failed for tenant",
                extra={"tenant_id": str(tenant.id), "error": str(exc)},
            )
    return written
