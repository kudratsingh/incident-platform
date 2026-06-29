"""Unit tests for the LLM triage consumer."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.triage import TriageAnalysis, TriageDisabledError
from app.workers.triage_consumer import LlmTriageConsumer


def _factory() -> MagicMock:
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)
    return MagicMock(return_value=session)


def _dlq_value(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event": "job.failed",
        "dead_lettered": True,
        "tenant_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
        "error": "timeout calling upstream",
        "retry_count": 3,
        "max_retries": 3,
        "trace_id": "trace-abc",
    }
    base.update(overrides)
    return base


async def test_skips_when_triage_disabled() -> None:
    factory = _factory()
    consumer = LlmTriageConsumer(factory)

    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=False), \
         patch("app.workers.triage_consumer.triage_service.triage_failure") as call:
        await consumer.handle_message(
            topic="job.dlq", key="u", value=_dlq_value()
        )
    call.assert_not_called()


async def test_skips_non_dlq_event() -> None:
    """job.completed should never reach this consumer, but if it does, skip."""
    factory = _factory()
    consumer = LlmTriageConsumer(factory)

    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=True), \
         patch("app.workers.triage_consumer.triage_service.triage_failure") as call:
        await consumer.handle_message(
            topic="job.dlq",
            key="u",
            value={"event": "job.completed", "job_id": str(uuid.uuid4())},
        )
    call.assert_not_called()


async def test_skips_non_terminal_failure() -> None:
    """job.failed without dead_lettered=True is a retry, not a triage trigger."""
    factory = _factory()
    consumer = LlmTriageConsumer(factory)

    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=True), \
         patch("app.workers.triage_consumer.triage_service.triage_failure") as call:
        await consumer.handle_message(
            topic="job.failed",
            key="u",
            value=_dlq_value(dead_lettered=False),
        )
    call.assert_not_called()


async def test_persists_analysis_on_success() -> None:
    factory = _factory()
    consumer = LlmTriageConsumer(factory)
    job_id = uuid.uuid4()
    analysis = TriageAnalysis(
        root_cause_category="external_api_failure",
        summary="Upstream HTTPS endpoint timed out after 30 s",
        suggested_fix="Confirm the upstream is healthy; raise the worker's HTTP timeout to 60s.",
        is_retryable=True,
        confidence=0.85,
    )
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 1500,
        "cache_creation_input_tokens": 0,
    }

    triage_repo = AsyncMock()
    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=True), \
         patch(
             "app.workers.triage_consumer.triage_service.triage_failure",
             new=AsyncMock(return_value=(analysis, usage, "claude-opus-4-7")),
         ), \
         patch("app.workers.triage_consumer.TriageRepository", return_value=triage_repo):
        await consumer.handle_message(
            topic="job.dlq",
            key="u",
            value=_dlq_value(job_id=str(job_id)),
        )

    triage_repo.upsert.assert_awaited_once()
    kwargs = triage_repo.upsert.await_args.kwargs
    assert kwargs["job_id"] == job_id
    assert kwargs["root_cause_category"] == "external_api_failure"
    assert kwargs["is_retryable"] is True
    assert kwargs["model_used"] == "claude-opus-4-7"
    assert kwargs["usage"]["cache_read_input_tokens"] == 1500


async def test_triage_disabled_error_is_swallowed() -> None:
    """A late `is_enabled()` check inside triage_failure shouldn't tear down
    the consumer — treat it the same as the upfront skip."""
    factory = _factory()
    consumer = LlmTriageConsumer(factory)

    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=True), \
         patch(
             "app.workers.triage_consumer.triage_service.triage_failure",
             new=AsyncMock(side_effect=TriageDisabledError("nope")),
         ):
        await consumer.handle_message(
            topic="job.dlq", key="u", value=_dlq_value()
        )


async def test_anthropic_api_error_propagates() -> None:
    """A 5xx from Anthropic must propagate so the consumer doesn't commit the
    offset — Kafka re-delivers later when the API recovers."""
    import anthropic
    from httpx import Request, Response

    factory = _factory()
    consumer = LlmTriageConsumer(factory)
    resp = Response(status_code=529, request=Request("POST", "https://api.anthropic.com"))
    err = anthropic.APIStatusError(
        message="server overloaded",
        response=resp,
        body={"type": "error", "error": {"type": "overloaded_error"}},
    )

    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=True), \
         patch(
             "app.workers.triage_consumer.triage_service.triage_failure",
             new=AsyncMock(side_effect=err),
         ):
        with pytest.raises(anthropic.APIStatusError):
            await consumer.handle_message(
                topic="job.dlq", key="u", value=_dlq_value()
            )


async def test_skips_invalid_job_id() -> None:
    factory = _factory()
    consumer = LlmTriageConsumer(factory)

    with patch("app.workers.triage_consumer.triage_service.is_enabled", return_value=True), \
         patch("app.workers.triage_consumer.triage_service.triage_failure") as call:
        await consumer.handle_message(
            topic="job.dlq",
            key="u",
            value=_dlq_value(job_id="not-a-uuid"),
        )
    call.assert_not_called()
