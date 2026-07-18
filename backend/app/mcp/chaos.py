"""
Chaos framework — the three gates from ADR 0008 rolled into one decorator.

Registration path:
  1. `CHAOS_ENABLED` env flag → if false, `@chaos_tool` is a no-op; the
     tool never enters the registry, so `tools/list` never shows it and
     `tools/call` returns MCP_TOOL_NOT_FOUND. The agent literally cannot
     see chaos exists in a chaos-disabled environment.
  2. Scope check (`chaos:invoke`) is done by the standard tool dispatch
     path in `handlers.py` — same layer, same error shape as any other
     scope failure.
  3. The `@chaos_tool` decorator can optionally enforce a per-tool
     blast-radius label (informational for now; Wave 2 wires alarms on
     the rate of high-blast-radius invocations).

Every chaos invocation is audited to a separate action name
(`chaos.tool_invoked`) so operators can filter it independently — this
routing happens in `handlers.py` based on the `is_chaos` flag on the
tool definition.
"""

from collections.abc import Callable
from enum import StrEnum

from app.config import get_settings
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolHandler, tool
from pydantic import BaseModel

logger = get_logger(__name__)


class BlastRadius(StrEnum):
    """Coarse categorization of what a chaos tool can affect. Used
    today only as an audit-log field and a startup-time filter; Wave 2
    will wire alarms on the rate of higher-blast invocations."""

    SINGLE_CONSUMER = "single_consumer"
    SINGLE_SERVICE = "single_service"
    SHARED_DEPENDENCY = "shared_dependency"
    ENVIRONMENT_WIDE = "environment_wide"


def chaos_tool[InputT: BaseModel, OutputT: BaseModel](
    name: str,
    *,
    description: str,
    input_model: type[InputT],
    output_model: type[OutputT],
    blast_radius: BlastRadius,
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a chaos tool. No-op when `CHAOS_ENABLED=false`.

    Chaos tools always require `chaos:invoke` — that's non-negotiable.
    Blast-radius classification is stored on the tool definition (via
    the description for now; Wave 2 promotes it to a first-class field
    when we start alarming on it).
    """
    settings = get_settings()
    if not settings.chaos_enabled:
        logger.info(
            "chaos tool skipped (CHAOS_ENABLED=false)",
            extra={"tool": name, "blast_radius": blast_radius.value},
        )

        def _noop(func: ToolHandler) -> ToolHandler:
            return func

        return _noop

    logger.info(
        "chaos tool registered",
        extra={"tool": name, "blast_radius": blast_radius.value},
    )
    # Prepend blast radius to the description so `tools/list` surfaces
    # it — the agent's LLM should know what a call actually does.
    full_description = (
        f"[chaos: {blast_radius.value}] {description}"
    )
    return tool(
        name,
        description=full_description,
        input_model=input_model,
        output_model=output_model,
        required_scope=Scope.CHAOS_INVOKE,
        is_chaos=True,
    )


def is_chaos_enabled() -> bool:
    """Public predicate — used by tests + operational logging."""
    return get_settings().chaos_enabled


__all__: list[str] = [
    "BlastRadius",
    "chaos_tool",
    "is_chaos_enabled",
]
