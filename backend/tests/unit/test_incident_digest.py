"""Unit tests for the periodic-digest service — Anthropic client mocked."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import get_settings
from app.services import incident_digest
from app.services.incident_digest import (
    DigestDisabledError,
    IncidentDigest,
    _top_errors,
)


def test_top_errors_buckets_by_fingerprint() -> None:
    """Error strings that differ only in digit values must collapse into one
    bucket — `attempt 1` and `attempt 27` are the same pattern."""
    msgs = [
        "Connection timed out reaching upstream API (attempt 1)",
        "Connection timed out reaching upstream API (attempt 2)",
        "Connection timed out reaching upstream API (attempt 27)",
        "Validation failed: missing required field 'amount'",
        "Validation failed: missing required field 'amount'",
        "Unique error somewhere",
    ]
    out = _top_errors(msgs, n=5)
    counts = {row["sample"]: row["count"] for row in out}
    timeout_key = "Connection timed out reaching upstream API (attempt #)"
    val_key = "Validation failed: missing required field 'amount'"
    # Three timeouts collapsed; two validations collapsed; one unique stays.
    assert counts[timeout_key] == 3
    assert counts[val_key] == 2


def test_top_errors_skips_empty() -> None:
    out = _top_errors(["", "   ", "real"], n=5)
    assert all(row["sample"] for row in out)


async def test_disabled_by_default() -> None:
    get_settings.cache_clear()
    try:
        with pytest.raises(DigestDisabledError):
            await incident_digest.generate_digest(
                tenant_slug="acme",
                window_start=datetime.now(UTC) - timedelta(hours=24),
                window_end=datetime.now(UTC),
                by_status_count={"completed": 100, "failed": 5},
                by_type_failed_count={"csv_upload": 5},
                error_messages=["boom"],
            )
    finally:
        get_settings.cache_clear()


async def test_calls_anthropic_with_cached_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake_response = MagicMock()
    fake_response.parsed_output = IncidentDigest(
        summary="A handful of csv_upload failures from one upstream timeout pattern.",
        key_concerns=["3 timeouts to the same upstream"],
        recommended_actions=["Check upstream health dashboard"],
    )
    fake_response.usage = MagicMock(
        input_tokens=400,
        output_tokens=120,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1500,
    )

    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    try:
        with patch(
            "app.services.incident_digest.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            digest, usage, model = await incident_digest.generate_digest(
                tenant_slug="acme",
                window_start=datetime.now(UTC) - timedelta(hours=24),
                window_end=datetime.now(UTC),
                by_status_count={"completed": 100, "failed": 3},
                by_type_failed_count={"csv_upload": 3},
                error_messages=[
                    "Connection timed out reaching upstream API",
                    "Connection timed out reaching upstream API",
                    "Connection timed out reaching upstream API",
                ],
            )
    finally:
        get_settings.cache_clear()

    assert "csv_upload" in digest.summary
    assert digest.key_concerns
    assert usage["input_tokens"] == 400
    assert model == "claude-opus-4-7"
    fake_client.messages.parse.assert_awaited_once()
    kwargs = fake_client.messages.parse.await_args.kwargs
    assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["thinking"] == {"type": "adaptive"}


async def test_run_digest_for_tenant_skips_empty_window(
    monkeypatch: pytest.MonkeyPatch, db_session, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """When the window has zero jobs the LLM call must not happen and no
    row is persisted — saves cost and avoids a meaningless 'nothing
    happened' digest."""
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    called = AsyncMock()
    try:
        with patch(
            "app.services.incident_digest.generate_digest", new=called
        ):
            row = await incident_digest.run_digest_for_tenant(
                db_session,
                default_tenant,
                window_start=datetime.now(UTC) - timedelta(hours=24),
                window_end=datetime.now(UTC),
            )
    finally:
        get_settings.cache_clear()

    assert row is None
    called.assert_not_called()


# ---------------------------------------------------------------------------
# The window query's error samples (WO-R2-63)
# ---------------------------------------------------------------------------


async def _job(  # type: ignore[no-untyped-def]
    session, tenant_id, *, status: str, error_message: str | None, minutes_ago: int = 30
):
    from app.models.enums import JobType
    from app.models.job import Job

    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    job = Job(
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        type=JobType.CSV_UPLOAD,
        status=status,
        payload={"rows": 1},
        error_message=error_message,
        created_at=created,
        updated_at=created,
    )
    session.add(job)
    await session.flush()
    return job


async def test_error_samples_exclude_a_job_that_failed_then_succeeded(
    db_session, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The retry that worked is not an incident.

    `error_message` is not cleared when a retry succeeds, so a completed job
    still carries the text of the attempt that failed. Selecting on
    `error_message IS NOT NULL` alone fed those to the digest as though they
    were outcomes — the narrative then described failures the counts beside
    it (always status-filtered) did not report.
    """
    from app.repositories.digest import DigestRepository

    await _job(
        db_session,
        default_tenant.id,
        status="completed",
        error_message="transient: connection reset (attempt 1)",
    )
    await _job(
        db_session,
        default_tenant.id,
        status="dead_letter",
        error_message="ValueError: bad row 15382",
    )
    await _job(
        db_session,
        default_tenant.id,
        status="failed",
        error_message="TimeoutError('stripe.api')",
    )

    _by_status, _by_type, errors = await DigestRepository(db_session).window_stats(
        default_tenant.id,
        datetime.now(UTC) - timedelta(hours=24),
        datetime.now(UTC),
    )

    assert sorted(errors) == sorted(
        ["ValueError: bad row 15382", "TimeoutError('stripe.api')"]
    )
    assert not any("transient" in e for e in errors)


async def test_error_samples_and_failed_counts_describe_the_same_jobs(
    db_session, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The two halves of the digest's input must not disagree: whatever the
    counts call a failure is what the samples are drawn from."""
    from app.repositories.digest import DigestRepository

    for _ in range(3):
        await _job(
            db_session,
            default_tenant.id,
            status="completed",
            error_message="transient, recovered on retry",
        )
    await _job(
        db_session, default_tenant.id, status="failed", error_message="real failure"
    )

    _by_status, failed_by_type, errors = await DigestRepository(
        db_session
    ).window_stats(
        default_tenant.id,
        datetime.now(UTC) - timedelta(hours=24),
        datetime.now(UTC),
    )

    assert sum(failed_by_type.values()) == len(errors) == 1


# ---------------------------------------------------------------------------
# No DB transaction is held across the LLM call (WO-R2-63 / WO-R2-08)
#
# The transaction half of R2-63 was fixed by #161, which split the worker's
# digest into read / call / write phases. This is the assertion that fix
# never had: it is green at master by design, and it is here so that
# re-composing the three phases into one `session.begin()` — the shape the
# code had for its whole life before #161 — turns red instead of quietly
# parking a pooled connection in `idle in transaction` for the length of an
# Anthropic round-trip, once per tenant, serially.
# ---------------------------------------------------------------------------


class _TrackingFactory:
    """A session_factory that remembers every session it hands out."""

    def __init__(self, factory) -> None:  # type: ignore[no-untyped-def]
        self._factory = factory
        self.sessions: list[Any] = []

    def __call__(self):  # type: ignore[no-untyped-def]
        session = self._factory()
        self.sessions.append(session)
        return session


async def test_no_transaction_is_open_during_the_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.base import Base
    from app.models.enums import JobType
    from app.models.job import Job
    from app.models.tenant import Tenant
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id = uuid.uuid4()
    created = datetime.now(UTC) - timedelta(minutes=30)
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id, slug="acme", name="Acme", is_active=True
                )
            )
            session.add(
                Job(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=uuid.uuid4(),
                    type=JobType.CSV_UPLOAD,
                    status="dead_letter",
                    payload={"rows": 1},
                    error_message="boom",
                    created_at=created,
                    updated_at=created,
                )
            )

    tracking = _TrackingFactory(factory)
    open_transactions: list[str] = []

    async def _fake_generate(**_kwargs: Any):  # type: ignore[no-untyped-def]
        # The moment the round-trip would be in flight.
        open_transactions.extend(
            repr(s) for s in tracking.sessions if s.in_transaction()
        )
        digest = MagicMock()
        digest.summary = "all quiet"
        digest.key_concerns = []
        digest.recommended_actions = []
        usage = {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        return digest, usage, "claude-test"

    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        with patch(
            "app.services.incident_digest.generate_digest", new=_fake_generate
        ):
            written = await incident_digest.run_digest_for_all_active_tenants(
                tracking  # type: ignore[arg-type]
            )
    finally:
        get_settings.cache_clear()
        await engine.dispose()

    assert written == 1, "the digest must still be produced and persisted"
    assert open_transactions == [], (
        "a DB transaction was open across the Anthropic round-trip — that is "
        "one pooled connection parked `idle in transaction` per tenant, on "
        "the pool the SSE and backpressure paths contend for"
    )
