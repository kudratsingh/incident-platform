"""The WO-R3-312 contract delta, in one place, read off the registry.

A platform tool change is a rebless delta: the commander re-pins the image, regenerates
its snapshot and ledgers every field. Spelling the delta out as a test means the ledger
note can be read off a file rather than reconstructed from a diff, and a second packet
cannot ride in on this one's count (the same discipline as
`test_phase8_read_tool_shape_deltas.py`).

The one wire-name surprise this batch carries is pinned here too: the input field is
`run_label`, and it lands in the `agent_runs.scenario` column. ADR 0012's screen bans
the lab's own vocabulary from any non-chaos tool's `tools/list` surface, and the honest
fix for a description that trips it is to reword the description, never to weaken the
screen — so the column keeps the operator's word and the wire does not.
"""

from __future__ import annotations

import pathlib

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.scopes import ALL_SCOPES, Scope
from app.mcp.registry import ToolDefinition, list_tools
from app.services.operator_audit import AGENT_RUN_REPORTED_ACTION

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: Spelled out rather than counted. Both are new; nothing existing moves.
COMMANDER_TOOLS_AFTER = ["report_agent_briefing", "report_agent_run"]

#: This order's fields are above the blank line; WO-R3-328 widened the same two models
#: and pins its own delta in `test_run_record_shape_delta.py`. Listed here as well
#: because this test asserts the EXACT set — a field this file does not know about is
#: what makes a re-pin surprising, whichever order added it.
REPORT_AGENT_RUN_INPUT_FIELDS = {
    "run_id",
    "state",
    "at",
    "alert_id",
    "run_label",
    "current_hypothesis",
    "last_step",
    #
    "hypotheses",
    "plan",
    "verification",
    "step",
    "budget",
}

REPORT_AGENT_RUN_OUTPUT_FIELDS = {
    "run_id",
    "state",
    "created",
    "phase_appended",
    "phase_count",
    "started_at",
    "updated_at",
    "finished_at",
    "accepted",
    #
    "steps_count",
    "steps_dropped",
}

HYPOTHESIS_FIELDS = {"name", "category", "confidence"}
STEP_FIELDS = {"kind", "tool", "at"}

REPORT_AGENT_BRIEFING_INPUT_FIELDS = {"run_id", "briefing", "prose", "at"}
REPORT_AGENT_BRIEFING_OUTPUT_FIELDS = {
    "run_id",
    "state",
    "recorded_at",
    "finished_at",
    "accepted",
}

#: The state vocabulary the wire advertises, in order.
REPORTABLE_STATES = [
    "triage",
    "investigating",
    "planning",
    "awaiting_approval",
    "remediating",
    "verifying",
    "resolved",
    "escalated",
    "failed",
]

#: The refusal codes that reach the commander's client. An unknown `error_code` is
#: bucketed there as a transport fault (R2-16), so a new one belongs in the ledger.
NEW_REFUSAL_CODES = [
    "agent_run_already_finished",
    "agent_run_briefing_already_recorded",
    "agent_run_not_found",
]


def _tool(name: str) -> ToolDefinition:
    definition = next((t for t in list_tools() if t.name == name), None)
    assert definition is not None, f"{name} is not registered"
    return definition


def _claude_md() -> str:
    return (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_tool_surface_grows_by_exactly_two() -> None:
    """23 → 25 with chaos off, 38 → 40 with `CHAOS_ENABLED=true`. The chaos-enabled
    figure is asserted in the chaos packets' own count tests; this one is the tier that
    is registered unconditionally."""
    assert sorted(t.name for t in list_tools() if t.is_commander) == (
        COMMANDER_TOOLS_AFTER
    )
    assert len(list_tools()) == 25, sorted(t.name for t in list_tools())


def test_the_read_tier_does_not_grow() -> None:
    """The point of the packet: it adds a write surface and no read surface at all."""
    read = sorted(
        t.name
        for t in list_tools()
        if t.required_scope in {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}
    )

    assert len(read) == 16, read
    for name in COMMANDER_TOOLS_AFTER:
        assert name not in read


def test_the_shape_deltas_are_exactly_these() -> None:
    """Where the ledger's field list is pinned: a shape change it does not mention is
    what makes a re-pin surprising."""
    run = _tool("report_agent_run")
    assert set(run.input_model.model_fields) == REPORT_AGENT_RUN_INPUT_FIELDS
    assert set(run.output_model.model_fields) == REPORT_AGENT_RUN_OUTPUT_FIELDS

    run_in = run.input_json_schema()
    assert set(run_in["$defs"]["HypothesisReport"]["properties"]) == HYPOTHESIS_FIELDS
    assert set(run_in["$defs"]["StepReport"]["properties"]) == STEP_FIELDS

    briefing = _tool("report_agent_briefing")
    assert (
        set(briefing.input_model.model_fields) == REPORT_AGENT_BRIEFING_INPUT_FIELDS
    )
    assert (
        set(briefing.output_model.model_fields) == REPORT_AGENT_BRIEFING_OUTPUT_FIELDS
    )


def test_nothing_existing_moves() -> None:
    """The batch is purely additive on the wire: no other tool's name, scope or
    idempotency flag changes, so the rest of the snapshot diffs as zero."""
    unchanged = {
        t.name: (
            t.required_scope.value if t.required_scope else None,
            t.is_idempotent,
            t.is_chaos,
        )
        for t in list_tools()
        if not t.is_commander
    }

    assert len(unchanged) == 23
    assert "report_agent_run" not in unchanged
    # The read tier's scopes are what a re-pin compares first.
    assert unchanged["get_consumer_lag"] == ("telemetry:read", False, False)
    assert unchanged["get_circuit_breakers"] == ("telemetry:read", False, False)
    assert unchanged["restart_consumer_group"] == ("actions:execute", True, False)


def test_the_state_vocabulary_is_closed_and_advertised() -> None:
    """A closed enum in the schema, so a caller sees the whole vocabulary rather than
    discovering it by refusal."""
    schema = _tool("report_agent_run").input_json_schema()

    assert schema["properties"]["state"]["enum"] == REPORTABLE_STATES


def test_the_wire_name_for_the_run_label_is_not_the_column_name() -> None:
    """The one surprise in the batch, pinned so nobody 'fixes' it back.

    `run_label` on the wire, `agent_runs.scenario` in the database. ADR 0012's screen
    bans lab vocabulary from a non-chaos tool's `tools/list` surface, and the word for
    a named rehearsal is in that vocabulary; the operator-facing REST shape, which the
    agent never reads, keeps it.
    """
    from app.models.agent_run import AgentRun
    from app.schemas.agent_run import AgentRunResponse

    assert "run_label" in _tool("report_agent_run").input_model.model_fields
    assert "scenario" in AgentRun.__table__.columns
    assert "scenario" in AgentRunResponse.model_fields


def test_neither_tool_takes_an_idempotency_key() -> None:
    """The upsert is the idempotency: a repeat report of the same state is a no-op by
    construction, so a key would add a wedge-able claim without adding a guarantee."""
    for name in COMMANDER_TOOLS_AFTER:
        definition = _tool(name)
        assert definition.is_idempotent is False
        assert "idempotency_key" not in definition.input_model.model_fields


def test_both_inputs_forbid_extra_fields() -> None:
    """`extra="forbid"`, as every other input model does: a typo'd field name is a
    refusal rather than a silently dropped report."""
    for name in COMMANDER_TOOLS_AFTER:
        assert _tool(name).input_json_schema().get("additionalProperties") is False


def test_the_briefing_body_is_deliberately_unvalidated() -> None:
    """The write-up is the caller's shape. Pinning it here would couple the two repos'
    release cycles for no gain — the platform stores it and shows it."""
    schema = _tool("report_agent_briefing").input_json_schema()

    assert schema["properties"]["briefing"]["type"] == "object"
    assert "properties" not in schema["properties"]["briefing"]


@pytest.mark.parametrize("name", COMMANDER_TOOLS_AFTER)
def test_no_class_docstring_reaches_a_pinned_schema(name: str) -> None:
    """plat #210: Pydantic copies a model's class docstring into the schema's
    top-level `description`, and these schemas are pinned in the commander's
    snapshot."""
    for schema in (
        _tool(name).input_json_schema(),
        _tool(name).output_json_schema(),
    ):
        assert "description" not in schema
        for defined in schema.get("$defs", {}).values():
            assert "description" not in defined


def test_the_new_scope_is_exactly_one_new_scope() -> None:
    assert len(ALL_SCOPES) == 6
    assert Scope.AGENT_RUNS_WRITE.value == "agent_runs:write"


def test_the_new_refusal_codes_are_exactly_these() -> None:
    from app.services import agent_run as module

    codes = sorted(
        obj.error_code
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Exception)
        and obj.__module__ == module.__name__
    )

    assert codes == NEW_REFUSAL_CODES


def test_the_rebless_ledger_names_this_delta() -> None:
    """The commander re-pins off this paragraph. A tool added without a note there is
    a surprise at snapshot time."""
    ledger = _claude_md()

    assert "report_agent_run" in ledger
    assert "report_agent_briefing" in ledger
    assert "WO-R3-312" in ledger
    assert "agent_runs:write" in ledger
    assert AGENT_RUN_REPORTED_ACTION in ledger
    assert "run_label" in ledger


def test_the_scope_table_in_claude_md_carries_the_new_scope() -> None:
    """The scope table is the living index of a fixed enum; a sixth member that is not
    in it makes the table wrong rather than incomplete."""
    text = _claude_md()

    assert "| `agent_runs:write` |" in text


def test_adr_0035_exists_and_is_indexed() -> None:
    adr = (
        _REPO_ROOT
        / "docs"
        / "ADR"
        / "0035-the-agent-reports-its-run-and-cannot-read-it-back.md"
    )
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")

    assert adr.is_file(), "ADR 0035 is missing"
    assert adr.name in index, "ADR 0035 is not in docs/ADR/README.md"


def test_the_new_table_is_in_the_data_model_doc() -> None:
    """Every table is documented column by column with a one-line why (CLAUDE.md's own
    documentation map says so)."""
    data_model = (_REPO_ROOT / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")

    assert "agent_runs" in data_model
    assert "phase_history" in data_model


def test_the_openapi_delta_is_documented() -> None:
    """The four new operator reads and the widened job shape are a REST contract
    change; the console is written against the note, not against the diff. Documented
    in ARCHITECTURE.md rather than a new file — that doc already owns the HTTP surface
    and the auth matrix, and a second one would drift from it."""
    architecture = (_REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    for route in (
        "/admin/agent-runs",
        "/admin/consumer-lag",
        "/admin/circuit-breakers",
        "/admin/alerts",
    ):
        assert route in architecture, route
    assert "dead_lettered_at" in architecture
    # Every new field says what its null means, which is the rule the order set.
    assert "lag_unknown_reason" in architecture
