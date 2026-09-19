"""Registry-level enforcement of ADR 0012 rule 1 — "non-chaos tools never
name chaos internals".

ADR 0012 shipped rule 1 as a single payload assertion on a single tool
(`restart_consumer_group`), and its own "A rule that now needs enforcing"
section names the durable fix: a registry-level test asserting no non-chaos
tool's schema or description contains chaos vocabulary. This is that test.

Scope of the screen: every string a `telemetry:read` / `incidents:read` /
`actions:*` principal can pull off the wire from `tools/list` — the tool
description plus the full `inputSchema` and `outputSchema` JSON (Pydantic
`Field(description=...)` text is serialized into those schemas, so a leak
can hide in a field doc just as easily as in the tool description).

The guard keys on `required_scope != Scope.CHAOS_INVOKE` rather than on
`is_chaos`, so the screen still holds in a chaos-enabled context where the
chaos tools are registered. Under default unit settings `CHAOS_ENABLED` is
false and the chaos tools are not registered at all.

Word-list discipline (ADR 0012, and the rule this test encodes): if a future
legitimate wording trips the screen, reword the description — do not weaken
the pattern. The leading `\b` and the closed inflection set already spare
words like "evaluate" and "retrieval"; the ban is on the lab's own
vocabulary, in whatever form it is written (WO-R2-62: the screen used to
match singular stems only, so `fixtures` and `scenarios` walked through it).
"""

import json
import re

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.scopes import Scope
from app.mcp.registry import ToolDefinition, list_tools

# Vocabulary that names the test apparatus rather than the system under
# test. Case-insensitive, anchored at the start of a word, and tolerant of
_LAB_STEMS = ("chaos", "eval", "seed", "fixture", "harness", "scenario")
_INFLECTIONS = r"(?:e?s|ed|ing)?"

LAB_VOCABULARY = re.compile(
    rf"\b(?:{'|'.join(_LAB_STEMS)}){_INFLECTIONS}\b",
    re.IGNORECASE,
)


def _wire_surface(td: ToolDefinition) -> str:
    """Every free-text thing `tools/list` serializes for one tool."""
    return "\n".join(
        [
            td.description,
            str(td.required_scope.value if td.required_scope else ""),
            json.dumps(td.input_json_schema(), sort_keys=True),
            json.dumps(td.output_json_schema(), sort_keys=True),
        ]
    )


def test_no_lab_vocabulary_on_non_chaos_tool_surface() -> None:
    offenders: dict[str, list[str]] = {}

    for td in list_tools():
        if td.required_scope == Scope.CHAOS_INVOKE:
            # Chaos tools ARE the chaos surface — they may name it freely.
            continue
        hits = sorted({m.group(0) for m in LAB_VOCABULARY.finditer(_wire_surface(td))})
        if hits:
            offenders[td.name] = hits

    assert offenders == {}, (
        "Lab vocabulary leaked onto the non-chaos tools/list wire surface "
        f"(ADR 0012 rule 1): {offenders}. Reword the description or Field "
        "text — do not weaken the screen."
    )


def test_screen_is_live_and_covers_the_read_surface() -> None:
    """Guard against the screen silently passing because it inspected
    nothing: the registry must be populated and the surface non-empty."""
    tools = [t for t in list_tools() if t.required_scope != Scope.CHAOS_INVOKE]
    assert len(tools) >= 10
    assert all(_wire_surface(t).strip() for t in tools)


def test_screen_catches_a_planted_leak() -> None:
    """The pattern itself is load-bearing — prove it fires."""
    assert LAB_VOCABULARY.search("Coarse category set by triage / seed / chaos.")
    assert LAB_VOCABULARY.search("plus 7 eval-seed groups")
    assert LAB_VOCABULARY.search("Seed job. Its parents")
    # ...and does not fire on ordinary incident-response prose.
    assert LAB_VOCABULARY.search("evaluate the retrieval latency") is None


@pytest.mark.parametrize(
    "leak",
    [
        "Ids of the seeded fixtures to replay.",
        "One of the rehearsal scenarios.",
        "Counts across all evals in the window.",
        "Job ids used as seeds for the run.",
        "Currently seeding the DLQ.",
        "Both harnesses report the same lag.",
        "Chaos tools are gated.",
        "A fixture job id.",
    ],
)
def test_screen_catches_inflected_forms(leak: str) -> None:
    """Plurals and participles are the forms a description actually uses."""
    assert LAB_VOCABULARY.search(leak), f"screen missed lab vocabulary in: {leak!r}"


@pytest.mark.parametrize(
    "innocent",
    [
        "evaluate the retrieval latency",
        "Evaluation of the request is deferred",
        "seedling",
        "Chaotic ordering is not a failure mode",
        "the scenario_id is absent",
    ],
)
def test_screen_spares_ordinary_prose(innocent: str) -> None:
    """The suffix set must not extend a stem into an unrelated word."""
    assert LAB_VOCABULARY.search(innocent) is None, (
        f"screen fired on ordinary prose: {innocent!r}"
    )
