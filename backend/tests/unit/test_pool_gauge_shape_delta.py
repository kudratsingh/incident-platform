"""The WO-R3-289 contract delta, in one place, read off the registry.

A platform tool change is a rebless delta: the commander re-pins the image, regenerates its
snapshot and ledgers every field. Spelling the delta out as a test means the ledger note can
be read off a file rather than reconstructed from a diff, and a second packet cannot ride in
on this one (the same discipline as `test_phase8_read_tool_shape_deltas.py`).

This batch moves no count at all. No tool is added, no tool is removed, no scope is added and
no existing field changes — `get_postgres_health` gains two output fields and one `$defs`
entry, and that is the whole surface delta.
"""

from __future__ import annotations

import pathlib

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
from app.core.pool_state import POOL_STATE_KEY_PREFIX
from app.core.scopes import Scope
from app.mcp.registry import ToolDefinition, list_tools

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: `get_postgres_health`'s output after this order. Everything above the blank line is
#: WO-R3-217's shape, byte for byte; the two below it are this order's delta.
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
    #
    "pools",
    "pool_gauges_unknown_reason",
}

#: The new `$defs` entry. One row per process that has published a pool reading.
POOL_GAUGE_READING_FIELDS = {
    "process",
    "size",
    "checked_out",
    "overflow",
    "max_overflow",
    "wait_timeouts_1m",
    "written_at",
    "reported_age_s",
}

#: The read tier does not move: this order adds no tool.
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


def _tool(name: str) -> ToolDefinition:
    definition = next((t for t in list_tools() if t.name == name), None)
    assert definition is not None, f"{name} is not registered"
    return definition


def _claude_md() -> str:
    return (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_surface_does_not_grow() -> None:
    """No tool is added, so `tools/list` gains no name and the tool-level delta is
    empty — the whole delta is inside one output model."""
    read = sorted(
        t.name
        for t in list_tools()
        if t.required_scope in {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}
    )

    assert read == READ_TIER_AFTER
    assert len(list_tools()) == 25, sorted(t.name for t in list_tools())


def test_the_shape_deltas_are_exactly_these() -> None:
    schema = _tool("get_postgres_health").output_json_schema()

    assert set(schema["properties"]) == POSTGRES_HEALTH_FIELDS
    assert set(schema["$defs"]["PoolGaugeReading"]["properties"]) == (
        POOL_GAUGE_READING_FIELDS
    )


def test_the_input_is_still_empty_and_closed() -> None:
    """The delta is output-only: nothing new to pass, nothing new to filter on."""
    schema = _tool("get_postgres_health").input_json_schema()

    assert schema.get("properties", {}) == {}
    assert schema.get("additionalProperties") is False


def test_no_class_docstring_reaches_the_pinned_schema() -> None:
    """plat #210: Pydantic copies a model's class docstring into the schema's top-level
    `description`, and this schema is pinned in the commander's snapshot."""
    schema = _tool("get_postgres_health").output_json_schema()

    assert "description" not in schema
    for defined in schema.get("$defs", {}).values():
        assert "description" not in defined


def test_the_existing_pool_fields_are_untouched() -> None:
    """The five flat readings keep saying they describe the answering process only. The
    new group is beside them, not instead of them — a caller written against WO-R3-217
    reads the same numbers it always did."""
    schema = _tool("get_postgres_health").output_json_schema()

    for field in ("pool_checked_out", "pool_overflow", "pool_wait_timeouts_1m"):
        described = schema["properties"][field].get("description", "").lower()
        assert "answered" in described, f"{field} no longer says whose pool it is"


def test_the_group_says_an_absent_process_is_not_a_healthy_one() -> None:
    """The whole point of the null-with-reason field: an empty `pools` must never read
    as every pool being fine."""
    schema = _tool("get_postgres_health").output_json_schema()
    described = schema["properties"]["pool_gauges_unknown_reason"].get("description", "")

    assert "null" in described.lower()

    tool_text = _tool("get_postgres_health").description.lower()
    assert "pool_gauges_unknown_reason" in tool_text
    assert "pools" in tool_text


def test_adr_0033_exists_and_is_indexed() -> None:
    adr = (
        _REPO_ROOT
        / "docs"
        / "ADR"
        / "0033-each-process-publishes-its-own-pool-gauge.md"
    )
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")

    assert adr.is_file(), "ADR 0033 is missing"
    assert adr.name in index, "ADR 0033 is not in docs/ADR/README.md"


def test_the_new_redis_key_is_in_the_catalogue() -> None:
    """Every key this platform writes is in `docs/REDIS.md` with its writer, reader and
    TTL — a key nobody catalogued is a key nobody knows survives a reset."""
    catalogue = (_REPO_ROOT / "docs" / "REDIS.md").read_text(encoding="utf-8")

    assert POOL_STATE_KEY_PREFIX in catalogue


def test_the_rebless_ledger_names_this_delta() -> None:
    """The commander re-pins off this paragraph. A field added without a note there is a
    surprise at snapshot time."""
    ledger = _claude_md()

    assert "WO-R3-289" in ledger
    assert "pool_gauges_unknown_reason" in ledger
