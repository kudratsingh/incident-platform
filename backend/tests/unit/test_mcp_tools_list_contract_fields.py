"""`tools/list` advertises required_scope and is_idempotent (WO-R2-32).

Both fields were registry-only, so `tools/list` described a tool's shape
(`inputSchema`/`outputSchema`) but not its *contract*. A change to either —
re-scoping a tool, or silently dropping its idempotency — was invisible to
the commander's contract snapshot and therefore uncatchable by the contract
test. `is_idempotent` is the one that bites: it is what makes a Tier-1
recovery re-invoke return the cached response, so a silent drop turns the
retry into a real second execution returning a different payload, which
verification reads as a spurious escalation.

The point of the change is the diff, so the tests are about the diff. The
snapshot helper here mirrors what the commander's `_tool_view` pins; the
commander half lands with the re-pin and rebless order, not here.

`handle_tools_list` is exercised directly rather than over HTTP because its
return value *is* the wire payload — it hands back an already-`model_dump`ed
result — so this covers serialization without coupling to auth fixtures.
"""

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import app.mcp.tools  # noqa: F401 — import fires every @tool decorator
import pytest
from app.core.scopes import ALL_SCOPES
from app.mcp.handlers import handle_tools_list
from app.mcp.registry import (
    _restore_for_tests,
    _snapshot_for_tests,
    get_tool,
)

CONTRACT_FIELDS = ("required_scope", "is_idempotent")


def _tools_list_payload() -> list[dict[str, Any]]:
    resp = handle_tools_list(request_id=1)
    assert resp.result is not None
    tools: list[dict[str, Any]] = resp.result["tools"]
    assert tools, "registry is empty — the tools package did not import"
    return tools


def _contract_snapshot() -> dict[str, dict[str, Any]]:
    """What the commander pins, reduced to the fields under test.

    Deliberately built from the `tools/list` *response* rather than from the
    registry: pinning the registry would make the test pass even if the
    handler never advertised the fields, which is the exact bug.
    """
    return {
        t["name"]: {f: t.get(f) for f in CONTRACT_FIELDS}
        for t in _tools_list_payload()
    }


@pytest.fixture
def restore_registry() -> Iterator[None]:
    """Let a test scribble on the registry and put it back afterward."""
    snap = _snapshot_for_tests()
    try:
        yield
    finally:
        _restore_for_tests(snap)


def _override(name: str, **changes: Any) -> None:
    """Replace one tool definition in place. `ToolDefinition` is frozen, so
    this is how a test simulates a contract change upstream."""
    snap = _snapshot_for_tests()
    snap[name] = dataclasses.replace(snap[name], **changes)
    _restore_for_tests(snap)


# ---------------------------------------------------------------------------
# The fields are advertised, for every tool, with the registry's values
# ---------------------------------------------------------------------------


def test_every_tool_advertises_both_contract_fields() -> None:
    """THE assertion for R2-32. Red before: neither key is present at all."""
    missing = [
        f"{t['name']}: {f}"
        for t in _tools_list_payload()
        for f in CONTRACT_FIELDS
        if f not in t
    ]
    assert missing == []


def test_advertised_values_match_the_registry() -> None:
    """The handler reports what the registry holds, not a default."""
    for entry in _tools_list_payload():
        definition = get_tool(entry["name"])
        assert definition is not None
        expected_scope = (
            definition.required_scope.value
            if definition.required_scope is not None
            else None
        )
        assert entry["required_scope"] == expected_scope, entry["name"]
        assert entry["is_idempotent"] == definition.is_idempotent, entry["name"]


def test_the_advertised_set_is_not_degenerate() -> None:
    """Anti-vacuity guard.

    A handler that emitted the keys but never populated them would still
    satisfy "the key exists". At least one real tool is idempotent and at
    least one carries a scope, so if either group comes back empty the
    fields are being defaulted rather than read.
    """
    tools = _tools_list_payload()
    assert any(t["is_idempotent"] for t in tools)
    assert any(t["required_scope"] is not None for t in tools)


def test_required_scope_is_a_bare_scope_string() -> None:
    """Serialized as the scope literal, never an enum repr — otherwise the
    commander's snapshot diffs on serialization instead of on the scope."""
    for entry in _tools_list_payload():
        scope = entry["required_scope"]
        if scope is not None:
            assert isinstance(scope, str)
            assert scope in ALL_SCOPES, entry["name"]
            assert "Scope." not in scope


def test_payload_is_json_serializable() -> None:
    """`tools/list` crosses the wire as JSON; an enum member would raise."""
    json.dumps(_tools_list_payload())


# ---------------------------------------------------------------------------
# The point of the change: a contract change is now a snapshot diff
# ---------------------------------------------------------------------------


def test_flipping_is_idempotent_produces_a_snapshot_diff(
    restore_registry: None,
) -> None:
    """The assertion the work order is actually about.

    Red before: both snapshots are identical, because `tools/list` never
    mentioned `is_idempotent` — a tool silently losing its idempotency
    guarantee produced no diff for the contract test to catch.
    """
    before = _contract_snapshot()
    target = next(t["name"] for t in _tools_list_payload() if t["is_idempotent"])

    _override(target, is_idempotent=False)
    after = _contract_snapshot()

    assert after != before
    assert before[target]["is_idempotent"] is True
    assert after[target]["is_idempotent"] is False
    # Nothing else moved — the diff points at the tool that changed.
    assert {k for k in before if before[k] != after[k]} == {target}


def test_flipping_required_scope_produces_a_snapshot_diff(
    restore_registry: None,
) -> None:
    """Same for a re-scoped tool: previously invisible, now a diff."""
    from app.core.scopes import Scope

    before = _contract_snapshot()
    target = next(
        t["name"]
        for t in _tools_list_payload()
        if t["required_scope"] == Scope.TELEMETRY_READ.value
    )

    _override(target, required_scope=Scope.ACTIONS_EXECUTE)
    after = _contract_snapshot()

    assert after != before
    assert after[target]["required_scope"] == Scope.ACTIONS_EXECUTE.value
    assert {k for k in before if before[k] != after[k]} == {target}


def test_dropping_a_scope_entirely_produces_a_snapshot_diff(
    restore_registry: None,
) -> None:
    """The worst case — a tool losing its scope requirement — is a diff too,
    and serializes as a real null rather than a missing key."""
    before = _contract_snapshot()
    target = next(
        t["name"] for t in _tools_list_payload() if t["required_scope"] is not None
    )

    _override(target, required_scope=None)
    after = _contract_snapshot()

    assert after != before
    assert after[target]["required_scope"] is None
    assert "required_scope" in _contract_snapshot()[target]


# ---------------------------------------------------------------------------
# Additive-only: the pre-existing surface is untouched
# ---------------------------------------------------------------------------


def test_change_is_additive_only() -> None:
    """The commander re-pins at wave 9, so the existing keys must not move.
    Only the two new ones may appear."""
    expected = {
        "name",
        "description",
        "inputSchema",
        "outputSchema",
        "required_scope",
        "is_idempotent",
    }
    for entry in _tools_list_payload():
        assert set(entry) == expected, entry["name"]


def test_existing_fields_still_populated() -> None:
    """Guards against a refactor that adds the new fields but disturbs the
    four the commander is already pinned to."""
    for entry in _tools_list_payload():
        assert entry["name"]
        assert entry["description"]
        assert entry["inputSchema"]["type"] == "object"
        assert isinstance(entry["outputSchema"], dict)
