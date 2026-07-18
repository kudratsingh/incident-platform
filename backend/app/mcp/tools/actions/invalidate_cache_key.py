"""
`invalidate_cache_key` — delete one Redis key.

Guardrail: only keys under the allowlisted prefixes below can be
deleted. This is a load-bearing check — an unrestricted DEL against
a shared Redis is an availability-affecting action; scoping the
tool to cache-y prefixes keeps it safe by construction. Adding a
new prefix is a code change and a PR review.

`actions:execute` + idempotent.
"""

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Prefixes the tool is allowed to delete. Anything else is refused
# with a validation error before the Redis call runs.
_ALLOWED_PREFIXES = (
    "cache:",
    "jobs:cache:",
    "kafka:consumer_lag:",  # metrics-loop cache; safe to force refresh
    "read_model:",  # CQRS read-model sets; consumers repopulate
)


class InvalidateCacheKeyError(AppError):
    status_code = 400
    error_code = "cache_key_forbidden"


class InvalidateCacheKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        min_length=1,
        max_length=512,
        description="Exact Redis key to delete. Must start with one of "
        f"{list(_ALLOWED_PREFIXES)}.",
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class InvalidateCacheKeyOutput(BaseModel):
    key: str
    deleted: bool = Field(
        description="True if a key existed and was deleted. False if "
        "the key wasn't in Redis at call time (harmless)."
    )


@tool(
    "invalidate_cache_key",
    description=(
        "Delete one Redis key (allowlisted prefixes only). Use to "
        "force refresh of a stale cache after fixing the underlying "
        "data. Idempotent — a follow-up call finds nothing to delete "
        "and returns `deleted=false`."
    ),
    input_model=InvalidateCacheKeyInput,
    output_model=InvalidateCacheKeyOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def invalidate_cache_key(
    inp: InvalidateCacheKeyInput, ctx: ToolContext
) -> InvalidateCacheKeyOutput:
    if not any(inp.key.startswith(p) for p in _ALLOWED_PREFIXES):
        raise InvalidateCacheKeyError(
            f"Key {inp.key!r} is not under an allowlisted prefix. "
            f"Allowed: {list(_ALLOWED_PREFIXES)}"
        )

    deleted_count = await ctx.redis.delete(inp.key)
    deleted = bool(deleted_count)
    logger.warning(
        "action invalidate_cache_key",
        extra={"key": inp.key, "deleted": deleted},
    )
    return InvalidateCacheKeyOutput(key=inp.key, deleted=deleted)
