"""Every LLM feature fails open, with the timeout ADR 0005 already promises
(WO-R2-08).

Two defects, one contract. ADR 0005 says every LLM feature fails open, that a
call which "times out (configurable per feature; defaults to 10s)" is one of
the conditions that must trigger the fallback, and it explicitly rejects
block-and-retry. Today only `retry_policy` has a timeout, and the triage
consumer does the one thing the ADR rejects: a deterministic failure escapes
`handle_message`, the base consumer seeks back to that exact offset, and the
same poison message is redelivered forever — each iteration a full, billed
Anthropic call.

The tests below assert on behaviour rather than on the new settings, so the
timeout cases are red against the unbounded code for the right reason: the
slow call is awaited to completion instead of being abandoned at the
deadline.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest
from aiokafka import ConsumerRecord, TopicPartition
from app.config import get_settings
from app.services import incident_digest, nl_query, triage
from app.workers.triage_consumer import LlmTriageConsumer
from httpx import Request, Response

# How long the fake Anthropic client blocks for. Comfortably longer than the
# 10ms deadline the tests configure, and short enough that a *missing* timeout
# still finishes the test rather than hanging the suite.
_SLOW_CALL_SECONDS = 0.5
_DEADLINE_SECONDS = 0.01


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _slow_client() -> MagicMock:
    """An Anthropic client whose `messages.parse` never returns in time."""

    async def _never_returns(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(_SLOW_CALL_SECONDS)
        raise AssertionError(
            "the LLM call ran to completion — it should have been abandoned "
            "at the configured deadline"
        )

    client = MagicMock()
    client.messages.parse = AsyncMock(side_effect=_never_returns)
    return client


def _api_status_error(status_code: int) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message=f"status {status_code}",
        response=Response(
            status_code=status_code,
            request=Request("POST", "https://api.anthropic.com"),
        ),
        body={"type": "error", "error": {"type": "test_error"}},
    )


def _dlq_value(**overrides: object) -> dict[str, object]:
    """A `job.dlq` event value shaped exactly like the dispatcher's."""
    base: dict[str, object] = {
        "event": "job.failed",
        "dead_lettered": True,
        "tenant_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "job_type": "csv_upload",
        "error": "timeout calling upstream",
        "message": "Job exhausted after 3 attempts: timeout calling upstream",
        "retry_count": 3,
        "max_retries": 3,
        "payload": {"file": "x.csv"},
        "trace_id": "trace-abc",
    }
    base.update(overrides)
    return base


def _session_factory() -> MagicMock:
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.begin = MagicMock(return_value=begin_ctx)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _record(value: dict[str, object], offset: int = 7) -> ConsumerRecord:
    return ConsumerRecord(
        topic="job.dlq",
        partition=0,
        offset=offset,
        timestamp=0,
        timestamp_type=0,
        key=None,
        value=value,
        checksum=None,
        serialized_key_size=-1,
        serialized_value_size=2,
        headers=(),
    )


# ---------------------------------------------------------------------------
# ADR 0005: "times out (configurable per feature; defaults to 10s)"
# ---------------------------------------------------------------------------


async def _assert_abandoned_at_deadline(coro: Any) -> None:
    """The call is abandoned at the deadline, not awaited to completion."""
    started = asyncio.get_running_loop().time()
    with pytest.raises(asyncio.TimeoutError):
        await coro
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < _SLOW_CALL_SECONDS, (
        f"the deadline did not fire — the call was awaited for {elapsed:.2f}s, "
        "which means the worker blocks for as long as the API does"
    )


async def test_triage_is_abandoned_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_TRIAGE_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TRIAGE_TIMEOUT_SECONDS", str(_DEADLINE_SECONDS))
    get_settings.cache_clear()
    try:
        with patch(
            "app.services.triage.anthropic.AsyncAnthropic",
            return_value=_slow_client(),
        ):
            await _assert_abandoned_at_deadline(
                triage.triage_failure(
                    job_type="csv_upload",
                    payload={"file": "x.csv"},
                    error_message="boom",
                    retry_count=3,
                    max_retries=3,
                    trace_id=None,
                )
            )
    finally:
        get_settings.cache_clear()


async def test_nl_query_is_abandoned_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_NL_QUERY_TIMEOUT_SECONDS", str(_DEADLINE_SECONDS))
    get_settings.cache_clear()
    try:
        with patch(
            "app.services.nl_query.anthropic.AsyncAnthropic",
            return_value=_slow_client(),
        ):
            await _assert_abandoned_at_deadline(
                nl_query.parse_question("which jobs failed today?")
            )
    finally:
        get_settings.cache_clear()


async def test_digest_is_abandoned_at_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("LLM_DIGEST_TIMEOUT_SECONDS", str(_DEADLINE_SECONDS))
    get_settings.cache_clear()
    try:
        with patch(
            "app.services.incident_digest.anthropic.AsyncAnthropic",
            return_value=_slow_client(),
        ):
            now = datetime.now(UTC)
            await _assert_abandoned_at_deadline(
                incident_digest.generate_digest(
                    tenant_slug="acme",
                    window_start=now - timedelta(hours=24),
                    window_end=now,
                    by_status_count={"failed": 3},
                    by_type_failed_count={"csv_upload": 3},
                    error_messages=["boom"],
                )
            )
    finally:
        get_settings.cache_clear()


def test_every_llm_feature_has_a_timeout_setting() -> None:
    """ADR 0005 promises the timeout is configurable *per feature*, with a
    10s default. Three of the four features had no such setting."""
    settings = get_settings()
    for field in (
        "llm_triage_timeout_seconds",
        "llm_nl_query_timeout_seconds",
        "llm_digest_timeout_seconds",
        "llm_retry_policy_timeout_seconds",
    ):
        assert hasattr(settings, field), f"ADR 0005 requires {field}"
        assert getattr(settings, field) == 10.0, f"{field} should default to 10s"


# ---------------------------------------------------------------------------
# The uncapped redelivery loop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(RuntimeError("parsed_output was None"), id="refusal-or-schema"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(ValueError("bad payload"), id="deterministic"),
    ],
)
async def test_a_deterministic_triage_failure_fails_open(
    failure: BaseException,
) -> None:
    """ADR 0005's stated fallback for DLQ triage: no triage row is written and
    the admin sees the job in the DLQ with its raw error_message. What must
    NOT happen is the escape that makes the base consumer redeliver."""
    factory = _session_factory()
    consumer = LlmTriageConsumer(factory)
    repo = AsyncMock()

    with patch(
        "app.workers.triage_consumer.triage_service.is_enabled", return_value=True
    ), patch(
        "app.workers.triage_consumer.triage_service.triage_failure",
        new=AsyncMock(side_effect=failure),
    ), patch(
        "app.workers.triage_consumer.TriageRepository", return_value=repo
    ):
        await consumer.handle_message(topic="job.dlq", key="u", value=_dlq_value())

    repo.upsert.assert_not_awaited()


async def test_a_poison_message_is_delivered_once_and_committed() -> None:
    """The end-to-end contract the finding names: one delivery, no triage row,
    offset advanced, no seek-back. Today the handler raises, `_process_batch`
    seeks back to this exact offset, and the next poll refetches it — one
    billed Anthropic call per second, forever, on one job.dlq partition."""
    factory = _session_factory()
    consumer = LlmTriageConsumer(factory)
    consumer._consumer = AsyncMock()
    consumer._consumer.seek = MagicMock()
    tp = TopicPartition("job.dlq", 0)
    calls = 0

    async def _always_fails(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("triage parse returned no output (stop_reason=refusal)")

    with patch(
        "app.workers.triage_consumer.triage_service.is_enabled", return_value=True
    ), patch(
        "app.workers.triage_consumer.triage_service.triage_failure",
        new=AsyncMock(side_effect=_always_fails),
    ), patch(
        "app.workers.triage_consumer.TriageRepository", return_value=AsyncMock()
    ), patch(
        "app.workers.kafka_consumer.validate_schema", return_value=None
    ):
        had_failure = await consumer._process_batch({tp: [_record(_dlq_value())]})

    assert had_failure is False, (
        "the poison message was reported as a failure, so the partition seeks "
        "back and redelivers it forever"
    )
    committed = [
        c.args[0] if c.args else c.kwargs["offsets"]
        for c in consumer._consumer.commit.await_args_list
    ]
    assert committed == [{tp: 8}], (
        f"offset was not advanced past the poison message — committed {committed}"
    )
    consumer._consumer.seek.assert_not_called()
    assert calls == 1, f"the message was handled {calls}x, not once"


async def test_a_transient_upstream_fault_still_redelivers() -> None:
    """The deliberate carve-out, kept: a 529 is the upstream being briefly
    unavailable, not a poison message. Re-raising means no commit, so Kafka
    redelivers once the API recovers."""
    factory = _session_factory()
    consumer = LlmTriageConsumer(factory)

    with patch(
        "app.workers.triage_consumer.triage_service.is_enabled", return_value=True
    ), patch(
        "app.workers.triage_consumer.triage_service.triage_failure",
        new=AsyncMock(side_effect=_api_status_error(529)),
    ):
        with pytest.raises(anthropic.APIStatusError):
            await consumer.handle_message(
                topic="job.dlq", key="u", value=_dlq_value()
            )


@pytest.mark.parametrize("status_code", [400, 401, 404, 422])
async def test_a_deterministic_api_status_fails_open(status_code: int) -> None:
    """`APIStatusError` is every non-2xx, not just the "5xx / 529" the code's
    comment claims. A 400 (bad model id after a config typo, an oversized
    payload) is deterministic: re-raising it redelivers the same message and
    re-bills the same call forever, which is the finding's own blast radius
    narrowed to 4xx."""
    factory = _session_factory()
    consumer = LlmTriageConsumer(factory)
    repo = AsyncMock()

    with patch(
        "app.workers.triage_consumer.triage_service.is_enabled", return_value=True
    ), patch(
        "app.workers.triage_consumer.triage_service.triage_failure",
        new=AsyncMock(side_effect=_api_status_error(status_code)),
    ), patch(
        "app.workers.triage_consumer.TriageRepository", return_value=repo
    ):
        await consumer.handle_message(topic="job.dlq", key="u", value=_dlq_value())

    repo.upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# The digest must not hold a DB transaction across the API round-trip
# ---------------------------------------------------------------------------


class _TrackedSession:
    """Just enough AsyncSession to observe when a transaction is open."""

    def __init__(self, tracker: dict[str, Any], tenants: list[Any]) -> None:
        self._tracker = tracker
        self._tenants = tenants
        self.in_transaction = False

    def begin(self) -> Any:
        session = self

        class _Tx:
            async def __aenter__(self) -> None:
                session.in_transaction = True
                session._tracker["open"] += 1

            async def __aexit__(self, *_exc: Any) -> bool:
                session.in_transaction = False
                session._tracker["open"] -= 1
                return False

        return _Tx()

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        result = MagicMock()
        result.scalars.return_value.all.return_value = self._tenants
        return result

    def add(self, _row: Any) -> None:
        return None

    async def flush(self) -> None:
        return None


def _tracked_factory(tracker: dict[str, Any], tenants: list[Any]) -> Any:
    def _factory() -> Any:
        session = _TrackedSession(tracker, tenants)

        class _Ctx:
            async def __aenter__(self) -> _TrackedSession:
                return session

            async def __aexit__(self, *_exc: Any) -> bool:
                return False

        return _Ctx()

    return _factory


async def test_digest_releases_its_transaction_before_the_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Postgres connection with an open read-write transaction was pinned for
    the whole Anthropic round-trip, per tenant, serially. A timeout bounds how
    long that lasts; releasing the transaction means it does not happen."""
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    tenant = MagicMock()
    tenant.id = uuid.uuid4()
    tenant.slug = "acme"
    tracker: dict[str, Any] = {"open": 0, "open_during_call": None}

    async def _fake_generate(*_args: Any, **_kwargs: Any) -> Any:
        tracker["open_during_call"] = tracker["open"]
        digest = MagicMock()
        digest.summary = "s"
        digest.key_concerns = []
        digest.recommended_actions = []
        return digest, {"input_tokens": 1, "cache_read_input_tokens": 0}, "model"

    repo = MagicMock()
    repo.window_stats = AsyncMock(
        return_value=({"failed": 3}, {"csv_upload": 3}, ["boom"])
    )

    try:
        with patch(
            "app.services.incident_digest.generate_digest", new=_fake_generate
        ), patch(
            "app.services.incident_digest.DigestRepository", return_value=repo
        ):
            written = await incident_digest.run_digest_for_all_active_tenants(
                _tracked_factory(tracker, [tenant])
            )
    finally:
        get_settings.cache_clear()

    assert written == 1
    assert tracker["open_during_call"] == 0, (
        "a DB transaction was open across the Anthropic round-trip — the "
        "connection is pinned for as long as the API takes"
    )
