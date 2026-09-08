"""A fence describes itself as an action with a verification surface (WO-R2-158).

Tool descriptions are the whole interface — the agent cannot read the
docstrings, the ADRs or this file, so a description that steers wrong is a
functional defect (CLAUDE.md, "Tool descriptions — normative"). Three of
them steered wrong around `mark_dlq_permanent`, and the platform's own
behaviour agreed with them:

  * `mark_dlq_permanent` called itself "idempotent" and left a caller to
    conclude that marking an already-`human_required` row does nothing. It
    genuinely did nothing — no row write, no audit row — so the reading was
    accurate and the behaviour was the bug. Now every mark writes, and the
    description says what a re-mark does.
  * Nothing said how to *verify* a fence. Re-reading `remediation_hint`
    looks like verification and is not: `human_required` is the same value
    whether triage classified the row or somebody fenced it, so the hint
    cannot tell a caller its own call landed. `fenced_at` can, and both
    descriptions now name it as the surface.
  * `create_bad_data_job` advertised one shape — a row that arrives already
    classified — which is the one shape that cannot measure a fence. The
    hint is now an argument and the description says why the unclassified
    variant exists.

Pinned as claims rather than as whole-string snapshots, same convention as
`test_dag_tool_descriptions.py`: a snapshot of a 1500-character description
fails on every wording change and tells the next reader nothing about which
sentence mattered.

Rebless note: these strings are pinned by the commander's contract snapshot,
so they land at the next re-pin, together with the two new `DlqEntry` fields
and the new `mark_dlq_permanent` output field asserted at the bottom.
"""

import importlib
from collections.abc import Iterator
from unittest.mock import patch

import app.mcp.tools  # noqa: F401  — import fires every @tool decorator
import pytest
from app.config import Settings
from app.mcp.registry import (
    _restore_for_tests,
    _snapshot_for_tests,
    get_tool,
)
from app.mcp.tools.list_dlq_messages import DlqEntry


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """`create_bad_data_job` is chaos-gated, so with `CHAOS_ENABLED=false`
    — the unit default — it never enters the registry and has no
    description to assert on (ADR 0008 gate 1).

    Reload it under a patched settings so the decorator re-evaluates, then
    restore the registry. Same trick as `test_chaos_gating.py`; kept local
    to the chaos assertions so the plain-tool tests above still run against
    the registry as it really boots.
    """
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools import chaos as chaos_pkg

    snapshot = _snapshot_for_tests()
    try:
        with patch.object(
            chaos_mod,
            "get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            importlib.reload(chaos_pkg.create_bad_data_job)  # type: ignore[attr-defined]
        yield
    finally:
        _restore_for_tests(snapshot)
        # Reload once more under the real (disabled) settings so the
        # module object left in `sys.modules` matches the registry the
        # next test sees.
        importlib.reload(chaos_pkg.create_bad_data_job)  # type: ignore[attr-defined]


def _description(tool_name: str) -> str:
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    return spec.description


def _field_description(tool_name: str, field: str) -> str:
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    schema = spec.input_model.model_json_schema()
    described = schema["properties"][field].get("description", "")
    assert described, f"{tool_name}.{field} has no description"
    return str(described)


# --------------------------------------------------------------------------
# mark_dlq_permanent — names its verification surface, and what a re-mark does
# --------------------------------------------------------------------------


def test_mark_dlq_permanent_names_fenced_at_as_the_verification_surface() -> None:
    text = _description("mark_dlq_permanent")
    assert "VERIFYING THE FENCE:" in text
    assert "`fenced_at` is the surface" in text


def test_mark_dlq_permanent_warns_the_hint_is_not_verification() -> None:
    """The trap this sentence closes: an agent that re-reads the hint has
    read a value that was already there before its call."""
    text = _description("mark_dlq_permanent")
    assert "Re-reading `remediation_hint` is NOT verification" in text
    assert "whether triage classified the row or somebody fenced it" in text


def test_mark_dlq_permanent_says_a_re_mark_is_not_a_no_op() -> None:
    """The exact reading that made an eval grade a no-op as a fence."""
    text = _description("mark_dlq_permanent")
    assert "RE-MARKING AN ALREADY-FENCED ROW: it is not a no-op." in text
    assert "an audit row is written" in text
    assert "already_marked: true" in text


def test_mark_dlq_permanent_still_states_what_idempotent_means_here() -> None:
    """"Every mark writes" and "idempotent" are both true and could read
    as contradictory, so the description has to say which scope each is
    about — per execution vs per `idempotency_key` (ADR 0010)."""
    text = _description("mark_dlq_permanent")
    assert "repeating the same" in text
    assert "without\nre-executing" in text or "without re-executing" in text
    assert "a deliberate re-fence needs a new key" in text


def test_mark_dlq_permanent_says_which_clock_fenced_at_is_on() -> None:
    text = _description("mark_dlq_permanent")
    assert "platform clock, UTC" in text


# --------------------------------------------------------------------------
# list_dlq_messages — says what the two new fields answer
# --------------------------------------------------------------------------


def test_list_dlq_says_who_classified_the_row() -> None:
    text = _description("list_dlq_messages")
    assert "WHO CLASSIFIED IT:" in text
    assert "`fenced_at` and `fenced_by`" in text
    assert "`remediation_hint` cannot" in text


def test_list_dlq_says_a_re_fence_moves_the_stamp() -> None:
    """Without this an agent could read a non-null `fenced_at` as proof of
    its own call when the stamp predates it."""
    text = _description("list_dlq_messages")
    assert "re-stamped on every `mark_dlq_permanent` call" in text


def test_list_dlq_says_a_replay_clears_the_fence_stamps() -> None:
    """The episode scope, on the surface that reads the fields — the
    columns are cleared with the hint by `JobService.replay_job`."""
    text = _description("list_dlq_messages")
    assert "a replay clears them with the hint" in text


@pytest.mark.parametrize("field", ["fenced_at", "fenced_by"])
def test_the_new_dlq_entry_fields_are_described(field: str) -> None:
    described = DlqEntry.model_fields[field].description
    assert described, f"DlqEntry.{field} has no description"
    # Both must say what null means: an undescribed null is the ambiguity
    # the whole pair exists to remove.
    assert "ull" in described


def test_fenced_at_field_says_which_clock_it_is_not() -> None:
    """The repo's first description rule. This row carries four
    timestamps and three of them are not this one."""
    described = DlqEntry.model_fields["fenced_at"].description or ""
    assert "`dead_lettered_at`" in described
    assert "`created_at`" in described


def test_fenced_by_field_states_its_format() -> None:
    described = DlqEntry.model_fields["fenced_by"].description or ""
    assert "{principal_type}:{principal_id}" in described


# --------------------------------------------------------------------------
# create_bad_data_job — says why the unclassified variant exists
# --------------------------------------------------------------------------


def test_create_bad_data_job_says_the_hint_decides_the_shape(chaos_registered: None) -> None:
    text = _description("create_bad_data_job")
    assert "`remediation_hint` decides whether the row arrives classified" in (
        text
    )
    assert "`unclassified` (or null) leaves `remediation_hint` NULL" in text


def test_create_bad_data_job_says_why_an_unclassified_row_is_needed(chaos_registered: None) -> None:
    """The reason, not just the option — an unclassified row is the only
    shape on which a fence is an observable action."""
    text = _description("create_bad_data_job")
    assert "observable action rather than a value" in text
    assert "mark_dlq_permanent" in text


def test_create_bad_data_job_states_the_deterministic_id_and_disposal(
    chaos_registered: None,
) -> None:
    text = _description("create_bad_data_job")
    assert "derives deterministically from the calling tenant" in text
    assert "same name in another tenant is a separate row" in text
    assert "idempotent while the row still matches" in text
    assert "DELETEd by the next environment reset" in text


def test_create_bad_data_job_field_descriptions_carry_the_details(chaos_registered: None) -> None:
    """The recipe an caller needs to precompute an id belongs on the field
    that names it, the way `create_stuck_dag`'s does."""
    fixture_name = _field_description("create_bad_data_job", "fixture_name")
    assert "uuid5(dddddddd-bad0-4000-8000-000000000000" in fixture_name
    assert "{tenant_id}:{fixture_name}" in fixture_name
    assert "bad_data_fixture_name_in_use" in fixture_name

    hint = _field_description("create_bad_data_job", "remediation_hint")
    # Omission and explicit null are different, and a caller has to be
    # told so — this is the one genuinely surprising bit of the argument.
    assert "Omitting the field is not the same as passing null" in hint


def test_create_bad_data_job_field_says_what_a_wrong_error_text_does(
    chaos_registered: None,
) -> None:
    described = _field_description("create_bad_data_job", "error_message")
    assert "contradicts a `human_required` hint" in described
    assert "a replay nothing has authorised" in described


# --------------------------------------------------------------------------
# The shape deltas, enumerated — this change is not description-only
# --------------------------------------------------------------------------


def test_the_output_shape_deltas_are_exactly_these(chaos_registered: None) -> None:
    """Unlike WO-R2-141, this change moves schemas, so the rebless ledger
    needs the field list and this test is where it is pinned. A shape
    change the ledger does not mention is what makes a re-pin surprising.
    """
    entry_fields = set(DlqEntry.model_fields)
    assert {"fenced_at", "fenced_by"} <= entry_fields

    mark = get_tool("mark_dlq_permanent")
    assert mark is not None
    assert set(mark.output_model.model_fields) == {
        "job_id",
        "previous_hint",
        "remediation_hint",
        "already_marked",
        "fenced_at",
    }
    # Input shape unchanged — a caller does not fence differently.
    assert set(mark.input_model.model_fields) == {
        "job_id",
        "reason",
        "idempotency_key",
    }

    hook = get_tool("create_bad_data_job")
    assert hook is not None
    assert set(hook.input_model.model_fields) == {
        "fixture_name",
        "job_type",
        "remediation_hint",
        "error_message",
    }
    assert set(hook.output_model.model_fields) == {
        "job_id",
        "fixture_name",
        "remediation_hint",
        "created",
        "accepted",
    }


def test_list_dlq_input_shape_is_unchanged() -> None:
    """The two new fields are on the way out, not the way in — no new
    filter, so `WHO CLASSIFIED IT` is a read the agent gets for free on a
    listing it was already making."""
    spec = get_tool("list_dlq_messages")
    assert spec is not None
    assert set(spec.input_model.model_fields) == {
        "job_type",
        "remediation_hint",
        "limit",
        "offset",
    }
