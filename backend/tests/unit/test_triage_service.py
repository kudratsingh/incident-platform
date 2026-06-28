"""Unit tests for the triage service — Anthropic client mocked."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import get_settings
from app.services import triage
from app.services.triage import TriageAnalysis, TriageDisabledError


async def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    try:
        with pytest.raises(TriageDisabledError):
            await triage.triage_failure(
                job_type="csv_upload",
                payload={"file": "x.csv"},
                error_message="boom",
                retry_count=3,
                max_retries=3,
                trace_id="t",
            )
    finally:
        get_settings.cache_clear()


async def test_calls_anthropic_with_cached_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled, we should send a system block with cache_control set."""
    monkeypatch.setenv("LLM_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake_response = MagicMock()
    fake_response.parsed_output = TriageAnalysis(
        root_cause_category="validation_error",
        summary="Missing required field 'amount'.",
        suggested_fix="Add the amount field to the payload, then replay.",
        is_retryable=False,
        confidence=0.9,
    )
    fake_response.usage = MagicMock(
        input_tokens=200,
        output_tokens=80,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1500,
    )

    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    try:
        with patch("app.services.triage.anthropic.AsyncAnthropic", return_value=fake_client):
            analysis, usage, model = await triage.triage_failure(
                job_type="bulk_api_sync",
                payload={"endpoint": "https://api.example.com/x"},
                error_message="HTTP 500",
                retry_count=3,
                max_retries=3,
                trace_id="trace-xyz",
            )

        assert analysis.root_cause_category == "validation_error"
        assert usage["cache_read_input_tokens"] == 1500
        assert model == "claude-opus-4-7"

        call_kwargs = fake_client.messages.parse.await_args.kwargs
        # System prompt is a list with cache_control on the (only) block.
        system_blocks = call_kwargs["system"]
        assert isinstance(system_blocks, list)
        assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
        # Pydantic schema is bound to output_format.
        assert call_kwargs["output_format"] is TriageAnalysis
        # Adaptive thinking is enabled.
        assert call_kwargs["thinking"] == {"type": "adaptive"}
        # And the model defaults to opus-4-7.
        assert call_kwargs["model"] == "claude-opus-4-7"
    finally:
        monkeypatch.delenv("LLM_TRIAGE_ENABLED", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        get_settings.cache_clear()


async def test_raises_when_parse_returns_no_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal or schema mismatch surfaces as a RuntimeError so callers can
    decide whether to swallow or re-raise — not as a silent None."""
    monkeypatch.setenv("LLM_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake_response = MagicMock()
    fake_response.parsed_output = None
    fake_response.stop_reason = "refusal"
    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    try:
        with patch("app.services.triage.anthropic.AsyncAnthropic", return_value=fake_client):
            with pytest.raises(RuntimeError, match="refusal"):
                await triage.triage_failure(
                    job_type="csv_upload",
                    payload={},
                    error_message="anything",
                    retry_count=3,
                    max_retries=3,
                    trace_id=str(uuid.uuid4()),
                )
    finally:
        monkeypatch.delenv("LLM_TRIAGE_ENABLED", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        get_settings.cache_clear()
