"""Breaker state, published and read across a process boundary (WO-R3-217, WP-8.1).

The registry is in-memory and the only breaker is created in the worker, while the reader is
a separate process from the same image (ADR 0006) — so a tool walking the registry reads its
own empty one and reports every breaker closed. That is divergence H7, and this file is what
proves it closed at unit level; `tests/integration/test_breaker_visibility.py` proves it on a
real Redis. Also held here: a wall clock beside the monotonic one, a failure *class* and never
a message, and an unreachable store reading as unknown rather than as "nothing is open".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.breaker_state import (
    BREAKER_STATE_KEY_PREFIX,
    BREAKER_STATE_TTL_SECONDS,
    BREAKERS_UNKNOWN_NONE_PUBLISHED,
    BREAKERS_UNKNOWN_UNREACHABLE,
    BREAKERS_UNKNOWN_UNREADABLE,
    FAILURE_CLASS_CONNECTION,
    FAILURE_CLASS_OTHER,
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASSES,
    breaker_key_for,
    classify_failure,
    publish_breaker_state,
    read_breaker_states,
)
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext, get_tool
from app.mcp.tools.circuit_breakers import (
    GetCircuitBreakersInput,
    GetCircuitBreakersOutput,
    get_circuit_breakers,
)


class _RedisStub:
    """`get` / `set` / `scan` — the whole surface both sides of the record use."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(values or {})
        self.ttls: dict[str, int | None] = {}
        self.raise_on_get = False
        self.raise_on_set = False
        self.raise_on_scan = False

    async def get(self, key: str) -> str | None:
        if self.raise_on_get:
            raise ConnectionError("redis unreachable")
        return self.store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        if self.raise_on_set:
            raise ConnectionError("redis unreachable")
        self.store[key] = str(value)
        self.ttls[key] = ex
        return True

    async def scan(
        self, cursor: int = 0, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[str]]:
        if self.raise_on_scan:
            raise ConnectionError("redis unreachable")
        prefix = (match or "*").rstrip("*")
        return 0, [k for k in sorted(self.store) if k.startswith(prefix)]


async def _open_breaker(
    breaker: CircuitBreaker, *, exc: BaseException | None = None
) -> None:
    """Fail it up to its threshold, the way a failing dependency would."""

    async def _fail() -> None:
        raise exc or RuntimeError("upstream said no")

    for _ in range(breaker.failure_threshold):
        with pytest.raises(Exception):  # noqa: B017 — any raise is the point
            await breaker.call(_fail)


# The failure reason is a class, never a message (ADR 0012, ADR 0030)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("slow"), FAILURE_CLASS_TIMEOUT),
        # `asyncio.TimeoutError` is this same class since 3.11, which is why one arm covers
        # both: a call that ran out of time inside the loop and one that did outside it.
        (TimeoutError(), FAILURE_CLASS_TIMEOUT),
        (ConnectionError("refused"), FAILURE_CLASS_CONNECTION),
        (OSError("no route to host"), FAILURE_CLASS_CONNECTION),
        (RuntimeError("endpoint 3 returned 503"), FAILURE_CLASS_OTHER),
        (ValueError("bad payload"), FAILURE_CLASS_OTHER),
    ],
)
def test_a_failure_is_classified_into_the_closed_set(
    exc: BaseException, expected: str
) -> None:
    assert classify_failure(exc) == expected
    assert expected in FAILURE_CLASSES


def test_the_failure_classes_are_exactly_three() -> None:
    assert FAILURE_CLASSES == (
        FAILURE_CLASS_TIMEOUT,
        FAILURE_CLASS_CONNECTION,
        FAILURE_CLASS_OTHER,
    )


def test_no_exception_message_reaches_the_class() -> None:
    """The message can carry a job id, a URL, or the name of whatever injected the
    fault. None of that may reach the agent."""
    assert classify_failure(RuntimeError("job 7f3a at https://api.internal/x")) == (
        FAILURE_CLASS_OTHER
    )


# The breaker records a wall clock, and why it last failed


async def test_opening_stamps_a_wall_clock_and_a_reason_class() -> None:
    breaker = CircuitBreaker("wall-clock", failure_threshold=2, recovery_timeout=30.0)
    redis = _RedisStub()

    with patch("app.core.redis.get_redis_client", return_value=redis):
        await _open_breaker(breaker, exc=TimeoutError("slow"))

    assert breaker.state is CircuitState.OPEN
    assert breaker.last_state_change_at is not None
    assert (datetime.now(UTC) - breaker.last_state_change_at).total_seconds() < 5
    assert breaker.last_failure_reason_class == FAILURE_CLASS_TIMEOUT
    assert breaker.last_failure_at is not None


async def test_a_failure_below_the_threshold_moves_no_state_change_time() -> None:
    """Counting is not transitioning: the state change time must mean what it says."""
    breaker = CircuitBreaker("below", failure_threshold=3, recovery_timeout=30.0)
    redis = _RedisStub()

    async def _fail() -> None:
        raise ConnectionError("refused")

    with patch("app.core.redis.get_redis_client", return_value=redis):
        with pytest.raises(ConnectionError):
            await breaker.call(_fail)

    assert breaker.state is CircuitState.CLOSED
    assert breaker.last_state_change_at is None
    assert breaker.last_failure_reason_class == FAILURE_CLASS_CONNECTION


async def test_the_recovery_timeout_still_runs_on_the_monotonic_clock() -> None:
    """The wall clock is added beside the monotonic one, not instead of it: a duration
    measured on a clock that can be stepped is the bug this avoids."""
    breaker = CircuitBreaker("monotonic", failure_threshold=1, recovery_timeout=30.0)
    redis = _RedisStub()

    with patch("app.core.redis.get_redis_client", return_value=redis):
        await _open_breaker(breaker)

        async def _ok() -> str:
            return "ok"

        with pytest.raises(CircuitOpenError):
            await breaker.call(_ok)


# The record: written by one process, read by another


async def test_an_opened_breaker_is_readable_by_a_reader_with_an_empty_registry() -> None:
    """The H7 proof at unit level: nothing here shares the in-memory registry."""
    breaker = CircuitBreaker("bulk-api-sync", failure_threshold=2, recovery_timeout=30.0)
    redis = _RedisStub()

    with patch("app.core.redis.get_redis_client", return_value=redis):
        await _open_breaker(breaker, exc=ConnectionError("refused"))

    assert breaker_key_for("bulk-api-sync") in redis.store
    assert redis.ttls[breaker_key_for("bulk-api-sync")] == BREAKER_STATE_TTL_SECONDS

    records, unknown_reason = await read_breaker_states(_RedisStub(redis.store))

    assert unknown_reason is None
    assert len(records) == 1
    record = records[0]
    assert record.name == "bulk-api-sync"
    assert record.state == "open"
    assert record.failure_count == 2
    assert record.failure_threshold == 2
    assert record.last_failure_reason_class == FAILURE_CLASS_CONNECTION
    assert record.last_state_change_at is not None
    assert record.recorded_at is not None


async def test_the_key_sits_outside_the_lab_namespace() -> None:
    """A platform key, so the world reset does not sweep it (ADR 0028, ADR 0030)."""
    assert breaker_key_for("x").startswith(BREAKER_STATE_KEY_PREFIX)
    assert not breaker_key_for("x").startswith("chaos:")


async def test_a_publish_that_cannot_reach_the_store_does_not_fail_the_call() -> None:
    """A lost diagnostic must never become a failed call."""
    breaker = CircuitBreaker("fails-open", failure_threshold=1, recovery_timeout=30.0)
    redis = _RedisStub()
    redis.raise_on_set = True

    with patch("app.core.redis.get_redis_client", return_value=redis):
        await _open_breaker(breaker)

    assert breaker.state is CircuitState.OPEN


async def test_a_healthy_breaker_publishes_at_most_once_a_window() -> None:
    """Redis must not be in the hot path of every call — but the record still
    refreshes while traffic flows, or its age could not be read at all."""
    breaker = CircuitBreaker("throttled", failure_threshold=5, recovery_timeout=30.0)
    redis = _RedisStub()
    writes = 0

    async def _counted_set(key: str, value: Any, ex: int | None = None) -> bool:
        nonlocal writes
        writes += 1
        redis.store[key] = str(value)
        redis.ttls[key] = ex
        return True

    async def _ok() -> str:
        return "ok"

    with patch("app.core.redis.get_redis_client", return_value=redis), patch.object(
        redis, "set", _counted_set
    ):
        for _ in range(5):
            await breaker.call(_ok)

    assert writes == 1, "a successful call must not write a record every time"


async def test_a_state_change_always_publishes_even_inside_the_window() -> None:
    breaker = CircuitBreaker("change", failure_threshold=1, recovery_timeout=30.0)
    redis = _RedisStub()

    async def _ok() -> str:
        return "ok"

    with patch("app.core.redis.get_redis_client", return_value=redis):
        await breaker.call(_ok)
        assert "open" not in redis.store[breaker_key_for("change")]
        await _open_breaker(breaker)

    assert '"state": "open"' in redis.store[breaker_key_for("change")]


# Unknown is a reason, never an empty listing that reads as healthy


async def test_nothing_published_reads_as_unknown_with_a_reason() -> None:
    records, unknown_reason = await read_breaker_states(_RedisStub())

    assert records == ()
    assert unknown_reason == BREAKERS_UNKNOWN_NONE_PUBLISHED


async def test_an_unreachable_store_reads_as_unknown_with_a_different_reason() -> None:
    redis = _RedisStub()
    redis.raise_on_scan = True

    records, unknown_reason = await read_breaker_states(redis)

    assert records == ()
    assert unknown_reason == BREAKERS_UNKNOWN_UNREACHABLE


async def test_a_record_that_will_not_parse_reads_as_unknown() -> None:
    redis = _RedisStub({breaker_key_for("garbled"): "{not json"})

    records, unknown_reason = await read_breaker_states(redis)

    assert records == ()
    assert unknown_reason == BREAKERS_UNKNOWN_UNREADABLE


async def test_one_garbled_record_does_not_hide_the_others() -> None:
    redis = _RedisStub()
    await publish_breaker_state(
        redis,
        name="good",
        state="closed",
        failure_count=0,
        failure_threshold=3,
        recovery_timeout_s=30.0,
        last_state_change_at=None,
        last_failure_at=None,
        last_failure_reason_class=None,
    )
    redis.store[breaker_key_for("garbled")] = "{not json"

    records, unknown_reason = await read_breaker_states(redis)

    assert [r.name for r in records] == ["good"]
    assert unknown_reason is None


# The tool


def _ctx(redis: Any) -> ToolContext:
    return ToolContext(
        db=object(),
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


async def _call(redis: Any) -> GetCircuitBreakersOutput:
    return await get_circuit_breakers(GetCircuitBreakersInput(), _ctx(redis))


async def test_the_tool_reports_an_open_breaker_with_its_ages() -> None:
    redis = _RedisStub()
    now = datetime.now(UTC)
    await publish_breaker_state(
        redis,
        name="bulk-api-sync",
        state="open",
        failure_count=3,
        failure_threshold=3,
        recovery_timeout_s=30.0,
        last_state_change_at=now,
        last_failure_at=now,
        last_failure_reason_class=FAILURE_CLASS_TIMEOUT,
        now=now,
    )

    out = await _call(redis)

    assert out.total == 1
    assert out.unknown_reason is None
    breaker = out.breakers[0]
    assert breaker.name == "bulk-api-sync"
    assert breaker.state == "open"
    assert breaker.failure_count == 3
    assert breaker.failure_threshold == 3
    assert breaker.last_failure_reason_class == FAILURE_CLASS_TIMEOUT
    assert breaker.seconds_since_state_change is not None
    assert breaker.seconds_since_state_change >= 0.0
    assert breaker.reported_age_s >= 0.0


async def test_the_tool_reports_unknown_rather_than_an_empty_healthy_listing() -> None:
    redis = _RedisStub()
    redis.raise_on_scan = True

    out = await _call(redis)

    assert out.breakers == ()
    assert out.total == 0
    assert out.unknown_reason == BREAKERS_UNKNOWN_UNREACHABLE


async def test_a_closed_breaker_reads_as_closed_with_no_failure_class() -> None:
    """The healthy reading has to be sayable, or the tool only works when broken."""
    redis = _RedisStub()
    await publish_breaker_state(
        redis,
        name="quiet",
        state="closed",
        failure_count=0,
        failure_threshold=3,
        recovery_timeout_s=30.0,
        last_state_change_at=None,
        last_failure_at=None,
        last_failure_reason_class=None,
    )

    out = await _call(redis)

    assert out.breakers[0].state == "closed"
    assert out.breakers[0].failure_count == 0
    assert out.breakers[0].last_failure_reason_class is None
    assert out.breakers[0].seconds_since_state_change is None


async def test_breakers_come_back_in_a_stable_order() -> None:
    redis = _RedisStub()
    for name in ("zeta", "alpha"):
        await publish_breaker_state(
            redis,
            name=name,
            state="closed",
            failure_count=0,
            failure_threshold=3,
            recovery_timeout_s=30.0,
            last_state_change_at=None,
            last_failure_at=None,
            last_failure_reason_class=None,
        )

    out = await _call(redis)

    assert [b.name for b in out.breakers] == ["alpha", "zeta"]


# The description rules (CLAUDE.md "Tool descriptions")


def _description() -> str:
    definition = get_tool("get_circuit_breakers")
    assert definition is not None
    return definition.description


@pytest.mark.parametrize(
    "phrase",
    ["clock", "no arguments", "offset", "not a heartbeat"],
)
def test_the_description_states_clock_paging_and_what_an_age_is_not(
    phrase: str,
) -> None:
    assert phrase in _description().lower()


def test_the_description_says_an_absent_breaker_is_unknown_not_closed() -> None:
    text = _description().lower()

    assert "unknown" in text
    assert "absent" in text or "not listed" in text


def test_the_tool_is_a_telemetry_read() -> None:
    definition = get_tool("get_circuit_breakers")
    assert definition is not None
    assert definition.required_scope is Scope.TELEMETRY_READ
    assert definition.is_chaos is False
    assert definition.is_idempotent is False
