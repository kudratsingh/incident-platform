"""A poisoned row says it is not replay-safe, and a lying row says it lies
(WO-R2-166).

Tool descriptions are the whole interface — the agent cannot read the
docstrings, the ADRs or this file, so a description that steers wrong is a
functional defect (CLAUDE.md, "Tool descriptions — normative"). Two of them
steered wrong here, and the platform's behaviour agreed with them:

  * `poison_message` said it dropped a `replay_safe` DLQ entry, and it did.
    A message that fails schema validation fails identically on every
    attempt, so "replay-safe" was never true of it. The pairing was the live
    defect of WO-R2-146 (run efdc3b2a9864: the agent read the schema error,
    refused the replay, and was graded down for being right); that repair
    moved the *text* to a transient one and left the hint, which made the
    row coherent and still false. The hint moves now.
  * Nothing could produce a row whose hint its own text contradicts, so no
    scenario could ask whether the agent believes the label or the evidence.
    `create_mislabeled_dlq_job` produces exactly that row and says so in the
    first clause of its description.

Pinned as claims rather than as whole-string snapshots, same convention as
`test_fence_tool_descriptions.py` and `test_dag_tool_descriptions.py`: a
snapshot of a 1500-character description fails on every wording change and
tells the next reader nothing about which sentence mattered.

Rebless note: these strings and shapes are pinned by the commander's
contract snapshot, so they land at the next re-pin, together with the new
tool itself. The enumerated deltas are at the bottom of this file.
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


@pytest.fixture
def chaos_registered() -> Iterator[None]:
    """Both hooks are chaos-gated, so with `CHAOS_ENABLED=false` — the unit
    default — neither enters the registry and neither has a description to
    assert on (ADR 0008 gate 1).

    Reload them under a patched settings so the decorators re-evaluate, then
    restore the registry. Same trick as `test_chaos_gating.py`.
    """
    from app.mcp import chaos as chaos_mod
    from app.mcp.tools import chaos as chaos_pkg

    modules = (
        chaos_pkg.poison_message,  # type: ignore[attr-defined]
        chaos_pkg.create_mislabeled_dlq_job,  # type: ignore[attr-defined]
    )
    snapshot = _snapshot_for_tests()
    try:
        with patch.object(
            chaos_mod,
            "get_settings",
            return_value=Settings(chaos_enabled=True, environment="test"),
        ):
            for module in modules:
                importlib.reload(module)
        yield
    finally:
        _restore_for_tests(snapshot)
        # Reload once more under the real (disabled) settings so the module
        # objects left in `sys.modules` match the registry the next test
        # sees.
        for module in modules:
            importlib.reload(module)


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
# poison_message — says what it produces, and that it is not replay-safe
# --------------------------------------------------------------------------


def test_poison_message_says_the_row_is_not_replay_safe(
    chaos_registered: None,
) -> None:
    """The sentence the whole change exists for. In caps because it reverses
    what this description said for four releases, and an agent that skims
    has to catch it."""
    text = _description("poison_message")
    assert "THE ROW IS NOT REPLAY-SAFE" in text
    assert "replay_safe` cannot be asked for" in text


def test_poison_message_says_what_the_row_actually_describes(
    chaos_registered: None,
) -> None:
    """Not just the negation — a description that only says "not
    replay-safe" leaves the agent to guess the fault. It names the schema
    violation and why a replay cannot clear it."""
    text = _description("poison_message")
    assert "schema violation" in text
    assert "required field missing from the stored payload" in text
    assert "every replay of it fails on the same field" in text


def test_poison_message_says_the_hint_only_says_who_classified_it(
    chaos_registered: None,
) -> None:
    """The distinction the two accepted values turn on. `unclassified` is
    not a weaker `human_required`: it is the same fault with nobody having
    categorised it yet, which is the honest arrival state because triage is
    off by default."""
    text = _description("poison_message")
    assert "decides only whether anything has classified that yet" in text
    assert "LLM triage is off by default" in text
    assert "leaves `remediation_hint` NULL" in text
    assert "refuses the row on sight" in text


def test_poison_message_states_the_deterministic_id_and_disposal(
    chaos_registered: None,
) -> None:
    text = _description("poison_message")
    assert "derives deterministically from the calling tenant" in text
    assert "in its own namespace" in text
    assert "idempotent on the row while it still matches" in text
    assert "DELETEd by the next environment reset" in text


def test_poison_message_says_publishing_is_not_idempotent(
    chaos_registered: None,
) -> None:
    """The one genuinely surprising bit of the new shape: the row is
    idempotent and the send is not, so a caller re-invoking to be sure the
    fixture exists puts a second poisoned message on the topic."""
    text = _description("poison_message")
    assert "Publishing is not idempotent" in text
    assert "another poisoned message on the topic" in text


def test_poison_message_field_descriptions_carry_the_details(
    chaos_registered: None,
) -> None:
    """The recipe a caller needs to precompute an id belongs on the field
    that names it, the way `create_stuck_dag`'s and `create_bad_data_job`'s
    do."""
    fixture_name = _field_description("poison_message", "fixture_name")
    assert "uuid5(eeeeeeee-dead-4000-8000-000000000000" in fixture_name
    assert "{tenant_id}:{fixture_name}" in fixture_name
    assert "poison_fixture_name_in_use" in fixture_name
    # The refusal ordering, on the field that can trigger it: a refused call
    # must not have poisoned a topic on its way to the refusal.
    assert "before the Kafka send" in fixture_name

    hint = _field_description("poison_message", "remediation_hint")
    assert "the DEFAULT" in hint
    assert "`replay_safe` is not accepted" in hint


def test_poison_message_output_field_repeats_the_not_replay_safe_claim(
    chaos_registered: None,
) -> None:
    """An agent that reads only the tool's result still has to be told. The
    old text advertised `remediation_hint=replay_safe` right here."""
    spec = get_tool("poison_message")
    assert spec is not None
    described = spec.output_model.model_fields["dlq_job_id"].description or ""
    assert "NOT replay-safe" in described
    hint = spec.output_model.model_fields["remediation_hint"].description or ""
    assert "Never `replay_safe`" in hint


# --------------------------------------------------------------------------
# create_mislabeled_dlq_job — says, in plain words, that the row is a lie
# --------------------------------------------------------------------------


def test_mislabel_hook_says_the_hint_and_text_contradict_each_other(
    chaos_registered: None,
) -> None:
    text = _description("create_mislabeled_dlq_job")
    assert "deliberately mislabelled" in text
    assert "`remediation_hint` is `replay_safe` while the error text is a" in (
        text
    )
    assert "contradict each other" in text


def test_mislabel_hook_says_why_the_contradiction_is_the_fixture(
    chaos_registered: None,
) -> None:
    """Without the reason, a reader takes the tool for a bug. It names the
    real failure it reproduces — a wrong classification nothing downstream
    re-derives."""
    text = _description("create_mislabeled_dlq_job")
    assert "that contradiction is the fixture" in text
    assert "wrong classification" in text
    assert "nothing downstream re-derives it from the error" in text


def test_mislabel_hook_says_it_is_the_only_sanctioned_incoherent_row(
    chaos_registered: None,
) -> None:
    """The scope of the exception, stated where an agent can read it: every
    other row in the lab still obeys the rule, so this one being incoherent
    is information rather than noise."""
    text = _description("create_mislabeled_dlq_job")
    assert "ONLY sanctioned incoherent row in the lab" in text
    assert "every other hook is held to the rule" in text


def test_mislabel_hook_states_the_explicit_gate(chaos_registered: None) -> None:
    text = _description("create_mislabeled_dlq_job")
    assert "Requires `mislabel: true`, passed explicitly" in text
    assert "never be produced by accident" in text

    described = _field_description("create_mislabeled_dlq_job", "mislabel")
    assert "Must be `true`" in described
    assert "there is no default" in described
    # Says what refusing looks like, and why there is no fallback row.
    assert "is a validation error" in described


def test_mislabel_hook_states_the_deterministic_id_and_disposal(
    chaos_registered: None,
) -> None:
    text = _description("create_mislabeled_dlq_job")
    assert "derives deterministically from the calling tenant" in text
    assert "in its own namespace" in text
    assert "idempotent while the row still matches" in text
    assert "DELETEd by the next environment reset" in text

    fixture_name = _field_description(
        "create_mislabeled_dlq_job", "fixture_name"
    )
    assert "uuid5(ffffffff-11ed-4000-8000-000000000000" in fixture_name
    assert "mislabeled_fixture_name_in_use" in fixture_name


def test_mislabel_hook_output_says_the_hint_is_the_wrong_one(
    chaos_registered: None,
) -> None:
    spec = get_tool("create_mislabeled_dlq_job")
    assert spec is not None
    hint = spec.output_model.model_fields["remediation_hint"].description or ""
    assert "the label that is wrong" in hint
    assert "not a configurable field" in hint


# --------------------------------------------------------------------------
# create_bad_data_job — no longer calls poison_message a replay_safe producer
# --------------------------------------------------------------------------


def test_create_bad_data_job_no_longer_miscasts_its_sibling() -> None:
    """A cross-reference is part of the interface too. This description told
    an agent that `poison_message` produces `replay_safe` entries, which is
    now false and was always misleading.

    Registered separately from the fixture above because this tool is
    reloaded by `test_fence_tool_descriptions.py`'s own fixture; here it is
    read straight from a chaos-enabled reload of just this module.
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
        text = _description("create_bad_data_job")
        assert "whose row carries a schema violation instead" in text
        assert "neither is replay-safe" in text
        assert "produces `replay_safe` entries" not in text
    finally:
        _restore_for_tests(snapshot)
        importlib.reload(chaos_pkg.create_bad_data_job)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# The deltas, enumerated — this change adds a tool and moves two schemas
# --------------------------------------------------------------------------


def test_the_shape_deltas_are_exactly_these(chaos_registered: None) -> None:
    """The rebless ledger needs the field list, so this is where it is
    pinned. A shape change the ledger does not mention is what makes a
    re-pin surprising (same reasoning as
    `test_fence_tool_descriptions.py::test_the_output_shape_deltas_are_exactly_these`).
    """
    poison = get_tool("poison_message")
    assert poison is not None
    # +fixture_name, +remediation_hint on the way in.
    assert set(poison.input_model.model_fields) == {
        "topic",
        "payload",
        "partition_key",
        "fixture_name",
        "remediation_hint",
    }
    # +fixture_name, +remediation_hint, +created on the way out.
    assert set(poison.output_model.model_fields) == {
        "topic",
        "payload_bytes",
        "partition_key",
        "accepted",
        "dlq_job_id",
        "fixture_name",
        "remediation_hint",
        "created",
    }

    # Wholly new tool — a tool-level delta, not a field-level one.
    mislabel = get_tool("create_mislabeled_dlq_job")
    assert mislabel is not None
    assert set(mislabel.input_model.model_fields) == {
        "mislabel",
        "fixture_name",
        "job_type",
    }
    assert set(mislabel.output_model.model_fields) == {
        "job_id",
        "fixture_name",
        "remediation_hint",
        "error_message",
        "created",
        "accepted",
    }


def test_the_new_refusal_codes_are_exactly_these() -> None:
    """Two new `error_code` strings reach the commander's ChaosClient, which
    buckets unknown codes as transport faults — so a code it has never seen
    reads as flakiness rather than as a fixture bug (R2-16). They belong in
    the ledger for that reason."""
    from app.mcp.tools.chaos.create_mislabeled_dlq_job import (
        CreateMislabeledDlqJobError,
    )
    from app.mcp.tools.chaos.poison_message import (
        PoisonMessageFixtureNameInUseError,
    )

    assert PoisonMessageFixtureNameInUseError.error_code == (
        "poison_fixture_name_in_use"
    )
    assert PoisonMessageFixtureNameInUseError.status_code == 409
    assert CreateMislabeledDlqJobError.error_code == (
        "mislabeled_fixture_name_in_use"
    )
    assert CreateMislabeledDlqJobError.status_code == 409
