"""
Tool registry — decorator-based, module-level.

A tool is a coroutine taking a Pydantic input model plus a `ToolContext` and
returning a Pydantic output model. Importing `app/mcp/tools` fires the decorators
at import time and fills `_REGISTRY`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.scopes import Scope
from app.dependencies import Principal
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs at call time; built per invocation, no
    globals."""

    db: AsyncSession
    redis: Redis
    principal: Principal


ToolHandler = Callable[[Any, ToolContext], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    required_scope: Scope | None
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    # Audit row goes to `chaos.tool_invoked`, not `agent.tool_invoked`.
    # Set only when `settings.chaos_enabled=True`.
    is_chaos: bool = False
    # Commander telemetry (ADR 0035): the responder's loop reports its own run
    # here, no model chooses the call, and the audit row goes to
    # `agent.run_reported` so the action stream stays what the agent *did*.
    is_commander: bool = False
    # Tier 1 actions take an `idempotency_key`; a repeat on the same
    # (tenant, principal, key) returns the cached response.
    is_idempotent: bool = False

    def input_json_schema(self) -> dict[str, Any]:
        """JSON Schema for the input model, emitted verbatim in `tools/list`."""
        return self.input_model.model_json_schema()

    def output_json_schema(self) -> dict[str, Any]:
        """JSON Schema for the output model, emitted in `tools/list` alongside
        `inputSchema` since v0.4.8. A platform extension, not in the MCP spec."""
        return self.output_model.model_json_schema()


_REGISTRY: dict[str, ToolDefinition] = {}


def tool[InputT: BaseModel, OutputT: BaseModel](
    name: str,
    *,
    description: str,
    input_model: type[InputT],
    output_model: type[OutputT],
    required_scope: Scope | None = None,
    is_chaos: bool = False,
    is_commander: bool = False,
    is_idempotent: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """Register `func` as a tool. Duplicate names raise at import time, so a
    collision surfaces on the first test run rather than at first call."""

    def _decorator(func: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise ValueError(f"Tool already registered: {name}")
        _REGISTRY[name] = ToolDefinition(
            name=name,
            description=description,
            required_scope=required_scope,
            input_model=input_model,
            output_model=output_model,
            handler=func,
            is_chaos=is_chaos,
            is_commander=is_commander,
            is_idempotent=is_idempotent,
        )
        return func

    return _decorator


def get_tool(name: str) -> ToolDefinition | None:
    return _REGISTRY.get(name)


def list_tools() -> list[ToolDefinition]:
    """Sorted by name so `tools/list` is stable across restarts."""
    return sorted(_REGISTRY.values(), key=lambda t: t.name)


def _snapshot_for_tests() -> dict[str, ToolDefinition]:
    """Test-only escape hatch — snapshot the registry so a test can
    scribble on it and restore afterward."""
    return dict(_REGISTRY)


def _restore_for_tests(snapshot: dict[str, ToolDefinition]) -> None:
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


__all__ = [
    "ToolContext",
    "ToolDefinition",
    "_restore_for_tests",
    "_snapshot_for_tests",
    "get_tool",
    "list_tools",
    "tool",
]
