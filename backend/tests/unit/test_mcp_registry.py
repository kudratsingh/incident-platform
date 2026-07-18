"""Unit tests for the MCP tool registry — decorator captures scope +
schema, duplicate registration errors, list_tools sorts stably."""

import uuid
from unittest.mock import AsyncMock

import pytest
from app.core.scopes import Scope
from app.mcp.registry import (
    ToolContext,
    _restore_for_tests,
    _snapshot_for_tests,
    get_tool,
    list_tools,
    tool,
)
from pydantic import BaseModel


class _EmptyIn(BaseModel):
    pass


class _EmptyOut(BaseModel):
    ok: bool


@pytest.fixture(autouse=True)
def _clean_registry():  # type: ignore[no-untyped-def]
    """Save the registry, run the test with a blank slate, restore. This
    way test-registered probe tools don't leak into other test modules,
    and real tools (registered on import) still exist afterward."""
    snapshot = _snapshot_for_tests()
    _restore_for_tests({})
    yield
    _restore_for_tests(snapshot)


async def test_decorator_registers_tool_with_scope() -> None:
    @tool(
        "probe",
        description="probe",
        input_model=_EmptyIn,
        output_model=_EmptyOut,
        required_scope=Scope.TELEMETRY_READ,
    )
    async def _probe(inp: _EmptyIn, ctx: ToolContext) -> _EmptyOut:
        return _EmptyOut(ok=True)

    td = get_tool("probe")
    assert td is not None
    assert td.name == "probe"
    assert td.required_scope == Scope.TELEMETRY_READ
    assert td.input_model is _EmptyIn
    assert td.output_model is _EmptyOut


async def test_input_schema_is_json_schema() -> None:
    @tool(
        "probe",
        description="probe",
        input_model=_EmptyIn,
        output_model=_EmptyOut,
    )
    async def _probe(inp: _EmptyIn, ctx: ToolContext) -> _EmptyOut:
        return _EmptyOut(ok=True)

    schema = get_tool("probe").input_json_schema()  # type: ignore[union-attr]
    assert schema["type"] == "object"
    assert "properties" in schema


async def test_duplicate_registration_raises() -> None:
    @tool(
        "probe",
        description="probe",
        input_model=_EmptyIn,
        output_model=_EmptyOut,
    )
    async def _first(inp: _EmptyIn, ctx: ToolContext) -> _EmptyOut:
        return _EmptyOut(ok=True)

    with pytest.raises(ValueError, match="already registered"):

        @tool(
            "probe",
            description="probe",
            input_model=_EmptyIn,
            output_model=_EmptyOut,
        )
        async def _second(inp: _EmptyIn, ctx: ToolContext) -> _EmptyOut:
            return _EmptyOut(ok=True)


async def test_list_tools_is_sorted() -> None:
    for n in ("zebra", "alpha", "mango"):
        @tool(
            n,
            description=n,
            input_model=_EmptyIn,
            output_model=_EmptyOut,
        )
        async def _t(inp: _EmptyIn, ctx: ToolContext) -> _EmptyOut:  # noqa: B023
            return _EmptyOut(ok=True)

    names = [t.name for t in list_tools()]
    assert names == ["alpha", "mango", "zebra"]


async def test_tool_context_carries_deps() -> None:
    from app.dependencies import Principal

    principal = Principal(kind="user", tenant_id=uuid.uuid4())
    ctx = ToolContext(db=AsyncMock(), redis=AsyncMock(), principal=principal)
    assert ctx.principal is principal
