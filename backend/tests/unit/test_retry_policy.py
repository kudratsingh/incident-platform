"""Unit tests for the LLM-guided retry-policy service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import get_settings
from app.services import retry_policy
from app.services.retry_policy import RetryDecision, RetryPolicyDisabledError


async def test_disabled_by_default() -> None:
    get_settings.cache_clear()
    try:
        with pytest.raises(RetryPolicyDisabledError):
            await retry_policy.decide_retry(
                job_type="csv_upload",
                error_message="boom",
                retry_count=2,
                max_retries=5,
            )
    finally:
        get_settings.cache_clear()


async def test_calls_anthropic_with_cached_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled, we should send a system block with cache_control set."""
    monkeypatch.setenv("LLM_RETRY_POLICY_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake_response = MagicMock()
    fake_response.parsed_output = RetryDecision(
        action="retry_with_backoff",
        backoff_seconds=60,
        reasoning="Transient 500 from upstream, worth one more try.",
    )
    fake_response.usage = MagicMock(
        input_tokens=200,
        output_tokens=40,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1500,
    )

    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    try:
        with patch(
            "app.services.retry_policy.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            decision, usage, model = await retry_policy.decide_retry(
                job_type="bulk_api_sync",
                error_message="HTTP 500",
                retry_count=2,
                max_retries=5,
                prior_error="HTTP 502",
            )
    finally:
        get_settings.cache_clear()

    assert decision.action == "retry_with_backoff"
    assert decision.backoff_seconds == 60
    assert usage["input_tokens"] == 200
    assert model == "claude-opus-4-7"
    fake_client.messages.parse.assert_awaited_once()
    kwargs = fake_client.messages.parse.await_args.kwargs
    # The system prompt must be a list with cache_control on the last block
    # — that's what gives us the prefix cache hit across retries.
    assert isinstance(kwargs["system"], list)
    assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # Adaptive thinking — failure classification benefits from it.
    assert kwargs["thinking"] == {"type": "adaptive"}


async def test_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow API call must time out — the worker can't block indefinitely."""
    monkeypatch.setenv("LLM_RETRY_POLICY_ENABLED", "true")
    monkeypatch.setenv("LLM_RETRY_POLICY_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    async def _slow(**_kwargs: object) -> object:
        await asyncio.sleep(0.5)
        return MagicMock()

    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(side_effect=_slow)

    try:
        with patch(
            "app.services.retry_policy.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            with pytest.raises(asyncio.TimeoutError):
                await retry_policy.decide_retry(
                    job_type="csv_upload",
                    error_message="boom",
                    retry_count=1,
                    max_retries=5,
                )
    finally:
        get_settings.cache_clear()


async def test_dead_letter_now_decision_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_RETRY_POLICY_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake_response = MagicMock()
    fake_response.parsed_output = RetryDecision(
        action="dead_letter_now",
        backoff_seconds=0,
        reasoning="Authentication failed — won't recover.",
    )
    fake_response.usage = MagicMock(
        input_tokens=100, output_tokens=30,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    try:
        with patch(
            "app.services.retry_policy.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            decision, _, _ = await retry_policy.decide_retry(
                job_type="bulk_api_sync",
                error_message="401 Unauthorized",
                retry_count=2,
                max_retries=5,
            )
    finally:
        get_settings.cache_clear()

    assert decision.action == "dead_letter_now"
