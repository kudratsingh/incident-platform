"""
Commander-telemetry tools — the one family of tools a model never chooses (ADR 0035).

`@commander_tool` sits beside `@chaos_tool` and does the same one job: it stamps a
recognisable prefix onto the description so a caller can tell this family apart from
the tools it is meant to reason with. `[chaos: <blast_radius>]` marks a hook the
evaluator fires; `[commander: telemetry]` marks a call the responder's own loop makes
at a fixed point in its cycle, reporting what it is doing so a human can watch.

Two consequences worth stating, because both are load-bearing:

- **It is not a read.** These tools write and answer with a receipt. There is no read
  tool for `agent_runs` at all, so the prefix marks a surface the caller cannot use to
  learn anything it did not already know.
- **It is filtered out of the planner, by the prefix.** The commander drops
  `[commander:` tools from the tool list it offers its model, exactly as it drops
  `[chaos:` ones — so the model neither sees these tools nor spends a budgeted call on
  them. The platform's half of that contract is that the prefix is stable and that no
  read-scoped tool ever carries it, both pinned by test.
"""

from collections.abc import Callable

from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolDefinition, ToolHandler, tool
from pydantic import BaseModel

logger = get_logger(__name__)

#: The marker the commander's planner filter matches on. One family, one label — there
#: is no second kind of commander telemetry, so this is a constant rather than an enum.
COMMANDER_DESCRIPTION_PREFIX = "[commander: telemetry] "


def commander_tool[InputT: BaseModel, OutputT: BaseModel](
    name: str,
    *,
    description: str,
    input_model: type[InputT],
    output_model: type[OutputT],
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a commander-telemetry tool: prefixed description, `agent_runs:write`.

    Always that scope — the family is defined by what it writes, so a member that
    needed a different one would not be a member.
    """
    logger.info("commander telemetry tool registered", extra={"tool": name})
    return tool(
        name,
        description=f"{COMMANDER_DESCRIPTION_PREFIX}{description}",
        input_model=input_model,
        output_model=output_model,
        required_scope=Scope.AGENT_RUNS_WRITE,
        is_commander=True,
    )


def is_commander_tool(definition: ToolDefinition) -> bool:
    """The predicate every census uses. Reads the registry flag, and the prefix is
    asserted to agree with it — two ways to say one thing, so a tool cannot carry the
    label without the behaviour or the behaviour without the label."""
    return definition.is_commander


__all__ = [
    "COMMANDER_DESCRIPTION_PREFIX",
    "commander_tool",
    "is_commander_tool",
]
