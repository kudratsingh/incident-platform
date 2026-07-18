"""
Tool registry — decorator-based, module-level.

Every tool is a coroutine that takes a Pydantic input model + a
`ToolContext` (session, redis, principal) and returns a Pydantic
output model. The decorator captures the tool's name, description,
required scope, and JSON Schema derived from the input model.

Registration is side-effect-driven: `app/mcp/tools/__init__.py`
imports every tool module and the decorators fire at import time,
populating `_REGISTRY`. The MCP handlers then read from it.
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
    """Everything a tool handler needs at call time. Kept as a dataclass
    so tools can accept it by keyword and only touch what they use.

    The MCP handler builds one per invocation from the request's DB
    session, Redis client, and authenticated principal — no globals."""

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
    # Chaos tools use a distinct audit action (`chaos.tool_invoked` vs
    # `agent.tool_invoked`) and are only registered when
    # `settings.chaos_enabled=True`. The dispatch layer reads this flag
    # to route the audit row into the chaos stream.
    is_chaos: bool = False
    # Tier 1 action tools require an `idempotency_key` argument. The
    # dispatch layer looks it up before invoking the handler; a repeat
    # call with the same (tenant, principal, key) returns the cached
    # response verbatim rather than re-running the tool.
    is_idempotent: bool = False

    def input_json_schema(self) -> dict[str, Any]:
        """JSON Schema for the input model, emitted verbatim in
        `tools/list`. Pydantic renders `$defs` etc. cleanly, so MCP
        clients get the same schema Pydantic uses for validation."""
        return self.input_model.model_json_schema()


_REGISTRY: dict[str, ToolDefinition] = {}


def tool[InputT: BaseModel, OutputT: BaseModel](
    name: str,
    *,
    description: str,
    input_model: type[InputT],
    output_model: type[OutputT],
    required_scope: Scope | None = None,
    is_chaos: bool = False,
    is_idempotent: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator: register `func` as a tool.

    Duplicate names raise at import time so a collision surfaces on the
    first test run rather than at first invocation. Tools that don't
    require a scope pass `required_scope=None` (e.g. protocol-level
    handlers like `initialize`, which don't go through this registry
    anyway — this escape hatch is for future no-scope tools like a
    health probe)."""

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
