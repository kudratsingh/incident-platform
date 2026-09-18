"""Every lab-written DLQ row's error text agrees with its hint (WO-R2-146).

Found live, not in review: run `efdc3b2a9864` put the agent in front of a `replay_safe` row whose
`error_message` was a `SchemaValidationError`, so a sound escalation was graded a failure. Two
layers, both needed — `coherence_violations` must report that exact pair, and every writer's real
pairs must pass the screen. `tests/api/test_mcp_wave2_chaos_hooks.py` and
`test_mcp_chaos_stuck_dag.py` close the loop over the wire.
"""

import json
import sys
from pathlib import Path

import pytest
from app.lab.dlq_failure_stories import (
    ALL_STORIES,
    DLQ_FAILURE_STORIES,
    STORIES_BY_KEY,
    DlqFailureStory,
    UnknownDlqHintError,
    coherence_violations,
    default_error_for,
    sanctioned_incoherent_story,
    story,
    story_for,
    triage_violations,
)
from app.mcp.tools.chaos.create_bad_data_job import (
    CreateBadDataJobInput,
    _declared_hint,
    _default_error_for,
)
from app.mcp.tools.chaos.create_stuck_dag import CreateStuckDagInput
from app.mcp.tools.chaos.poison_message import (
    PoisonMessageInput,
    _dlq_error_for_topic,
)
from app.mcp.tools.chaos.seed_dlq_messages import SeedDlqMessagesInput
from app.models.enums import RemediationHint
from pydantic import ValidationError

# The seed script is not an importable package member — it lives under
# `scripts/` and is loaded by path everywhere else in this suite too.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "scripts")
)
import seed_eval_fixtures as seed  # type: ignore[import-not-found]  # noqa: E402

HINTS: tuple[str | None, ...] = (
    RemediationHint.REPLAY_SAFE.value,
    RemediationHint.WAIT_AND_REPLAY.value,
    RemediationHint.HUMAN_REQUIRED.value,
    None,
)

# Verbatim from live run efdc3b2a9864 — the row the agent read.
LIVE_DEFECT_HINT = RemediationHint.REPLAY_SAFE.value
LIVE_DEFECT_ERROR = (
    "SchemaValidationError: payload missing required field 'user_id' "
    "(received keys: ['tenant_id', 'action', 'ts'])"
)


# 1. The screen has teeth


def test_the_pair_that_shipped_is_reported_as_incoherent() -> None:
    """Red-before, kept as a permanent regression: this is the exact
    (hint, error_message) the live run graded an honest escalation on."""
    reasons = coherence_violations(LIVE_DEFECT_HINT, LIVE_DEFECT_ERROR)
    assert reasons, (
        "the screen accepts the pair that caused WO-R2-146 — it would "
        "not have caught the defect it was written for"
    )
    assert any("permanent" in r for r in reasons)


def test_no_writer_still_stamps_the_pair_that_shipped() -> None:
    """The narrow statement of the fix: whatever else changed, that
    text is no longer paired with `replay_safe` anywhere in the lab."""
    for hint, error in _every_writer_pair():
        assert not (
            hint == LIVE_DEFECT_HINT and "SchemaValidationError" in error
        ), f"a writer still pairs replay_safe with {error!r}"


@pytest.mark.parametrize(
    ("hint", "error", "expected"),
    [
        # A transient text under a hint that says the data is broken.
        (
            RemediationHint.HUMAN_REQUIRED.value,
            "UpstreamTimeout: timed out after 30s on attempt 3/3",
            "transient",
        ),
        # A rate limit under a hint that says replay right now.
        (
            RemediationHint.REPLAY_SAFE.value,
            "RateLimited: 429 Too Many Requests (retry-after: 120s)",
            "wait_and_replay",
        ),
        # Nothing at all under a hint that promises a reason to wait.
        (
            RemediationHint.WAIT_AND_REPLAY.value,
            "the job did not work",
            "does not say why to wait",
        ),
        # A "replay me" text under "nothing has classified this". The null
        # hint's rule is asymmetric — see the two cases below it.
        (
            None,
            "UpstreamTimeout: timed out after 30s, nothing was committed",
            "invites a replay",
        ),
        (
            None,
            "RateLimited: 429 Too Many Requests (retry-after: 120s)",
            "invites a replay",
        ),
    ],
)
def test_the_screen_catches_each_direction_of_contradiction(
    hint: str | None, error: str, expected: str
) -> None:
    reasons = coherence_violations(hint, error)
    assert any(expected in r for r in reasons), reasons


def test_an_unknown_hint_is_a_violation_not_a_pass() -> None:
    assert coherence_violations("replay_later", "anything at all")


# 1b. The null hint's rule is asymmetric, and that is the point (WO-R2-158)


def test_an_unclassified_row_may_carry_a_permanent_fault_text() -> None:
    """A hint is a classification and a text is a symptom: "nobody classified this" does not
    disagree with "the stored payload has a bad row", and that text already points away from a
    replay. RED before WO-R2-158."""
    reasons = coherence_violations(
        None,
        "ValueError: invalid literal for int() with base 10: 'N/A' at "
        "row 8,214",
    )
    assert not reasons, reasons


def test_the_unclassified_bad_data_pair_is_an_entry_in_the_table() -> None:
    """Declared in the table, not composed at the call site."""
    pinned = story("unclassified_csv_bad_row")
    assert pinned.hint is None
    assert "invalid literal for int()" in pinned.error_message
    assert "row 8,214" in pinned.error_message
    assert not coherence_violations(pinned.hint, pinned.error_message)
    # A null hint may never carry a triage block: that block *is* a
    # classification, so it contradicts the hint the way a text cannot.
    assert pinned.triage is None
    assert not triage_violations(pinned.hint, pinned.triage)
    assert pinned in DLQ_FAILURE_STORIES[None]


def test_the_unclassified_schema_pair_is_an_entry_in_the_table() -> None:
    """`poison_message`'s default story (WO-R2-166), declared in the table for the same reason: a
    schema violation under a null hint is coherent and says nothing about a replay."""
    pinned = story("unclassified_schema_missing_field")
    assert pinned.hint is None
    assert "SchemaValidationError" in pinned.error_message
    assert "missing required field" in pinned.error_message
    assert not coherence_violations(pinned.hint, pinned.error_message)
    assert pinned.triage is None
    assert not triage_violations(pinned.hint, pinned.triage)
    assert pinned in DLQ_FAILURE_STORIES[None]


def test_the_two_schema_stories_are_distinguishable() -> None:
    """`SCHEMA_MISSING_FIELD` is verbatim from live run efdc3b2a9864; the unclassified variant names
    a different field so the two stay distinguishable."""
    shipped = story("schema_missing_field")
    variant = story("unclassified_schema_missing_field")
    assert shipped.error_message != variant.error_message
    assert LIVE_DEFECT_ERROR not in variant.error_message


def test_the_unclassified_default_still_says_nothing_about_its_class() -> None:
    """Element 0 of the null tuple stays the worker-exit text, so `default_error_for(None)` is
    unchanged."""
    assert default_error_for(None) == story("unclassified_worker_exit").error_message
    assert story("unclassified_worker_exit").error_message != story(
        "unclassified_csv_bad_row"
    ).error_message


def test_a_null_hint_still_refuses_a_triage_block() -> None:
    """The half of the null-hint rule that did NOT loosen."""
    assert triage_violations(None, story("csv_bad_row").triage)


# 2. The table itself


@pytest.mark.parametrize("story_", ALL_STORIES, ids=lambda s: s.key)
def test_every_story_in_the_table_is_coherent(
    story_: DlqFailureStory,
) -> None:
    assert not coherence_violations(story_.hint, story_.error_message)
    assert not triage_violations(story_.hint, story_.triage)


@pytest.mark.parametrize("hint", HINTS)
def test_every_hint_the_platform_can_write_has_a_story(
    hint: str | None,
) -> None:
    """Including `None`: a null hint needs a text that stays silent about its class."""
    assert DLQ_FAILURE_STORIES[hint]
    assert default_error_for(hint) == story_for(hint).error_message


def test_the_hint_vocabulary_matches_the_enum() -> None:
    """A hint with no story leaves a writer nothing coherent to stamp, and `default_error_for`
    raises."""
    assert set(DLQ_FAILURE_STORIES) - {None} == {
        h.value for h in RemediationHint
    }


def test_story_keys_are_unique() -> None:
    assert len(STORIES_BY_KEY) == len(ALL_STORIES)


def test_an_unknown_hint_raises_rather_than_falling_back() -> None:
    """A fallback text would be a string of unknown class on a row of
    known class — the defect, reintroduced as a default."""
    with pytest.raises(UnknownDlqHintError):
        default_error_for("replay_later")
    with pytest.raises(UnknownDlqHintError):
        story("no_such_story")


# 3. Every writer


def _every_writer_pair() -> list[tuple[str | None, str]]:
    """(hint, error_message) for every value the lab's writers stamp, read off the writers not the
    table."""
    pairs: list[tuple[str | None, str]] = []

    # `seed_dlq_messages` and `create_stuck_dag` both substitute the canonical text when
    # `error_message` is omitted.
    for hint in (
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.HUMAN_REQUIRED.value,
    ):
        assert SeedDlqMessagesInput(remediation_hint=hint).error_message is None
        assert (
            CreateStuckDagInput(remediation_hint=hint).error_message is None
        )
        pairs.append((hint, default_error_for(hint)))

    # `create_bad_data_job`: two declarable hints since WO-R2-158, read through the hook's own
    # resolver so a hand-composed string shows up as an incoherent pair.
    for declared in ("human_required", "unclassified"):
        inp = CreateBadDataJobInput(remediation_hint=declared)  # type: ignore[arg-type]
        assert inp.error_message is None
        hint = _declared_hint(inp.remediation_hint)
        pairs.append((hint, _default_error_for(hint)))

    # `poison_message`: two declarable hints since WO-R2-166, neither `replay_safe`. Read off the
    # hook, so a regression that restored the old hint shows up as an incoherent pair.
    for declared_topic_hint in ("human_required", "unclassified"):
        poison_inp = PoisonMessageInput(
            topic="job.submitted",
            remediation_hint=declared_topic_hint,  # type: ignore[arg-type]
        )
        poison_hint = _declared_hint(poison_inp.remediation_hint)
        pairs.append(
            (poison_hint, _dlq_error_for_topic("job.submitted", poison_hint))
        )

    # The eval seed pack.
    for spec in seed._dlq_specs():
        pairs.append(
            (
                spec.get("remediation_hint"),  # type: ignore[arg-type]
                spec["error_message"],  # type: ignore[arg-type]
            )
        )
    return pairs


def test_every_writer_pair_is_coherent() -> None:
    failures = [
        (hint, error, reasons)
        for hint, error in _every_writer_pair()
        if (reasons := coherence_violations(hint, error))
    ]
    assert not failures, failures


def test_the_walk_covers_every_writer() -> None:
    """Guards the guard: a writer dropped from `_every_writer_pair`
    would silently shrink the check to nothing."""
    pairs = _every_writer_pair()
    # `create_mislabeled_dlq_job` is deliberately NOT in this walk: its whole output is the one
    # sanctioned incoherent pair. The section below asserts the screen still flags it.
    assert len(pairs) == 3 + 2 + 2 + len(seed._dlq_specs())
    # Both of `create_bad_data_job`'s hints are in the walk, so a dropped argument shows up as a
    # shrinking count and a missing null hint.
    assert None in [hint for hint, _ in pairs]


def test_create_stuck_dag_default_hint_is_still_covered() -> None:
    """Its default hint is stamped on the root when a caller names none,
    so it is a writer value in its own right."""
    default_hint = CreateStuckDagInput().remediation_hint
    assert not coherence_violations(
        default_hint, default_error_for(default_hint)
    )


@pytest.mark.parametrize(
    "hint", [None, RemediationHint.HUMAN_REQUIRED.value]
)
def test_poison_message_row_still_names_its_topic(hint: str | None) -> None:
    """Name the topic and correction without disclosing the injecting hook."""
    text = _dlq_error_for_topic("job.submitted", hint)
    assert "job.submitted" in text
    assert "chaos" not in text.lower()
    assert "poison_message" not in text
    assert "producer must correct the payload" in text


@pytest.mark.parametrize(
    "hint", [None, RemediationHint.HUMAN_REQUIRED.value]
)
def test_poison_message_row_describes_the_fault_it_injects(
    hint: str | None,
) -> None:
    """WO-R2-166: the hook publishes a schema-invalid payload, so its row says so under both hints.
    RED twice before — the schema text under `replay_safe`, then a coherent but false
    `UpstreamTimeout`."""
    text = _dlq_error_for_topic("job.submitted", hint)
    assert "SchemaValidationError" in text
    assert "missing required field" in text
    assert not coherence_violations(hint, text), text


def test_poison_message_cannot_be_asked_for_a_replay_safe_row() -> None:
    """`replay_safe` is not in the hook's input vocabulary at all, asserted on the input model
    because it is a schema property an agent reads."""
    with pytest.raises(ValidationError):
        PoisonMessageInput(
            topic="job.submitted",
            remediation_hint=RemediationHint.REPLAY_SAFE.value,  # type: ignore[arg-type]
        )
    # On the accepted values, not the rendered blob: the description names `replay_safe` on purpose,
    # to tell an agent reading the schema it is not on offer.
    schema = PoisonMessageInput.model_json_schema()
    field = schema["properties"]["remediation_hint"]
    accepted = {
        value
        for branch in field["anyOf"]
        for value in branch.get("enum", branch.get("const", []) or [])
    }
    assert accepted == {"human_required", "unclassified"}, json.dumps(field)


def test_poison_message_defaults_to_unclassified() -> None:
    """Omitting the field gives a NULL hint — not the old `replay_safe`, and not `human_required`,
    which would say a triage pass already ran."""
    assert PoisonMessageInput(topic="job.submitted").remediation_hint == (
        "unclassified"
    )
    assert _declared_hint(
        PoisonMessageInput(topic="job.submitted").remediation_hint
    ) is None


# 3b. The one sanctioned incoherent pair (WO-R2-166)


def test_the_sanctioned_pair_is_still_reported_as_incoherent() -> None:
    """The screen must keep reporting the sanctioned pair: teaching it to accept `replay_safe`
    beside a permanent-fault text is WO-R2-146 reintroduced."""
    lie = sanctioned_incoherent_story()
    assert lie.hint == RemediationHint.REPLAY_SAFE.value
    reasons = coherence_violations(lie.hint, lie.error_message)
    assert reasons, (
        "the screen accepts the deliberately mislabelled fixture — the "
        "sanctioned exception has become a hole in the rule"
    )
    assert any("permanent" in r for r in reasons)


def test_the_sanctioned_pair_is_unreachable_through_the_table() -> None:
    """It is declared, not tabulated. Nothing that resolves a text *from a
    hint* can reach it, so a writer cannot stamp it by accident — only the
    one named accessor returns it."""
    lie = sanctioned_incoherent_story()
    assert lie not in ALL_STORIES
    assert lie.key not in STORIES_BY_KEY
    for stories in DLQ_FAILURE_STORIES.values():
        assert lie not in stories
    # And the by-key door is shut too.
    with pytest.raises(UnknownDlqHintError):
        story(lie.key)
    # The canonical replay_safe text is still the transient one.
    assert default_error_for(RemediationHint.REPLAY_SAFE.value) != (
        lie.error_message
    )


def test_the_sanctioned_pair_is_not_the_string_that_shipped() -> None:
    """This fixture uses the CSV bad-row text so `test_no_writer_still_stamps_the_pair_that_shipped`
    stays absolute."""
    lie = sanctioned_incoherent_story()
    assert "SchemaValidationError" not in lie.error_message
    assert lie.error_message == story("csv_bad_row").error_message


def test_the_sanctioned_pair_carries_no_triage_block() -> None:
    """One lie per fixture. `is_retryable=True` beside a bad-data text
    would be a second, different contradiction and would muddy what the
    scenario measures."""
    lie = sanctioned_incoherent_story()
    assert lie.triage is None


# 4. The seeded pack is drawn from the table, not written beside it


def test_seeded_pack_texts_come_from_the_table() -> None:
    for spec in seed._dlq_specs():
        pinned = story(str(spec["story_key"]))
        assert spec["error_message"] == pinned.error_message
        assert spec["triage"] == seed._triage_from(pinned)
        assert pinned.hint == spec.get("remediation_hint")


def test_seeded_pack_shape_is_unchanged() -> None:
    """Ids, types, hints and order are pinned by scenario YAML; WO-R2-146 moves the texts and
    nothing else."""
    specs = seed._dlq_specs()
    assert [str(s["job_id"]) for s in specs] == [
        str(seed.stable(name))
        for name in (
            "dlq-job-schema-violation",
            "dlq-job-send-email",
            "dlq-job-process-payment",
            "dlq-job-csv-parse",
        )
    ]
    assert [s.get("remediation_hint") for s in specs] == [
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.HUMAN_REQUIRED.value,
    ]
    assert [s["type"] for s in specs] == [
        "bulk_api_sync",
        "bulk_api_sync",
        "bulk_api_sync",
        "csv_upload",
    ]
    assert [s["retry_count"] for s in specs] == [3, 3, 3, 3]


def test_the_two_wait_and_replay_rows_still_tell_different_stories() -> None:
    """The pack's value is in the variety a planner reads. Collapsing
    both onto one canonical text would be coherent and duller."""
    texts = [
        s["error_message"]
        for s in seed._dlq_specs()
        if s.get("remediation_hint") == RemediationHint.WAIT_AND_REPLAY.value
    ]
    assert len(texts) == 2
    assert texts[0] != texts[1]
