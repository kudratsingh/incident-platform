"""The WO-R3-217 contract delta, in one place, read off the registry.

A platform tool change is a rebless delta: the commander re-pins the image, regenerates its
snapshot and ledgers every field. Spelling the delta out as a test means the ledger note can
be read off a file rather than reconstructed from a diff, and a second packet cannot ride in
on this one's count (the same discipline as `test_stranded_chain_and_lab_pause.py`).
"""

from __future__ import annotations

import pathlib

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.breaker_state import BREAKER_STATE_KEY_PREFIX
from app.core.scopes import Scope
from app.mcp.registry import ToolDefinition, list_tools

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Spelled out rather than counted: 14 → 16.
READ_TIER_AFTER = [
    "get_cache_key_info",
    "get_circuit_breakers",
    "get_consumer_lag",
    "get_dag_state",
    "get_deploy_history",
    "get_incident",
    "get_outbox_status",
    "get_postgres_health",
    "get_redis_health",
    "get_slo_status",
    "get_trace",
    "list_active_alerts",
    "list_audit_events",
    "list_dlq_messages",
    "list_incidents",
    "search_traces",
]

#: `get_postgres_health`'s output after this order. The first five existed; the twelve below
#: them are the delta. The last two are WO-R3-289's, listed here because this assertion is an
#: exact set — the reasoning for them belongs to `test_pool_gauge_shape_delta.py`, which owns
#: that delta and pins the `$defs` entry underneath `pools`.
POSTGRES_HEALTH_FIELDS = {
    "ok",
    "ping_latency_ms",
    "active_connections",
    "dialect",
    "error",
    "pool_size",
    "pool_checked_out",
    "pool_overflow",
    "pool_max_overflow",
    "pool_wait_timeouts_1m",
    "pool_stats_unknown_reason",
    "longest_active_query_ms",
    "active_queries_over_slow_threshold",
    "slow_query_threshold_ms",
    "p95_query_ms_1m",
    "slow_query_count_1m",
    "query_stats_unknown_reason",
    # WO-R3-289 (ADR 0033), not this order's.
    "pools",
    "pool_gauges_unknown_reason",
}

SLO_STATUS_FIELDS = {
    "measured_at",
    "objectives",
    "total",
    "fast_burn_threshold",
}

SLO_OBJECTIVE_FIELDS = {
    "id",
    "name",
    "description",
    "target",
    "window_hours",
    "total",
    "failed",
    "current_success_rate",
    "budget_remaining_pct",
    "burn_rate",
    "healthy",
    "fast_burn",
}

CIRCUIT_BREAKERS_FIELDS = {
    "measured_at",
    "breakers",
    "total",
    "unknown_reason",
}

CIRCUIT_BREAKER_READING_FIELDS = {
    "name",
    "state",
    "failure_count",
    "failure_threshold",
    "recovery_timeout_s",
    "last_state_change_at",
    "seconds_since_state_change",
    "last_failure_at",
    "last_failure_reason_class",
    "recorded_at",
    "reported_age_s",
}


def _tool(name: str) -> ToolDefinition:
    definition = next((t for t in list_tools() if t.name == name), None)
    assert definition is not None, f"{name} is not registered"
    return definition


def _claude_md() -> str:
    return (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_read_tier_grows_by_exactly_two() -> None:
    read = sorted(
        t.name
        for t in list_tools()
        if t.required_scope in {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}
    )

    assert read == READ_TIER_AFTER
    # 23 → 25 with WO-R3-312's two `agent_runs:write` tools, which are not reads and so
    # leave the list above alone (ADR 0035).
    assert len(list_tools()) == 25, sorted(t.name for t in list_tools())


def test_neither_new_tool_takes_an_argument() -> None:
    """Both are whole-platform readings. `extra="forbid"` plus an empty schema is the
    promise that there is nothing to filter and nothing to page."""
    for name in ("get_slo_status", "get_circuit_breakers"):
        schema = _tool(name).input_json_schema()
        assert schema.get("properties", {}) == {}
        assert schema.get("additionalProperties") is False


def test_the_shape_deltas_are_exactly_these() -> None:
    assert set(
        _tool("get_postgres_health").output_json_schema()["properties"]
    ) == POSTGRES_HEALTH_FIELDS

    slo = _tool("get_slo_status").output_json_schema()
    assert set(slo["properties"]) == SLO_STATUS_FIELDS
    assert set(slo["$defs"]["SloObjective"]["properties"]) == SLO_OBJECTIVE_FIELDS

    breakers = _tool("get_circuit_breakers").output_json_schema()
    assert set(breakers["properties"]) == CIRCUIT_BREAKERS_FIELDS
    assert set(
        breakers["$defs"]["CircuitBreakerReading"]["properties"]
    ) == CIRCUIT_BREAKER_READING_FIELDS


@pytest.mark.parametrize(
    "name", ["get_postgres_health", "get_slo_status", "get_circuit_breakers"]
)
def test_no_class_docstring_reaches_a_pinned_schema(name: str) -> None:
    """plat #210: Pydantic copies a model's class docstring into the schema's top-level
    `description`, and these schemas are pinned in the commander's snapshot."""
    schema = _tool(name).output_json_schema()

    assert "description" not in schema
    for defined in schema.get("$defs", {}).values():
        assert "description" not in defined


@pytest.mark.parametrize(
    "field", ["p95_query_ms_1m", "slow_query_count_1m", "pool_wait_timeouts_1m"]
)
def test_every_promised_field_is_present_even_when_it_cannot_be_measured(
    field: str,
) -> None:
    """The plan named these three. Two are null with a reason in this release and one is
    counted per process — all three are in the contract, so a caller written against the
    plan finds them rather than a missing key."""
    assert field in _tool("get_postgres_health").output_json_schema()["properties"]


def test_adr_0030_exists_and_is_indexed() -> None:
    adr = (
        _REPO_ROOT
        / "docs"
        / "ADR"
        / "0030-breaker-state-is-published-and-a-reading-is-never-invented.md"
    )
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")

    assert adr.is_file(), "ADR 0030 is missing"
    assert adr.name in index, "ADR 0030 is not in docs/ADR/README.md"


def test_the_new_redis_key_is_in_the_catalogue() -> None:
    """Every key this platform writes is in `docs/REDIS.md` with its writer, reader and
    TTL — a key nobody catalogued is a key nobody knows survives a reset."""
    catalogue = (_REPO_ROOT / "docs" / "REDIS.md").read_text(encoding="utf-8")

    assert BREAKER_STATE_KEY_PREFIX in catalogue


def test_the_rebless_ledger_names_this_delta() -> None:
    """The commander re-pins off this paragraph. A tool added without a note there is a
    surprise at snapshot time."""
    ledger = _claude_md()

    assert "get_circuit_breakers" in ledger
    assert "get_slo_status" in ledger
    assert "WO-R3-217" in ledger
