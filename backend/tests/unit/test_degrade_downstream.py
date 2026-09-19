"""`degrade_downstream` — the breaker opens because the dependency failed (WO-R3-220, WP-8.4).

The hook sets one flag; `process_bulk_api_sync` reads it once per job and the *shipped* breaker
does the rest, so these tests drive the real breaker rather than a copy of it. `fail` opens it and
fails the job; `slow` does neither. The half-open cycle is asserted because a scenario's
precondition has to tolerate it (`app/core/circuit_breaker.py`, ADR 0031).
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import app.mcp.tools  # noqa: F401  — import fires every @tool decorator
import pytest
from app.config import Settings
from app.core.circuit_breaker import CircuitState
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.chaos import BlastRadius
from app.mcp.registry import (
    ToolContext,
    _restore_for_tests,
    _snapshot_for_tests,
    get_tool,
    list_tools,
)
from app.workers import async_tasks
from pydantic import ValidationError


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """Chaos-gated, so reload under patched settings and restore (ADR 0008 gate 1)."""
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools.chaos import degrade_downstream as module

    snapshot = _snapshot_for_tests()
    try:
        with patch.object(
            chaos_mod,
            "get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            importlib.reload(module)
        yield
    finally:
        _restore_for_tests(snapshot)
        importlib.reload(module)


@pytest.fixture(autouse=True)
def fresh_breaker() -> Iterator[None]:
    """The breaker is a process-wide singleton (`_registry`), so each test starts it closed."""

    def _reset() -> None:
        breaker = async_tasks.bulk_api_breaker()
        breaker._state = CircuitState.CLOSED
        breaker._failure_count = 0
        breaker._opened_at = None
        breaker._probe_in_flight = False

    _reset()
    yield
    _reset()


class _Redis:
    def __init__(self, value: str | None = None, raises: bool = False) -> None:
        self.value = value
        self.raises = raises
        self.sets: list[tuple[str, Any, int | None]] = []

    async def get(self, _key: str) -> str | None:
        if self.raises:
            raise RuntimeError("redis is down")
        return self.value

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self.sets.append((key, value, ex))
        self.value = str(value)
        return True


async def _publish(_pct: int, _msg: str) -> None:
    return None


def _degraded(value: str) -> Any:
    """Run the processor with the flag set to `value`, chaos on."""
    return patch.multiple(
        async_tasks,
        get_settings=lambda: Settings(chaos_enabled=True, environment="test"),
        get_redis_client=lambda: _Redis(value=value),
    )


# Gate 1, scope, blast radius


def test_the_hook_is_absent_from_the_registry_when_chaos_is_disabled() -> None:
    from app.mcp.tools.chaos import degrade_downstream  # noqa: F401

    assert "degrade_downstream" not in {t.name for t in list_tools()}


def test_the_hook_requires_chaos_invoke_and_declares_a_blast_radius(
    chaos_registered: None,
) -> None:
    """`single_service`: one simulated third-party dependency, not the platform around it."""
    spec = get_tool("degrade_downstream")
    assert spec is not None
    assert spec.required_scope == Scope.CHAOS_INVOKE
    assert spec.is_chaos is True
    assert spec.description.startswith(
        f"[chaos: {BlastRadius.SINGLE_SERVICE.value}] "
    )


def test_the_modes_are_a_closed_pair(chaos_registered: None) -> None:
    """A third mode would be a third world; anything else is refused before the handler runs."""
    spec = get_tool("degrade_downstream")
    assert spec is not None
    schema = spec.input_model.model_json_schema()
    assert schema["properties"]["mode"]["enum"] == ["fail", "slow"]
    with pytest.raises(ValidationError):
        spec.input_model(mode="flaky")


def test_the_dials_are_bounded(chaos_registered: None) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    schema = spec.input_model.model_json_schema()
    assert schema["properties"]["delay_ms"]["maximum"] == 30_000
    assert schema["properties"]["ttl_seconds"]["maximum"] == 3600
    for kwargs in ({"delay_ms": 30_001}, {"ttl_seconds": 0}, {"nope": 1}):
        with pytest.raises(ValidationError):
            spec.input_model(**kwargs)


def test_the_defaults_are_the_world_the_packet_wants(chaos_registered: None) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    inp = spec.input_model()
    # The literal in the schema and the constant the processor compares against are one value.
    assert inp.mode == "fail" == async_tasks.DEGRADE_FAIL
    assert inp.ttl_seconds == 300


# The flag, and what the output says about the breaker


async def _invoke(spec: Any, redis: _Redis, **kwargs: Any) -> Any:
    ctx = ToolContext(
        db=None,  # type: ignore[arg-type]
        redis=redis,
        principal=Principal(
            kind="service_account",
            tenant_id=uuid.uuid4(),
            scopes=frozenset({Scope.CHAOS_INVOKE.value}),
        ),
    )
    return await spec.handler(spec.input_model(**kwargs), ctx)


async def test_the_hook_writes_the_mode_and_the_delay(chaos_registered: None) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    redis = _Redis()
    out = await _invoke(spec, redis, mode="slow", delay_ms=1500, ttl_seconds=60)

    assert redis.sets == [(async_tasks.DOWNSTREAM_FLAG_KEY, "slow:1500", 60)]
    assert out.mode == "slow"
    assert out.delay_ms == 1500
    assert out.flag_key == async_tasks.downstream_flag_key()
    assert out.accepted is True


async def test_the_fail_mode_reports_no_delay(chaos_registered: None) -> None:
    """`delay_ms` is meaningless under `fail`, and a number the tool ignores is a number a caller
    will believe."""
    spec = get_tool("degrade_downstream")
    assert spec is not None
    out = await _invoke(spec, _Redis(), mode="fail", delay_ms=9999)
    assert out.delay_ms == 0


async def test_the_output_reads_the_breakers_own_numbers(
    chaos_registered: None,
) -> None:
    """Not a copy: the threshold and the recovery window come off the registered breaker, so a
    change there cannot leave this tool describing the old one."""
    spec = get_tool("degrade_downstream")
    assert spec is not None
    breaker = async_tasks.bulk_api_breaker()
    out = await _invoke(spec, _Redis())
    assert out.dependency == breaker.name == "bulk-api-sync"
    assert out.failure_threshold == breaker.failure_threshold == 3
    assert out.recovery_timeout_seconds == breaker.recovery_timeout == 30.0


def test_the_flag_lives_under_the_chaos_namespace() -> None:
    assert fnmatch.fnmatch(async_tasks.downstream_flag_key(), "chaos:*")


def test_make_eval_reset_sweeps_the_flag() -> None:
    """Teardown beyond the TTL, asserted against the reset script's real pattern tuple: the
    breaker then closes on the first probe that succeeds."""
    from tests.unit.test_eval_reset import _reset_module

    patterns = _reset_module()._CHAOS_KEY_PATTERNS
    key = async_tasks.downstream_flag_key()
    assert any(fnmatch.fnmatch(key, p) for p in patterns)


# Reading the flag


async def test_the_flag_is_not_read_at_all_when_chaos_is_disabled() -> None:
    """Gate 1 on a per-job path: production must not pay a Redis round-trip per job."""
    client = AsyncMock()
    with patch.object(
        async_tasks,
        "get_settings",
        return_value=Settings(chaos_enabled=False, environment="test"),
    ), patch.object(async_tasks, "get_redis_client", return_value=client):
        assert await async_tasks.read_degradation() is None
    client.get.assert_not_awaited()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("fail:0", ("fail", 0)),
        ("fail", ("fail", 0)),
        (b"slow:1500", ("slow", 1500)),
        ("slow:not-a-number", ("slow", 0)),
        ("wedged:1", None),
        ("", None),
    ],
)
def test_the_flag_parses_to_a_mode_and_a_delay(
    raw: Any, expected: tuple[str, int] | None
) -> None:
    parsed = async_tasks.parse_degradation(raw)
    if expected is None:
        assert parsed is None
    else:
        assert parsed is not None
        assert (parsed.mode, parsed.delay_ms) == expected


async def test_the_flag_read_fails_open() -> None:
    with patch.object(
        async_tasks,
        "get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ), patch.object(
        async_tasks, "get_redis_client", return_value=_Redis(raises=True)
    ):
        assert await async_tasks.read_degradation() is None


# What the fault actually does to the shipped breaker


async def test_three_failed_endpoints_open_the_breaker() -> None:
    """The whole point of targeting something that already exists: the breaker's own threshold
    does the work, so `get_circuit_breakers` has a real state to report."""
    breaker = async_tasks.bulk_api_breaker()
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": 3}, _publish
            )
    assert breaker.state is CircuitState.OPEN


async def test_a_job_whose_every_endpoint_failed_is_itself_failed() -> None:
    """Without this the fault has no job-level observable at all: the processor's per-endpoint
    error counts do not reach any operational tool, so the job completed and nothing showed."""
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError) as exc:
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": 1}, _publish
            )
    rendered = str(exc.value)
    assert "0 of 1 endpoints" in rendered
    # The message reaches `jobs.error_message`, which the agent reads (ADR 0012 rule 1).
    for word in ("chaos", "fixture", "scenario", "eval", "seed", "harness"):
        assert word not in rendered.lower()


async def test_one_failed_endpoint_does_not_open_the_breaker() -> None:
    """Threshold 3, asserted from the outside: a single-endpoint job fails without tripping it."""
    breaker = async_tasks.bulk_api_breaker()
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": 1}, _publish
            )
    assert breaker.state is CircuitState.CLOSED


async def test_the_slow_mode_keeps_the_breaker_closed() -> None:
    """`slow` moves the job's duration only. A scenario that needs an open breaker must ask for
    `fail`, and the description says so."""
    breaker = async_tasks.bulk_api_breaker()
    started = time.monotonic()
    with _degraded("slow:50"):
        result = await async_tasks.process_bulk_api_sync(
            {"endpoint_count": 4}, _publish
        )
    assert breaker.state is CircuitState.CLOSED
    assert result["endpoints_synced"] == 4
    assert result["errors"] == 0
    assert time.monotonic() - started >= 0.05


async def test_an_absent_flag_leaves_the_organic_path_alone() -> None:
    """The gate again, from the other side: with no flag the processor is the one that shipped."""
    breaker = async_tasks.bulk_api_breaker()
    with patch.multiple(
        async_tasks,
        get_settings=lambda: Settings(chaos_enabled=True, environment="test"),
        get_redis_client=lambda: _Redis(value=None),
    ), patch.object(async_tasks.random, "random", return_value=0.99), patch.object(
        async_tasks.random, "uniform", return_value=0.0
    ):
        result = await async_tasks.process_bulk_api_sync(
            {"endpoint_count": 3}, _publish
        )
    assert result["endpoints_synced"] == 3
    assert breaker.state is CircuitState.CLOSED


async def test_the_open_breaker_cycles_through_half_open_rather_than_staying_open() -> None:
    """The precondition a scenario must tolerate: after the recovery window one probe is admitted,
    it fails while the flag is set, and the breaker re-opens. A reader sampling once can legally
    see `half_open`, so a precondition has to poll across the window (WO-R3-220's test
    requirement; the real window is 30 s, shortened here)."""
    breaker = async_tasks.bulk_api_breaker()
    with _degraded("fail:0"), patch.object(breaker, "recovery_timeout", 0.05):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync({"endpoint_count": 3}, _publish)
        assert breaker.state is CircuitState.OPEN
        assert breaker._failure_count == 3

        # Inside the window the call is rejected without reaching the dependency, and the
        # rejection is not a probe outcome — the failure count does not move.
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync({"endpoint_count": 1}, _publish)
        assert breaker.state is CircuitState.OPEN
        assert breaker._failure_count == 3

        # Past it, one probe is admitted, fails, and re-opens the breaker.
        await asyncio.sleep(0.06)
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync({"endpoint_count": 1}, _publish)
        assert breaker.state is CircuitState.OPEN
        assert breaker._failure_count == 4


async def test_a_rejected_call_is_reported_as_an_error_not_a_success() -> None:
    """While the breaker is open the endpoints are never reached, so every result is an error and
    the job still fails — the fault persists across the whole window, not just the opening job."""
    breaker = async_tasks.bulk_api_breaker()
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync({"endpoint_count": 3}, _publish)
        assert breaker.state is CircuitState.OPEN
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync({"endpoint_count": 2}, _publish)


async def test_the_fan_out_ceiling_still_holds_under_the_flag() -> None:
    """`MAX_ENDPOINT_COUNT` is load-bearing, not belt-and-braces (async_tasks.py:25-31): a
    degraded job must not be a way around it."""
    assert async_tasks.MAX_ENDPOINT_COUNT == 100
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError) as failure:
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": 10_000}, _publish
            )
    assert f"0 of {async_tasks.MAX_ENDPOINT_COUNT} endpoints" in str(failure.value)


# What the description has to say


def test_the_description_says_the_reader_must_poll_the_window(
    chaos_registered: None,
) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    text = spec.description
    assert "poll across a recovery window rather than sample once" in text
    assert "either `open` or `half_open` is a correct" in text


def test_the_description_says_only_fail_opens_the_breaker(
    chaos_registered: None,
) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    assert "Under `slow` nothing fails and the breaker stays closed." in (
        spec.description
    )


def test_the_description_says_the_job_fails_and_how_far_it_goes(
    chaos_registered: None,
) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    text = spec.description
    assert "is itself failed" in text
    assert "retries first, then the dead-letter queue" in text


def test_the_description_says_what_it_does_not_touch(
    chaos_registered: None,
) -> None:
    """The A3 discrimination: an open breaker with the database and Redis healthy."""
    spec = get_tool("degrade_downstream")
    assert spec is not None
    assert "the database and Redis are untouched" in spec.description


def test_the_description_says_it_self_cleans(chaos_registered: None) -> None:
    spec = get_tool("degrade_downstream")
    assert spec is not None
    text = spec.description
    assert "Self-cleans when the flag expires" in text
    assert "environment reset clears it" in text
