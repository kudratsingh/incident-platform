"""The `tools/list` delta for WO-R3-267 is exactly one tool, exactly two fields."""

from __future__ import annotations

import json

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
from app.mcp.registry import list_tools
from app.mcp.tools.cache_key_info import (
    GetCacheKeyInfoInput,
    GetCacheKeyInfoOutput,
)

#: The two fields this work order adds. Named once, used by every assertion
#: below, so the delta cannot be widened in one place and pinned in another.
NEW_OUTPUT_FIELDS = ("records_referenced", "records_found")


def test_the_output_shape_delta_is_exactly_these_two_fields() -> None:
    assert list(GetCacheKeyInfoOutput.model_fields) == [
        "key",
        "exists",
        "type",
        "ttl_seconds",
        "size",
        *NEW_OUTPUT_FIELDS,
    ]


def test_the_input_shape_is_unchanged() -> None:
    """One argument, the key. The check is not opt-in."""
    assert list(GetCacheKeyInfoInput.model_fields) == ["key"]


def test_both_new_fields_are_nullable_integers() -> None:
    """`null` is the only honest answer where the platform cannot resolve
    an entry's references, so neither field may be a plain `int` — and a
    default of 0 would read as a finding rather than an absence of one."""
    schema = GetCacheKeyInfoOutput.model_json_schema()
    for name in NEW_OUTPUT_FIELDS:
        prop = schema["properties"][name]
        types = {branch.get("type") for branch in prop["anyOf"]}
        assert types == {"integer", "null"}, name
        assert prop.get("default") is None, name


def test_no_defs_entry_is_added_to_the_output_schema() -> None:
    """Scalars, not a nested model: nothing new to resolve on the wire."""
    assert "$defs" not in GetCacheKeyInfoOutput.model_json_schema()


def test_no_other_tool_grows_these_fields() -> None:
    """The delta is one tool. Anything else advertising these names would
    be a second entry in the rebless ledger that nobody wrote down."""
    carriers = sorted(
        td.name
        for td in list_tools()
        if any(
            name
            in json.dumps(
                [td.input_json_schema(), td.output_json_schema()], sort_keys=True
            )
            for name in NEW_OUTPUT_FIELDS
        )
    )
    assert carriers == ["get_cache_key_info"]


def test_the_description_says_what_the_counts_do_not_prove() -> None:
    """A claim about what a reading means is held to the same bar as the
    reading itself (the `poison_message` lesson). Equal counts must not be
    left to read as "this copy is current"."""
    td = next(t for t in list_tools() if t.name == "get_cache_key_info")
    assert "not a claim that the copied" in td.description
    assert "null is not zero" in td.description
