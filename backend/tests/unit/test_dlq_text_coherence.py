"""Every lab-written DLQ row's error text agrees with its hint (WO-R2-146).

The defect this file exists to stop was found live, not in review. Run
`efdc3b2a9864` (2026-09-07) put the agent in front of a dead-lettered DAG
root stamped `remediation_hint=replay_safe` whose `error_message` read
`SchemaValidationError: payload missing required field 'user_id'`. The
agent read the row, reasoned that a payload missing a required field
fails the same way every time, and escalated instead of replaying —
sound operator judgement, graded as a failure, because the fixture
contradicted itself. In the lab no processor validates payloads, so the
hint was the truth and the text was decoration. Nothing on the wire said
which to believe.

Two layers here, and both are needed:

  1. **The screen has teeth.** `coherence_violations` is exercised
     against the exact pair that shipped, and must report it. A screen
     that passes everything would make layer 2 meaningless.
  2. **Every writer's pairs pass the screen.** Not the table's own
     entries restated — the values each writer actually stamps: the
     defaults its input model advertises, the string
     `poison_message` composes, and the four rows the eval seed pack
     carries.

`tests/api/test_mcp_wave2_chaos_hooks.py` and
`test_mcp_chaos_stuck_dag.py` close the loop end to end, asserting the
rows those hooks really write come back coherent over the wire.
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


# ---------------------------------------------------------------------------
# 1. The screen has teeth
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 1b. The null hint's rule is asymmetric, and that is the point (WO-R2-158)
# ---------------------------------------------------------------------------


def test_an_unclassified_row_may_carry_a_permanent_fault_text() -> None:
    """The rule used to be "the text must not imply a class", which made
    the one pair an escalation drill needs unrepresentable.

    A hint is a classification; an error text is the symptom the failing
    code recorded. "Nobody has classified this" does not disagree with
    "the symptom is a bad row in the stored payload" — that text is the
    evidence a triage pass would read to *reach* `human_required`, and it
    points away from a replay, which is where a null hint already sits.
    It is also the normal state of an organically dead-lettered job on
    this platform, where LLM triage is off by default.

    RED before WO-R2-158: reported "implies a class" and the story below
    could not exist.
    """
    reasons = coherence_violations(
        None,
        "ValueError: invalid literal for int() with base 10: 'N/A' at "
        "row 8,214",
    )
    assert not reasons, reasons


def test_the_unclassified_bad_data_pair_is_an_entry_in_the_table() -> None:
    """Not merely admissible — declared, so a reader of the table can see
    which text `create_bad_data_job` stamps for an unclassified row
    instead of finding it composed at the call site."""
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
    """`poison_message`'s default story (WO-R2-166). Same shape as the
    bad-data variant above and there for the same reason: the text a hook
    stamps is declared in the table, not composed at the call site.

    A schema violation under a null hint is coherent for exactly the
    reason the bad-data one is — the text is a symptom, the hint is a
    classification, and "nobody classified this" does not contradict "the
    payload is missing a required field". What it emphatically does not
    say is that a replay would help.
    """
    pinned = story("unclassified_schema_missing_field")
    assert pinned.hint is None
    assert "SchemaValidationError" in pinned.error_message
    assert "missing required field" in pinned.error_message
    assert not coherence_violations(pinned.hint, pinned.error_message)
    assert pinned.triage is None
    assert not triage_violations(pinned.hint, pinned.triage)
    assert pinned in DLQ_FAILURE_STORIES[None]


def test_the_two_schema_stories_are_distinguishable() -> None:
    """`SCHEMA_MISSING_FIELD` is verbatim from live run efdc3b2a9864 and is
    pinned as "the exact pair that shipped". The unclassified variant names
    a different field so a reader sweeping a queue can tell a poisoned row
    from a seeded one, and so a test asserting on the shipped string cannot
    match this one by accident."""
    shipped = story("schema_missing_field")
    variant = story("unclassified_schema_missing_field")
    assert shipped.error_message != variant.error_message
    assert LIVE_DEFECT_ERROR not in variant.error_message


def test_the_unclassified_default_still_says_nothing_about_its_class() -> None:
    """Adding the bad-data variant must not move what a writer given only
    "uncategorised" stamps. Element 0 of the null tuple stays the
    worker-exit text, so `default_error_for(None)` is unchanged."""
    assert default_error_for(None) == story("unclassified_worker_exit").error_message
    assert story("unclassified_worker_exit").error_message != story(
        "unclassified_csv_bad_row"
    ).error_message


def test_a_null_hint_still_refuses_a_triage_block() -> None:
    """The half of the null-hint rule that did NOT loosen."""
    assert triage_violations(None, story("csv_bad_row").triage)


# ---------------------------------------------------------------------------
# 2. The table itself
# ---------------------------------------------------------------------------


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
    """Including `None`. A null hint is the case the tools describe as
    UNKNOWN-and-not-replay-safe, so it needs a text that stays silent
    about its class rather than no text at all."""
    assert DLQ_FAILURE_STORIES[hint]
    assert default_error_for(hint) == story_for(hint).error_message


def test_the_hint_vocabulary_matches_the_enum() -> None:
    """A hint added to `RemediationHint` without a story would leave a
    writer with nothing coherent to stamp, and `default_error_for`
    raises rather than inventing one — so this fails first, here."""
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


# ---------------------------------------------------------------------------
# 3. Every writer
# ---------------------------------------------------------------------------


def _every_writer_pair() -> list[tuple[str | None, str]]:
    """(hint, error_message) for every value the lab's writers stamp.

    Read off the writers, not off the table — a writer that stops
    consulting the table has to show up here as an incoherent pair, not
    as a passing test about the table it no longer uses.
    """
    pairs: list[tuple[str | None, str]] = []

    # `seed_dlq_messages` and `create_stuck_dag`: both take a hint and
    # substitute the canonical text when `error_message` is omitted, so
    # the pair is (each accepted hint, what the omission resolves to).
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

    # `create_bad_data_job`: two declarable hints since WO-R2-158, each
    # resolving its own text when `error_message` is omitted. Read through
    # the hook's own resolver rather than the table, so a hook that starts
    # composing its strings by hand shows up here as an incoherent pair.
    for declared in ("human_required", "unclassified"):
        inp = CreateBadDataJobInput(remediation_hint=declared)  # type: ignore[arg-type]
        assert inp.error_message is None
        hint = _declared_hint(inp.remediation_hint)
        pairs.append((hint, _default_error_for(hint)))

    # `poison_message`: two declarable hints since WO-R2-166, each
    # resolving its own schema-violation text, which the hook then wraps
    # with the topic. Neither is `replay_safe` — that is the whole change,
    # and the walk reads it off the hook so a regression that restored the
    # old hint shows up here as an incoherent pair rather than as a passing
    # test about a table the hook no longer consults.
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
    # seed_dlq_messages/create_stuck_dag's three hints + create_bad_data_job's
    # two declarable hints + poison_message's two + the seeded pack.
    #
    # `create_mislabeled_dlq_job` is deliberately NOT in this walk: its whole
    # output is the one sanctioned incoherent pair, so adding it here would
    # turn a passing suite red for the fixture working as designed. It is
    # covered instead by the section below, which asserts the screen still
    # flags it.
    assert len(pairs) == 3 + 2 + 2 + len(seed._dlq_specs())
    # Both of `create_bad_data_job`'s hints are actually in the walk — the
    # unclassified one is the pair WO-R2-158 added, so a regression that
    # dropped the argument would show up as a shrinking count above and as
    # a missing null hint here.
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
    """Whatever else moved, the row stays traceable to this hook and to
    the topic it poisoned — that is how a human sweeping the DLQ joins the
    row back to the Kafka half of the same invocation."""
    text = _dlq_error_for_topic("job.submitted", hint)
    assert "job.submitted" in text
    assert "poison_message" in text


@pytest.mark.parametrize(
    "hint", [None, RemediationHint.HUMAN_REQUIRED.value]
)
def test_poison_message_row_describes_the_fault_it_injects(
    hint: str | None,
) -> None:
    """WO-R2-166. The hook publishes a schema-invalid payload, so its
    dead-letter row says so under both hints it accepts.

    RED before the fix in two different ways, both wrong: the original
    text was the schema violation under a `replay_safe` hint (incoherent,
    and the live defect of WO-R2-146); the WO-R2-146 repair swapped the
    text for `UpstreamTimeout …`, which was coherent and still false —
    it described a transient fault this hook never injects.
    """
    text = _dlq_error_for_topic("job.submitted", hint)
    assert "SchemaValidationError" in text
    assert "missing required field" in text
    assert not coherence_violations(hint, text), text


def test_poison_message_cannot_be_asked_for_a_replay_safe_row() -> None:
    """The narrow structural statement of the fix: `replay_safe` is not in
    the hook's input vocabulary at all, so no argument produces it.

    Asserted on the input model rather than on a handler run because this
    is a schema property — an agent reading `inputSchema` sees the same
    two words, and a widening of the `Literal` fails here before any test
    that needs a broker.
    """
    with pytest.raises(ValidationError):
        PoisonMessageInput(
            topic="job.submitted",
            remediation_hint=RemediationHint.REPLAY_SAFE.value,  # type: ignore[arg-type]
        )
    # On the accepted *values*, not on the rendered blob: the description
    # names `replay_safe` on purpose, to tell an agent reading the schema
    # that the row it might have expected is not on offer here.
    schema = PoisonMessageInput.model_json_schema()
    field = schema["properties"]["remediation_hint"]
    accepted = {
        value
        for branch in field["anyOf"]
        for value in branch.get("enum", branch.get("const", []) or [])
    }
    assert accepted == {"human_required", "unclassified"}, json.dumps(field)


def test_poison_message_defaults_to_unclassified() -> None:
    """A freshly poisoned message has been classified by nobody, because
    LLM triage is off by default here. Omitting the field must therefore
    give a NULL hint, not the old `replay_safe` and not `human_required`
    (which would say a triage pass already ran)."""
    assert PoisonMessageInput(topic="job.submitted").remediation_hint == (
        "unclassified"
    )
    assert _declared_hint(
        PoisonMessageInput(topic="job.submitted").remediation_hint
    ) is None


# ---------------------------------------------------------------------------
# 3b. The one sanctioned incoherent pair (WO-R2-166)
# ---------------------------------------------------------------------------


def test_the_sanctioned_pair_is_still_reported_as_incoherent() -> None:
    """The fixture for "the classifier lied" is a row whose hint its own
    text contradicts. The screen must keep saying so.

    This is the test that stops the exception becoming a loophole: a
    change that taught `coherence_violations` to accept `replay_safe`
    beside a permanent-fault text would be WO-R2-146 reintroduced, and it
    would pass every other test in this file.
    """
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
    """`test_no_writer_still_stamps_the_pair_that_shipped` promises that
    the exact live-defect text is never paired with `replay_safe` again.
    That promise stays absolute only if this fixture uses a different
    permanent text — so it uses the CSV bad-row one."""
    lie = sanctioned_incoherent_story()
    assert "SchemaValidationError" not in lie.error_message
    assert lie.error_message == story("csv_bad_row").error_message


def test_the_sanctioned_pair_carries_no_triage_block() -> None:
    """One lie per fixture. `is_retryable=True` beside a bad-data text
    would be a second, different contradiction and would muddy what the
    scenario measures."""
    lie = sanctioned_incoherent_story()
    assert lie.triage is None


# ---------------------------------------------------------------------------
# 4. The seeded pack is drawn from the table, not written beside it
# ---------------------------------------------------------------------------


def test_seeded_pack_texts_come_from_the_table() -> None:
    for spec in seed._dlq_specs():
        pinned = story(str(spec["story_key"]))
        assert spec["error_message"] == pinned.error_message
        assert spec["triage"] == seed._triage_from(pinned)
        assert pinned.hint == spec.get("remediation_hint")


def test_seeded_pack_shape_is_unchanged() -> None:
    """Ids, types, hints and order are pinned by scenario YAML and by
    the commander's canned fixtures. WO-R2-146 moves the texts and
    nothing else, so this is the half that must not have changed."""
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
