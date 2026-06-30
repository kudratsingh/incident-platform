"""API contract tests for POST /admin/query."""

from unittest.mock import AsyncMock, patch

import pytest
from app.config import get_settings
from app.services.nl_query import JobFilterSpec
from httpx import AsyncClient


async def test_disabled_returns_503(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Default config: feature off → 503 + error_code=nl_query_unavailable."""
    resp = await client.post(
        "/api/v1/admin/query",
        json={"question": "failed CSV uploads"},
        headers=admin_headers,
    )
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "nl_query_unavailable"


async def test_empty_question_returns_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/api/v1/admin/query",
        json={"question": ""},
        headers=admin_headers,
    )
    # 400-class — request validation error.
    assert 400 <= resp.status_code < 500


async def test_question_too_long_returns_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/api/v1/admin/query",
        json={"question": "x" * 501},
        headers=admin_headers,
    )
    assert 400 <= resp.status_code < 500


async def test_enabled_returns_spec_and_items(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled and the LLM returns a valid spec, we surface both the
    filter spec and the rows."""
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake_spec = JobFilterSpec(status="dead_letter", limit=10)
    fake = AsyncMock(return_value=(fake_spec, {"input_tokens": 100}, "claude-opus-4-7"))
    try:
        with patch("app.services.nl_query.parse_question", new=fake):
            resp = await client.post(
                "/api/v1/admin/query",
                json={"question": "dead-lettered jobs"},
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["spec"]["status"] == "dead_letter"
    assert body["spec"]["limit"] == 10
    assert "items" in body
    assert "total" in body
    assert body["model"] == "claude-opus-4-7"


async def test_llm_failure_surfaces_as_503(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout or network blip from the LLM call shouldn't 500 — return
    503 so the UI can show 'try again'."""
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake = AsyncMock(side_effect=RuntimeError("network down"))
    try:
        with patch("app.services.nl_query.parse_question", new=fake):
            resp = await client.post(
                "/api/v1/admin/query",
                json={"question": "anything"},
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 503
    assert resp.json()["error_code"] == "nl_query_unavailable"
