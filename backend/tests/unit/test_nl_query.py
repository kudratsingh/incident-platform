"""Unit tests for the natural-language admin-query service."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import get_settings
from app.services import nl_query
from app.services.nl_query import JobFilterSpec, NLQueryDisabledError


async def test_disabled_by_default() -> None:
    get_settings.cache_clear()
    try:
        with pytest.raises(NLQueryDisabledError):
            await nl_query.parse_question("failed CSV uploads")
    finally:
        get_settings.cache_clear()


async def test_filter_spec_validates_enums() -> None:
    """Pydantic enum validation is what prevents prompt-injected values from
    reaching the DB. A junk status value must fail loudly."""
    with pytest.raises(ValueError):
        JobFilterSpec(status="DROP_TABLE_users")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        JobFilterSpec(type="rm -rf /")  # type: ignore[arg-type]


async def test_filter_spec_clamps_retry_bounds() -> None:
    with pytest.raises(ValueError):
        JobFilterSpec(retry_count_min=-1)
    with pytest.raises(ValueError):
        JobFilterSpec(retry_count_max=200)


async def test_filter_spec_limit_bounds() -> None:
    with pytest.raises(ValueError):
        JobFilterSpec(limit=0)
    with pytest.raises(ValueError):
        JobFilterSpec(limit=500)


async def test_calls_anthropic_with_cached_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    fake_response = MagicMock()
    fake_response.parsed_output = JobFilterSpec(
        status="dead_letter",
        type="csv_upload",
        created_after=one_hour_ago,
        limit=20,
    )
    fake_response.usage = MagicMock(
        input_tokens=300,
        output_tokens=80,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=1200,
    )

    fake_client = MagicMock()
    fake_client.messages.parse = AsyncMock(return_value=fake_response)

    try:
        with patch(
            "app.services.nl_query.anthropic.AsyncAnthropic",
            return_value=fake_client,
        ):
            spec, usage, model = await nl_query.parse_question(
                "dead-lettered CSV uploads in the last hour"
            )
    finally:
        get_settings.cache_clear()

    assert spec.status == "dead_letter"
    assert spec.type == "csv_upload"
    assert spec.created_after is not None
    assert usage["input_tokens"] == 300
    assert model == "claude-opus-4-7"
    fake_client.messages.parse.assert_awaited_once()
    kwargs = fake_client.messages.parse.await_args.kwargs
    assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["thinking"] == {"type": "adaptive"}
    # The user message must include `now` so the LLM can resolve "last hour".
    user_content = kwargs["messages"][0]["content"]
    assert "now" in user_content
