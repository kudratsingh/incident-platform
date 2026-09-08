"""The one table of dead-letter failure stories the lab may stamp on a row.

A dead-lettered job the agent can see carries two things that say what
went wrong: `jobs.remediation_hint` — the coarse category its remediation
logic branches on — and `jobs.error_message`, the free text a planner
actually reads. `list_dlq_messages` returns both, plus the `job_triages`
row when one exists, in the same response.

Before this table they disagreed. Live run `efdc3b2a9864` (2026-09-07)
put the `remediate_runaway_saga_success` agent in front of a chain whose
dead-lettered root was stamped `remediation_hint=replay_safe` and

    SchemaValidationError: payload missing required field 'user_id'
    (received keys: ['tenant_id', 'action', 'ts'])

The agent read the row, judged that a payload missing a required field
will fail the same way on every attempt, and escalated rather than
replaying. That is what an operator should do with that text. The hint
said the opposite, and the hint was the truth — in the lab no processor
validates payloads, so the error string was decoration. Nothing on the
wire told the agent that, and nothing could: a self-contradictory fixture
grades sound reasoning as failure. WO-R2-146.

So the rule this module exists to hold:

    a lab row's error text must describe a failure of the same *kind* its
    remediation hint prescribes an action for.

  * `replay_safe`      — a transient fault that left nothing behind.
                         Replaying now is the whole fix.
  * `wait_and_replay`  — a dependency shedding load or refusing
                         connections. Replaying now burns another
                         attempt; replaying after a delay works.
  * `human_required`   — bad data or a schema violation baked into the
                         stored payload. Every replay fails identically.
  * `None`             — nothing has classified this failure. The text
                         must not invite a replay, because a null hint
                         means unknown and *not* replay-safe.

That last bullet used to read "the text must not imply a class either".
That was too broad in one direction, and the over-reach was load-bearing
rather than cosmetic: it made the pair an escalation drill actually needs
— an *unclassified* row whose error text a human can read and act on —
representable nowhere in this table. The rule for a null hint is
asymmetric, because the harm is:

  * A null-hint row whose text reads transient ("timed out, nothing was
    committed") or backoff ("429, retry-after 120s") is incoherent. Every
    tool description says a null hint is UNKNOWN and explicitly not
    replay-safe, so a text that says "replaying is the fix" is telling
    the agent to do the thing the missing classification does not
    authorise.
  * A null-hint row whose text names a permanent data fault is coherent.
    A hint is a *classification*; an error text is the *symptom* the
    failing code recorded. "Nobody has classified this" and "the symptom
    is a bad row in the payload" do not disagree — the text is precisely
    the evidence a triage pass (or an operator) would read to reach
    `human_required`, and it points away from a replay, which is where a
    null hint already sits. Refusing that pair would be refusing the
    normal state of an organically dead-lettered job on a stack with LLM
    triage switched off, which is this platform's default.

So `coherence_violations` screens a null hint for backoff and transient
markers and admits permanent ones. What a null hint may never carry is a
`job_triages` row — that block *is* a classification — and
`triage_violations` still refuses it.

`coherence_violations()` is that rule as code, and every writer's pairs
are walked through it by `tests/unit/test_dlq_text_coherence.py`. The
markers are deliberately a small closed vocabulary rather than a
classifier: the point is that a human adding a story can see from this
file which words decide its class. A new story whose wording trips the
screen gets reworded — the screen does not get loosened.

Each hint maps to a *tuple* of stories rather than one string. Two
distinct `wait_and_replay` fixtures (an unreachable SMTP relay, a
rate-limited partner API) are more useful to read than the same sentence
twice, and both are coherent. Element 0 is the canonical default: it is
what a writer given only a hint stamps.

Nothing here is production behaviour. The strings are never parsed to
decide anything at runtime — `remediation_hint` remains the only source
of truth for routing, exactly as `RemediationHint`'s own docstring says.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.models.enums import RemediationHint

# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DlqTriage:
    """The `job_triages` row that goes with a story.

    `list_dlq_messages` returns this block inline with the entry, so it is
    as agent-visible as `error_message` and belongs to the same coherence
    rule: a `replay_safe` row whose triage says "fix the producer, then
    replay" contradicts its hint just as loudly as the error text does.

    `suggested_fix` deliberately names no tool. Steering the agent toward
    a specific Tier-1 call from fixture data would grade tool choice on
    what the fixture whispered rather than on what the agent concluded,
    and several scenarios forbid the very tools such a whisper would
    suggest.
    """

    root_cause_category: str
    summary: str
    suggested_fix: str
    is_retryable: bool
    confidence: float


@dataclass(frozen=True)
class DlqFailureStory:
    """One coherent (error text, triage) pair for one remediation hint.

    `key` is how a caller pins a specific variant — the eval seeder names
    the story each of its rows uses, so a reader of the seeder can see
    which of a hint's stories that row tells without counting tuple
    indices.
    """

    key: str
    hint: str | None
    error_message: str
    triage: DlqTriage | None = None


# --------------------------------------------------------------------------
# The stories
# --------------------------------------------------------------------------

UPSTREAM_TIMEOUT = DlqFailureStory(
    key="upstream_timeout",
    hint=RemediationHint.REPLAY_SAFE.value,
    error_message=(
        "UpstreamTimeout: bulk_api_sync POST "
        "https://partner-api.internal/v2/sync timed out after 30s on "
        "attempt 3/3 — the request was never acknowledged, so no "
        "downstream write was recorded"
    ),
    triage=DlqTriage(
        root_cause_category="upstream_timeout",
        summary=(
            "partner-api.internal did not answer inside the 30s deadline "
            "on any of the three attempts. The payload was accepted by "
            "validation and nothing was committed downstream."
        ),
        suggested_fix=(
            "Re-run the job as it stands: the call is idempotent, no "
            "partial write survived the timeout, and a fresh attempt is "
            "the entire remedy. Neither the payload nor the producer "
            "needs changing."
        ),
        is_retryable=True,
        confidence=0.88,
    ),
)

PARTNER_RATE_LIMITED = DlqFailureStory(
    key="partner_rate_limited",
    hint=RemediationHint.WAIT_AND_REPLAY.value,
    error_message=(
        "RateLimited: partner-api.internal answered 429 Too Many "
        "Requests on attempt 3/3 (retry-after: 120s); the per-tenant "
        "quota window has not rolled over yet"
    ),
    triage=DlqTriage(
        root_cause_category="rate_limited",
        summary=(
            "All three attempts landed inside one quota window. The "
            "partner is shedding load, not rejecting the payload."
        ),
        suggested_fix=(
            "Let the 120s retry-after window pass before the next "
            "attempt. An immediate re-run spends another attempt against "
            "the same closed window and comes back 429."
        ),
        is_retryable=True,
        confidence=0.86,
    ),
)

SMTP_UNREACHABLE = DlqFailureStory(
    key="smtp_unreachable",
    hint=RemediationHint.WAIT_AND_REPLAY.value,
    error_message=(
        "send_email downstream call failed: "
        "ConnectionRefusedError('smtp.mailer.internal:587')"
    ),
    triage=DlqTriage(
        root_cause_category="downstream_unavailable",
        summary="SMTP relay unreachable — connection refused at TCP layer.",
        suggested_fix=(
            "Check smtp.mailer.internal ECS task health; recent billing "
            "hotfix (v0.4.2) may have changed VPC egress rules — "
            "cross-reference `get_deploy_history`. Re-run once the "
            "dependency answers."
        ),
        is_retryable=True,
        confidence=0.82,
    ),
)

SCHEMA_MISSING_FIELD = DlqFailureStory(
    key="schema_missing_field",
    hint=RemediationHint.HUMAN_REQUIRED.value,
    error_message=(
        "SchemaValidationError: payload missing required field "
        "'user_id' (received keys: ['tenant_id', 'action', 'ts'])"
    ),
    triage=DlqTriage(
        root_cause_category="schema_violation",
        summary=(
            "The producer omitted user_id. All three attempts failed on "
            "the same missing field, and the stored payload still lacks "
            "it."
        ),
        suggested_fix=(
            "Not recoverable as stored — a fresh attempt fails on the "
            "same field. The producer has to be corrected to include "
            "user_id and re-emit the work; a person owns that change."
        ),
        is_retryable=False,
        confidence=0.91,
    ),
)

CSV_BAD_ROW = DlqFailureStory(
    key="csv_bad_row",
    hint=RemediationHint.HUMAN_REQUIRED.value,
    error_message=(
        "ValueError: invalid literal for int() with base 10: "
        "'not-a-number' at row 15,382"
    ),
    triage=DlqTriage(
        root_cause_category="bad_input",
        summary="Non-numeric value in a supposedly-integer CSV column.",
        suggested_fix=(
            "Not retryable — data quality issue. Notify the uploader; "
            "add a validation step in the CSV importer that fails the "
            "whole upload with a clear error rather than half-processing."
        ),
        is_retryable=False,
        confidence=0.94,
    ),
)

UNCLASSIFIED_WORKER_EXIT = DlqFailureStory(
    key="unclassified_worker_exit",
    hint=None,
    error_message=(
        "WorkerExit: the processor handling this job stopped on attempt "
        "3/3 without recording an outcome; no exception was captured and "
        "the failure has not been categorised"
    ),
    # No triage row on purpose. A null hint means nothing has classified
    # this failure, and a triage block *is* a classification — writing
    # one would contradict the hint the same way a mismatched error text
    # does.
    triage=None,
)

UNCLASSIFIED_CSV_BAD_ROW = DlqFailureStory(
    key="unclassified_csv_bad_row",
    hint=None,
    error_message=(
        "ValueError: invalid literal for int() with base 10: 'N/A' in "
        "column 'quantity' at row 8,214 of 12,000 — csv_upload aborted "
        "on attempt 3/3"
    ),
    # Same reason as the story above, and the reason this variant exists:
    # a triage block would classify the row, and the whole point of it is
    # that nothing has. The text is a symptom a reader can act on; the
    # hint column is still empty, so deciding what to do with the row is
    # work the reader has to do. `create_bad_data_job` pins this story by
    # key for the escalation drill (`dlq_human_required_escalates`), where
    # the agent has to read the error, fence the row itself, and escalate
    # — none of which is measurable against a row that arrived already
    # stamped `human_required`.
    triage=None,
)


# Element 0 of each tuple is the canonical default for that hint.
DLQ_FAILURE_STORIES: Mapping[str | None, tuple[DlqFailureStory, ...]] = (
    MappingProxyType(
        {
            RemediationHint.REPLAY_SAFE.value: (UPSTREAM_TIMEOUT,),
            RemediationHint.WAIT_AND_REPLAY.value: (
                PARTNER_RATE_LIMITED,
                SMTP_UNREACHABLE,
            ),
            RemediationHint.HUMAN_REQUIRED.value: (
                SCHEMA_MISSING_FIELD,
                CSV_BAD_ROW,
            ),
            # Element 0 stays the story that says nothing at all about
            # its class: that is what a writer given only "uncategorised"
            # should stamp, and `default_error_for(None)` must keep
            # returning it. The bad-data variant is reached by key, by the
            # one hook that wants an unclassified row a reader can
            # actually act on.
            None: (UNCLASSIFIED_WORKER_EXIT, UNCLASSIFIED_CSV_BAD_ROW),
        }
    )
)

ALL_STORIES: tuple[DlqFailureStory, ...] = tuple(
    story for stories in DLQ_FAILURE_STORIES.values() for story in stories
)

STORIES_BY_KEY: Mapping[str, DlqFailureStory] = MappingProxyType(
    {story.key: story for story in ALL_STORIES}
)


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


class UnknownDlqHintError(KeyError):
    """A hint with no story. Raised rather than returning a fallback text:
    a fallback would be a string of unknown class stamped on a row of
    known class, which is the defect this module exists to prevent."""


def stories_for(hint: str | None) -> tuple[DlqFailureStory, ...]:
    """Every coherent story for `hint`, canonical default first."""
    try:
        return DLQ_FAILURE_STORIES[hint]
    except KeyError:
        known = ", ".join(
            repr(h) for h in DLQ_FAILURE_STORIES if h is not None
        )
        raise UnknownDlqHintError(
            f"no failure story for remediation_hint {hint!r}; "
            f"known hints: {known}, or None for uncategorised"
        ) from None


def story_for(hint: str | None) -> DlqFailureStory:
    """The canonical story for `hint` — what a writer given only a hint
    stamps."""
    return stories_for(hint)[0]


def default_error_for(hint: str | None) -> str:
    """The canonical error text for `hint`.

    The one-line call every fixture writer makes. Replaces the per-module
    error tables that drifted out of agreement with each other and with
    the hints they were written beside."""
    return story_for(hint).error_message


def story(key: str) -> DlqFailureStory:
    """One story by name, for a caller pinning a specific variant."""
    try:
        return STORIES_BY_KEY[key]
    except KeyError:
        known = ", ".join(sorted(STORIES_BY_KEY))
        raise UnknownDlqHintError(
            f"no failure story keyed {key!r}; known: {known}"
        ) from None


# --------------------------------------------------------------------------
# The coherence rule
# --------------------------------------------------------------------------

# Words that say "this failure is baked into the stored payload and will
# recur identically". A row carrying one of these cannot honestly be
# replayable.
PERMANENT_MARKERS: tuple[str, ...] = (
    "schemavalidationerror",
    "valueerror",
    "invalid literal",
    "missing required field",
    "malformed",
    "corrupt",
)

# Words that say "the dependency is refusing work right now" — the signal
# that separates "replay after a delay" from "replay immediately".
BACKOFF_MARKERS: tuple[str, ...] = (
    "429",
    "retry-after",
    "ratelimited",
    "rate limited",
    "connectionrefusederror",
    "connection refused",
    "serviceunavailable",
    "503",
)

# Words that say "something went wrong in flight and left nothing
# behind".
TRANSIENT_MARKERS: tuple[str, ...] = (
    "timed out",
    "timeouterror",
    "upstreamtimeout",
    "connectionreseterror",
    "reset by peer",
)


def _present(markers: tuple[str, ...], text: str) -> list[str]:
    lowered = text.lower()
    return [m for m in markers if m in lowered]


def coherence_violations(hint: str | None, error_message: str) -> list[str]:
    """Every way `error_message` contradicts `hint`. Empty means coherent.

    Returns reasons rather than a bool so a failing table test names the
    contradiction instead of only its existence — the original defect was
    invisible for a month because nothing ever said the two fields
    disagreed.
    """
    permanent = _present(PERMANENT_MARKERS, error_message)
    backoff = _present(BACKOFF_MARKERS, error_message)
    transient = _present(TRANSIENT_MARKERS, error_message)
    reasons: list[str] = []

    if hint == RemediationHint.REPLAY_SAFE.value:
        if permanent:
            reasons.append(
                "replay_safe row reads as a permanent data fault "
                f"({', '.join(permanent)}) — a replay cannot fix it"
            )
        if backoff:
            reasons.append(
                "replay_safe row reads as a dependency refusing work "
                f"({', '.join(backoff)}) — that is wait_and_replay"
            )
        if not transient:
            reasons.append(
                "replay_safe row names no transient fault, so nothing in "
                "the text says a replay would behave differently"
            )
    elif hint == RemediationHint.WAIT_AND_REPLAY.value:
        if permanent:
            reasons.append(
                "wait_and_replay row reads as a permanent data fault "
                f"({', '.join(permanent)}) — waiting changes nothing"
            )
        if not backoff:
            reasons.append(
                "wait_and_replay row names nothing that would clear on "
                "its own, so the text does not say why to wait"
            )
    elif hint == RemediationHint.HUMAN_REQUIRED.value:
        if not permanent:
            reasons.append(
                "human_required row names no permanent fault, so the "
                "text reads as something a replay could fix"
            )
        if transient or backoff:
            reasons.append(
                "human_required row reads as a transient fault "
                f"({', '.join(transient + backoff)}) — that invites a "
                "replay the hint forbids"
            )
    elif hint is None:
        # Asymmetric on purpose — see the module docstring. A null hint is
        # UNKNOWN and explicitly not replay-safe, so the contradiction is
        # a text that says a replay (now, or after a wait) is the remedy.
        # A text naming a permanent data fault agrees with the null hint
        # about the only thing the null hint asserts: don't replay this.
        routable = transient + backoff
        if routable:
            reasons.append(
                "uncategorised row's text reads as a fault a replay would "
                f"clear ({', '.join(routable)}) while the hint says "
                "nothing has classified it — a null hint is UNKNOWN and "
                "explicitly not replay-safe, so a text that invites a "
                "replay contradicts it"
            )
    else:
        reasons.append(f"unknown remediation_hint {hint!r}")

    return reasons


def triage_violations(
    hint: str | None, triage: DlqTriage | None
) -> list[str]:
    """Every way a triage block contradicts `hint`. Empty means coherent.

    `list_dlq_messages` returns the triage row inline, so `is_retryable`
    is a second, blunter statement of the same thing the hint says.
    """
    reasons: list[str] = []
    if hint is None:
        if triage is not None:
            reasons.append(
                "uncategorised row carries a triage classification, "
                "which is itself a category"
            )
        return reasons
    if triage is None:
        return reasons
    retryable_hints = (
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
    )
    if hint in retryable_hints and not triage.is_retryable:
        reasons.append(
            f"{hint} row's triage says is_retryable=False"
        )
    if hint == RemediationHint.HUMAN_REQUIRED.value and triage.is_retryable:
        reasons.append("human_required row's triage says is_retryable=True")
    return reasons
