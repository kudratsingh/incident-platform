"""
Chaos framework — ADR 0008's three gates rolled into one decorator.

`CHAOS_ENABLED=false` makes `@chaos_tool` a no-op, so the tool never enters the
registry and the agent cannot see chaos exists. The `chaos:invoke` scope check and
the routing of the audit row to `chaos.tool_invoked` both happen in `handlers.py`.
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
    """Coarse categorization of what a chaos tool can affect; an audit-log field."""

    SINGLE_CONSUMER = "single_consumer"
    #: Narrower than SINGLE_CONSUMER, added for `pause_control_loop` (ADR 0027):
    #: a background loop is one coroutine, not a whole Kafka subscription.
    SINGLE_LOOP = "single_loop"
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
    """Register a chaos tool; a no-op when `CHAOS_ENABLED=false`. Always needs
    `chaos:invoke`."""
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
