"""Unit tests for the periodic-digest service — Anthropic client mocked."""

from datetime import UTC, datetime, timedelta
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
