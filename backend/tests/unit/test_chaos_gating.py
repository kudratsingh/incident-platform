"""Unit tests for the chaos framework gates from ADR 0008."""

from unittest.mock import patch

import pytest
from app.config import Settings, assert_chaos_gate


def test_gate_allows_chaos_disabled_in_production() -> None:
    """Baseline — production without chaos is fine."""
    settings = Settings(environment="production", chaos_enabled=False)
    assert_chaos_gate(settings)  # no raise


def test_gate_allows_chaos_enabled_in_staging() -> None:
    """Chaos in non-prod is the intended path."""
    settings = Settings(environment="staging", chaos_enabled=True)
    assert_chaos_gate(settings)  # no raise


def test_gate_refuses_chaos_enabled_in_production() -> None:
    """The load-bearing check — the whole ADR 0008 argument."""
    settings = Settings(environment="production", chaos_enabled=True)
    with pytest.raises(RuntimeError, match="Chaos tools must never"):
        assert_chaos_gate(settings)


def test_chaos_tool_decorator_no_ops_when_disabled() -> None:
    """When CHAOS_ENABLED=false the @chaos_tool decorator does not
    register anything — the tool literally cannot be called."""
    from app.mcp import chaos as chaos_mod
    from app.mcp.registry import _restore_for_tests, _snapshot_for_tests
    from pydantic import BaseModel

    class _In(BaseModel):
        pass

    class _Out(BaseModel):
        ok: bool = True

    snap = _snapshot_for_tests()
    _restore_for_tests({})
    try:
        with patch.object(
            chaos_mod, "get_settings", return_value=Settings(chaos_enabled=False)
        ):

            @chaos_mod.chaos_tool(
                "test_probe_off",
                description="probe",
                input_model=_In,
                output_model=_Out,
                blast_radius=chaos_mod.BlastRadius.SINGLE_CONSUMER,
            )
            async def _probe(inp, ctx):
                return _Out()

        from app.mcp.registry import get_tool

        assert get_tool("test_probe_off") is None, (
            "chaos_tool must be a no-op when CHAOS_ENABLED=false"
        )
    finally:
        _restore_for_tests(snap)


def test_chaos_tool_decorator_registers_when_enabled() -> None:
    from app.mcp import chaos as chaos_mod
    from app.mcp.registry import (
        _restore_for_tests,
        _snapshot_for_tests,
        get_tool,
    )
    from pydantic import BaseModel

    class _In(BaseModel):
        pass

    class _Out(BaseModel):
        ok: bool = True

    snap = _snapshot_for_tests()
    _restore_for_tests({})
    try:
        with patch.object(
            chaos_mod, "get_settings", return_value=Settings(chaos_enabled=True)
        ):

            @chaos_mod.chaos_tool(
                "test_probe_on",
                description="probe",
                input_model=_In,
                output_model=_Out,
                blast_radius=chaos_mod.BlastRadius.SINGLE_CONSUMER,
            )
            async def _probe(inp, ctx):
                return _Out()

        td = get_tool("test_probe_on")
        assert td is not None
        assert td.is_chaos is True
        # Blast radius label is prepended to the description so the
        # agent sees it in tools/list.
        assert "[chaos: single_consumer]" in td.description
        # Chaos tools always require chaos:invoke, even if the caller
        # accidentally omitted the parameter.
        from app.core.scopes import Scope

        assert td.required_scope == Scope.CHAOS_INVOKE
    finally:
        _restore_for_tests(snap)
