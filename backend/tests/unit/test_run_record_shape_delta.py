"""The WO-R3-328 contract delta, in one place, read off the registry.

A platform tool change is a rebless delta: the commander re-pins the image, regenerates
its snapshot and ledgers every field. Spelling the delta out as a test means the ledger
note can be read off a file rather than reconstructed from a diff, and a second packet
cannot ride in on this one (the same discipline as `test_agent_run_contract.py`, which
pins WO-R3-312's half and is not rewritten here).

This batch moves no count: no tool is added or removed, no scope is added, no refusal
code is added. `report_agent_run`'s input gains five optional fields and five `$defs`
entries, its output gains two, and `get_consumer_lag`'s output shape does not move at
all — only the window behind `recent_samples` and the description that advertises it.

Two things here are deliberately NOT symmetric with WO-R3-312's fields, and both are
pinned so nobody "tidies" them:

- `current_hypothesis` and `last_step` are still the current reading and are still
  REPLACED on every call. The five new fields are FILLED IN and never cleared by a
  later report that omits them, because the reporter now sends a step-only report
  after every tool call and clearing would blank a panel an operator is reading.
- the new `step.kind` vocabulary is wider than `last_step.kind`: a status report is a
  step in the ledger and was never a `last_step`.
"""

from __future__ import annotations

import pathlib
from typing import Any

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.consumer_lag import (
    LAG_SAMPLES_KEEP,
    LAG_SAMPLES_TTL,
    LAG_SAMPLES_WINDOW_SECONDS,
)
from app.core.scopes import ALL_SCOPES, Scope
from app.mcp.registry import ToolDefinition, list_tools
from app.services.agent_run import STEPS_CAP, VERIFICATIONS_CAP
from pydantic import BaseModel

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: `report_agent_run`'s input after this order. Everything above the blank line is
#: WO-R3-312's shape, byte for byte; the five below it are this order's delta.
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

#: Its output gains two: the cap is observable to the caller that fills it.
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

#: The `$defs` WO-R3-312 shipped. Unchanged, in name and in members.
HYPOTHESIS_FIELDS = {"name", "category", "confidence"}
STEP_FIELDS = {"kind", "tool", "at"}

#: The five new `$defs` entries.
RANKED_HYPOTHESIS_FIELDS = {"name", "category", "confidence", "reasoning_excerpt"}
PLAN_FIELDS = {
    "action_tool",
    "action_arguments",
    "target_hypothesis",
    "rationale_excerpt",
}
VERIFICATION_FIELDS = {"verdict", "reasoning_excerpt", "attempt", "of"}
STEP_EVENT_FIELDS = {
    "seq",
    "kind",
    "tool",
    "arguments",
    "result_excerpt",
    "outcome",
    "latency_ms",
    "at",
}
BUDGET_FIELDS = {
    "tool_calls_used",
    "tool_calls_max",
    "tokens_used",
    "usd_used",
    "wall_seconds",
}

DEFS_AFTER = {
    "HypothesisReport",
    "StepReport",
    "RankedHypothesisReport",
    "PlanReport",
    "VerificationReport",
    "StepEventReport",
    "BudgetReport",
}

#: The truncation limits the ledger names. The caller truncates; these refuse a caller
#: that did not, so an excerpt is never a full tool output by accident.
EXCERPT_LIMITS = {
    ("RankedHypothesisReport", "reasoning_excerpt"): 280,
    ("PlanReport", "rationale_excerpt"): 280,
    ("VerificationReport", "reasoning_excerpt"): 280,
    ("StepEventReport", "result_excerpt"): 400,
}

#: `get_consumer_lag`'s output, unchanged by this order.
CONSUMER_LAG_OUTPUT_FIELDS = {
    "consumer_group",
    "lag",
    "lag_known",
    "source",
    "cache_key",
    "measured_at",
    "age_seconds",
    "recent_samples",
}


def _tool(name: str) -> ToolDefinition:
    definition = next((t for t in list_tools() if t.name == name), None)
    assert definition is not None, f"{name} is not registered"
    return definition


def _claude_md() -> str:
    return (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _nested(name: str) -> type[BaseModel]:
    from app.mcp.tools import commander_runs

    model = getattr(commander_runs, name, None)
    assert model is not None, f"{name} is not defined"
    return model  # type: ignore[return-value]


def _max_length(model: type[BaseModel], field: str) -> int | None:
    """The declared `max_length`, dug out of the field metadata.

    Not off the JSON Schema: an optional string serialises as an `anyOf`, and the limit
    then lives one level down in a branch whose order is not ours to depend on.
    """
    for constraint in model.model_fields[field].metadata:
        limit: Any = getattr(constraint, "max_length", None)
        if limit is not None:
            return int(limit)
    return None


def test_the_surface_does_not_grow() -> None:
    """No tool is added, so `tools/list` gains no name: the whole delta is inside one
    input model, one output model and one description."""
    assert sorted(t.name for t in list_tools() if t.is_commander) == [
        "report_agent_briefing",
        "report_agent_run",
    ]
    assert len(list_tools()) == 25, sorted(t.name for t in list_tools())

    read = sorted(
        t.name
        for t in list_tools()
        if t.required_scope in {Scope.TELEMETRY_READ, Scope.INCIDENTS_READ}
    )
    assert len(read) == 16, read


def test_the_shape_deltas_are_exactly_these() -> None:
    """Where the ledger's field list is pinned: a shape change it does not mention is
    what makes a re-pin surprising."""
    run = _tool("report_agent_run")

    assert set(run.input_model.model_fields) == REPORT_AGENT_RUN_INPUT_FIELDS
    assert set(run.output_model.model_fields) == REPORT_AGENT_RUN_OUTPUT_FIELDS

    schema = run.input_json_schema()
    assert set(schema["$defs"]) == DEFS_AFTER
    assert set(schema["$defs"]["RankedHypothesisReport"]["properties"]) == (
        RANKED_HYPOTHESIS_FIELDS
    )
    assert set(schema["$defs"]["PlanReport"]["properties"]) == PLAN_FIELDS
    assert set(schema["$defs"]["VerificationReport"]["properties"]) == (
        VERIFICATION_FIELDS
    )
    assert set(schema["$defs"]["StepEventReport"]["properties"]) == STEP_EVENT_FIELDS
    assert set(schema["$defs"]["BudgetReport"]["properties"]) == BUDGET_FIELDS


def test_the_briefing_tool_does_not_move_at_all() -> None:
    """`report_agent_briefing` is untouched by this order — the run record grew, the
    write-up did not."""
    briefing = _tool("report_agent_briefing")

    assert set(briefing.input_model.model_fields) == {"run_id", "briefing", "prose", "at"}
    assert set(briefing.output_model.model_fields) == {
        "run_id",
        "state",
        "recorded_at",
        "finished_at",
        "accepted",
    }


def test_the_two_current_reading_shapes_are_untouched() -> None:
    """WO-R3-312's `$defs` keep their members AND their replace-on-every-call meaning.
    A caller written against that release reads the same fields with the same rules."""
    schema = _tool("report_agent_run").input_json_schema()

    assert set(schema["$defs"]["HypothesisReport"]["properties"]) == HYPOTHESIS_FIELDS
    assert set(schema["$defs"]["StepReport"]["properties"]) == STEP_FIELDS

    for field in ("current_hypothesis", "last_step"):
        described = schema["properties"][field]["description"].lower()
        assert "replaced wholesale" in described, field


def test_every_new_input_field_is_optional_and_additive() -> None:
    """The order's own word: all optional, additive. A release that required one of
    these would refuse every report the previous reporter sends."""
    schema = _tool("report_agent_run").input_json_schema()

    assert set(schema["required"]) == {"run_id", "state"}
    for field in ("hypotheses", "plan", "verification", "step", "budget"):
        assert _tool("report_agent_run").input_model.model_fields[field].is_required() is (
            False
        ), field


def test_the_new_fields_are_filled_in_and_never_cleared() -> None:
    """The asymmetry with `current_hypothesis`, said out loud on the wire.

    The reporter sends a step-only report after every tool call. If omitting the ranked
    list cleared it, the agent panel would blank between transitions — which is the
    failure this order exists to remove, in a new place.
    """
    schema = _tool("report_agent_run").input_json_schema()

    for field in ("hypotheses", "plan", "verification", "budget"):
        described = schema["properties"][field]["description"].lower()
        assert "never cleared" in described, field
        assert "replaced wholesale" not in described, field


def test_one_step_per_call_and_the_cap_is_stated() -> None:
    """Never promise completeness you cap (CLAUDE.md's own rule): the list is bounded,
    so the description says the bound and the receipt reports what was dropped."""
    run = _tool("report_agent_run")
    described = run.input_json_schema()["properties"]["step"]["description"].lower()

    assert "one step per call" in described
    assert str(STEPS_CAP) in described
    assert "steps_dropped" in run.output_json_schema()["properties"]


def test_the_excerpt_limits_are_the_ones_the_ledger_names() -> None:
    """Excerpts, not full outputs (ADR 0037). The limits are refused at the wire so a
    caller that forgets to truncate cannot turn this table into a trace store."""
    for (model_name, field), limit in EXCERPT_LIMITS.items():
        assert _max_length(_nested(model_name), field) == limit, (model_name, field)


def test_the_step_kind_vocabulary_is_closed_and_wider_than_last_steps() -> None:
    """A status report is a step in the ledger. It was never a `last_step`, so the two
    enums differ on purpose and the narrower one does not move."""
    schema = _tool("report_agent_run").input_json_schema()

    assert schema["$defs"]["StepEventReport"]["properties"]["kind"]["enum"] == [
        "read",
        "action",
        "report",
    ]
    assert schema["$defs"]["StepReport"]["properties"]["kind"]["enum"] == [
        "read",
        "action",
    ]


def test_the_verification_verdict_is_open_and_says_so() -> None:
    """The caller's vocabulary, not ours: a closed enum here would refuse a verdict the
    responder has and the platform has no opinion about."""
    schema = _tool("report_agent_run").input_json_schema()
    verdict = schema["$defs"]["VerificationReport"]["properties"]["verdict"]

    assert "enum" not in verdict
    assert "verified" in verdict["description"]


@pytest.mark.parametrize("name", ["report_agent_run", "report_agent_briefing"])
def test_no_class_docstring_reaches_a_pinned_schema(name: str) -> None:
    """plat #210: Pydantic copies a model's class docstring into the schema's top-level
    `description`, and these schemas are pinned in the commander's snapshot."""
    for schema in (_tool(name).input_json_schema(), _tool(name).output_json_schema()):
        assert "description" not in schema
        for defined in schema.get("$defs", {}).values():
            assert "description" not in defined


def test_the_input_still_forbids_extra_fields() -> None:
    schema = _tool("report_agent_run").input_json_schema()

    assert schema.get("additionalProperties") is False
    for name in DEFS_AFTER:
        assert schema["$defs"][name].get("additionalProperties") is False, name


def test_no_new_scope_and_no_new_refusal_code() -> None:
    """The batch widens a write surface that already had a scope and already had its
    three refusals; a fourth code would have to reach the commander's client."""
    from app.services import agent_run as module

    assert len(ALL_SCOPES) == 6
    codes = sorted(
        obj.error_code
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Exception)
        and obj.__module__ == module.__name__
    )
    assert codes == [
        "agent_run_already_finished",
        "agent_run_briefing_already_recorded",
        "agent_run_not_found",
    ]


def test_the_consumer_lag_output_shape_does_not_move() -> None:
    """Same reading, longer window. `recent_samples` keeps its name, its members and
    its newest-first order — only how many of them there are changes."""
    schema = _tool("get_consumer_lag").output_json_schema()

    assert set(schema["properties"]) == CONSUMER_LAG_OUTPUT_FIELDS
    assert set(schema["$defs"]["LagSample"]["properties"]) == {"lag", "measured_at"}
    assert set(_tool("get_consumer_lag").input_model.model_fields) == {"consumer_group"}


def test_the_lag_window_is_fifteen_minutes_and_the_description_says_so() -> None:
    """The window is the delta a re-pin sees: a description that still said "five"
    would be a tool lying about how much history it holds."""
    assert LAG_SAMPLES_WINDOW_SECONDS == 900
    assert LAG_SAMPLES_KEEP == 15
    # Long enough that a full window survives a gap in the metrics pass, short enough
    # that a window nothing refreshes still disappears.
    assert LAG_SAMPLES_TTL > LAG_SAMPLES_WINDOW_SECONDS

    surface = (
        _tool("get_consumer_lag").description
        + _tool("get_consumer_lag").output_json_schema()["properties"][
            "recent_samples"
        ]["description"]
    )
    assert "15 minutes" in surface


def test_the_new_columns_are_on_the_row() -> None:
    from app.models.agent_run import AgentRun

    for column in (
        "hypotheses",
        "plan",
        "verification",
        "verifications",
        "steps",
        "steps_dropped",
        "budget",
    ):
        assert column in AgentRun.__table__.columns, column
    # `phase_history` is unchanged, which the order states explicitly.
    assert "phase_history" in AgentRun.__table__.columns


def test_the_caps_are_declared_once_and_shared() -> None:
    """Two lists, two bounds, both in the service that appends to them — the REST shape
    and the tool description read them rather than repeating a number."""
    assert STEPS_CAP == 200
    assert VERIFICATIONS_CAP == 50


def test_adr_0037_exists_and_is_indexed() -> None:
    adr = _REPO_ROOT / "docs" / "ADR" / "0037-a-run-record-carries-the-run.md"
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")

    assert adr.is_file(), "ADR 0037 is missing"
    assert adr.name in index, "ADR 0037 is not in docs/ADR/README.md"


def test_the_rebless_ledger_names_this_delta() -> None:
    """The commander re-pins off this paragraph. A field added without a note there is
    a surprise at snapshot time."""
    ledger = _claude_md()

    assert "WO-R3-328" in ledger
    for name in ("hypotheses", "verifications", "steps_dropped", "reasoning_excerpt"):
        assert name in ledger, name
    assert "/admin/agent-runs/{id}/steps" in ledger
    assert "exclude_prefix" in ledger


def test_the_data_model_doc_carries_the_new_columns() -> None:
    """Every column is documented with a one-line why (CLAUDE.md's documentation map
    says so), and a capped column has to say what the cap does."""
    data_model = (_REPO_ROOT / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")

    for column in ("hypotheses", "verifications", "steps_dropped", "budget"):
        assert column in data_model, column


def test_the_rest_delta_is_documented() -> None:
    """The console is written against the note, not against the diff."""
    architecture = (_REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "/admin/agent-runs/{id}/steps" in architecture
    assert "after_seq" in architecture
    assert "exclude_prefix" in architecture
