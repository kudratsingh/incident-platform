"""
Periodic incident summaries.

The digest worker collects per-tenant failure stats for a window and
hands a small structured aggregate to Claude. The model returns:

  * a one-paragraph narrative ("what happened, what we did, what we learned")
  * a short list of `key_concerns` — failure modes that recurred
  * a short list of `recommended_actions` — concrete next steps for ops

Persisted to `incident_summaries` for the admin Digests tab.

Why the LLM rather than a static template: the value here is in
identifying *patterns* across hundreds of events — "every csv_upload
failure mentioned malformed UTF-8 in column 3" is a sentence a template
can't generate, but a model with the raw aggregates can. A static
template gets you a count; an LLM gets you a hypothesis.

Implementation mirrors the other LLM services in this codebase:
  * messages.parse() + Pydantic schema → no raw JSON parsing.
  * claude-opus-4-7 + adaptive thinking.
  * Frozen system prompt with cache_control: ephemeral so the
    instructions cache across digests.
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
    """Bucket error messages by a normalized fingerprint and return the top N
    with their counts. Keeps the LLM payload small even when there are
    thousands of failures.

    The fingerprint truncates to 120 chars and collapses runs of digits to a
    single `#`. That way "attempt 1" / "attempt 2" / "attempt 17" all bucket
    together — the recurring *pattern* is what's interesting, not the
    specific attempt number.
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

    The caller has already done the SQL aggregation — this service is
    pure LLM glue. `error_messages` is the list of `error_message`
    fields from jobs that failed or dead-lettered in the window; we
    fingerprint+truncate them internally so the model sees only the
    top recurring patterns.

    Raises DigestDisabledError when the feature is off, anthropic / network
    exceptions on real API failures, and asyncio.TimeoutError when the call
    exceeds `llm_digest_timeout_seconds` (ADR 0005).
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
    """The read half: aggregate the window. None when there is nothing to
    summarize (no jobs), which is the caller's signal to skip the LLM call.

    Split out of `run_digest_for_tenant` so the worker can finish this
    transaction *before* the Anthropic round-trip — see
    `run_digest_for_all_active_tenants`.
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
    """Generate one digest for one tenant and persist it, on one session.

    Returns the persisted row, or None if there was nothing to summarize
    (empty window — no jobs). Caller manages the transaction boundary and
    only commits when this returns non-None.

    Raises DigestDisabledError when the feature is off; lets anthropic
    exceptions and asyncio.TimeoutError propagate so the caller can decide
    whether to retry.

    NOTE: this composes all three steps on the caller's session, so the LLM
    round-trip happens inside whatever transaction the caller holds. That is
    acceptable for the admin route (`POST /admin/digests`), where a request is
    already holding its own connection for its duration and the deadline above
    bounds it — but it is NOT how the worker should do it. The digest loop
    calls the three parts separately so it can close the read transaction
    first; see `run_digest_for_all_active_tenants`.
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
            # Three phases, deliberately not one transaction. The Anthropic
            # round-trip sits BETWEEN them, holding no connection: the old
            # shape opened `session.begin()`, issued the aggregate query, and
            # then awaited the API inside that transaction — pinning a pooled
            # connection and an open read-write transaction, per tenant,
            # serially, for as long as the API took. The deadline added above
            # bounds that; not holding the transaction removes it.
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
