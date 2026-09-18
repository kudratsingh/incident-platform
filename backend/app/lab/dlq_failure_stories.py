"""The one table of dead-letter failure stories the lab may stamp on a row.

Live run `efdc3b2a9864` shipped a `replay_safe` row whose `error_message` was a
permanent schema fault, and graded the agent's correct escalation as a failure
(WO-R2-146). Hence the rule this module holds, as code in
`coherence_violations()` and walked by `tests/unit/test_dlq_text_coherence.py`:

    a lab row's error text must describe a failure of the same *kind* its
    remediation hint prescribes an action for.

  * `replay_safe`     — transient, nothing left behind; replay now.
  * `wait_and_replay` — a dependency shedding load; replay after a delay.
  * `human_required`  — bad data or a schema violation in the stored payload.
  * `None`            — nothing has classified it. Asymmetric: the text may
                        name a permanent fault (that agrees), but must not
                        invite a replay, and may never carry a triage block.

The markers are a small closed vocabulary on purpose — a story that trips the
screen gets reworded, the screen does not get loosened. Each hint maps to a
tuple of stories, element 0 the canonical default. `MISLABELED_BAD_DATA` is the
one sanctioned incoherent pair (see its own comment). Nothing here is
production behaviour; `remediation_hint` stays the only source of truth.
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

    `list_dlq_messages` returns it inline, so it obeys the same coherence rule
    as the error text. `suggested_fix` names no tool on purpose — a fixture
    that whispers a Tier-1 call grades the whisper, not the agent.
    """

    root_cause_category: str
    summary: str
    suggested_fix: str
    is_retryable: bool
    confidence: float


@dataclass(frozen=True)
class DlqFailureStory:
    """One coherent (error text, triage) pair for one remediation hint;
    `key` pins a variant so a writer names the story instead of an index."""

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
    # No triage on purpose: a null hint means nothing classified this, and a
    # triage block is a classification.
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
    # No triage, same reason, and the point of this variant: the text is a
    # symptom a reader can act on while the hint column stays empty.
    # `create_bad_data_job` pins it by key for the escalation drill
    # (`dlq_human_required_escalates`).
    triage=None,
)


UNCLASSIFIED_SCHEMA_MISSING_FIELD = DlqFailureStory(
    key="unclassified_schema_missing_field",
    hint=None,
    error_message=(
        "SchemaValidationError: payload missing required field 'job_id' "
        "(received keys: []) — rejected on attempt 3/3 and the failure "
        "has not been categorised"
    ),
    # `poison_message`'s default story: that hook publishes a schema-invalid
    # payload, so a schema violation is the honest symptom (it used to read
    # "UpstreamTimeout …" under `replay_safe` — screened clean and still
    # false). Null hint because a poisoned message arrives unclassified. A
    # different field from `SCHEMA_MISSING_FIELD` on purpose: that text is
    # verbatim from run efdc3b2a9864 and pinned. No triage block.
    triage=None,
)


# --------------------------------------------------------------------------
# The one sanctioned incoherent pair — declared here, kept out of the table
# --------------------------------------------------------------------------

MISLABELED_BAD_DATA = DlqFailureStory(
    key="mislabeled_bad_data",
    hint=RemediationHint.REPLAY_SAFE.value,
    error_message=CSV_BAD_ROW.error_message,
    # DELIBERATELY INCOHERENT, and `coherence_violations` must keep reporting
    # it: a `remediation_hint` can be wrong in production, and an agent that
    # trusts the column over the text it contradicts has to be measurable.
    # Reachable only through `sanctioned_incoherent_story()`, written only by
    # `create_mislabeled_dlq_job` under `mislabel: true`. Its text is
    # `CSV_BAD_ROW`'s, so the run efdc3b2a9864 string stays paired with
    # `human_required` alone. No triage block: `is_retryable=True` here would
    # be a second lie.
    triage=None,
)


# Element 0 of each tuple is the canonical default for that hint.
# `MISLABELED_BAD_DATA` is deliberately not a member of any tuple below.
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
            # Element 0 stays the story that says nothing about its class —
            # `default_error_for(None)` must keep returning it. The two
            # permanent-fault variants are reached by key.
            None: (
                UNCLASSIFIED_WORKER_EXIT,
                UNCLASSIFIED_CSV_BAD_ROW,
                UNCLASSIFIED_SCHEMA_MISSING_FIELD,
            ),
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
    """A hint with no story. Raised rather than returning a fallback text of
    unknown class."""


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
    """The canonical error text for `hint` — the one-line call every fixture
    writer makes."""
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


def sanctioned_incoherent_story() -> DlqFailureStory:
    """The one pair this table declares and the coherence screen refuses.

    Named rather than a bare constant so `grep sanctioned_incoherent_story`
    enumerates every writer that may stamp a lie — one,
    `create_mislabeled_dlq_job`. `coherence_violations` on it is non-empty.
    """
    return MISLABELED_BAD_DATA


# --------------------------------------------------------------------------
# The coherence rule
# --------------------------------------------------------------------------

# "Baked into the stored payload, recurs identically" — a row with one of
# these cannot honestly be replayable.
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

    Reasons rather than a bool so a failing test names the contradiction."""
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
        # Asymmetric on purpose (module docstring): a null hint is UNKNOWN and
        # not replay-safe, so only a text inviting a replay contradicts it.
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
    """Every way a triage block contradicts `hint`; `is_retryable` is a
    second, blunter statement of what the hint says."""
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
