"""
`invalidate_cache_key` — delete one Redis key.

Two load-bearing guardrails. *Namespace*: only the allowlisted prefixes below, since
an unrestricted DEL on a shared Redis affects availability. *Tenant* (R2-54): the
allowlist never said the key was *yours*, so the tenant segment comes from the
authenticated principal (`app/mcp/tools/_cache_scope.py`, shared with
`get_cache_key_info`). `actions:execute` + idempotent.
"""

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools._cache_scope import assert_key_in_tenant
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Prefixes the tool may delete; anything else is refused before the Redis call.
# BAKED VERBATIM into the inputSchema and the commander's pinned snapshot, so
# editing it is contract drift (E2-02).
_ALLOWED_PREFIXES = (
    # Read-through caches: the per-job cache and the eval hot_set fixture.
    "cache:",
    # Synthetic; only the `create_stale_cache` chaos hook writes it, and its mirror
    # must stay a SUBSET of this tuple (tests/unit/test_cache_key_allowlist.py).
    "jobs:cache:",
    "kafka:consumer_lag:",  # metrics-loop cache; safe to force refresh
    # Also synthetic. Do NOT add the real CQRS sets (`jobs:tenant:*` / `jobs:user:*`)
    # — they are projections, so a deleted set never repopulates and stats undercount.
    "read_model:",
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
        f"{list(_ALLOWED_PREFIXES)}. Tenant-scoped keys "
        "(`cache:job:{tenant_id}:{job_id}`) are deletable only within the "
        "calling principal's own tenant.",
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
        "Delete one Redis key (allowlisted prefixes only, and within "
        "your own tenant). Use to force refresh of a stale cache after "
        "fixing the underlying data. Idempotent — a follow-up call finds "
        "nothing to delete and returns `deleted=false`."
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
    # Second gate, what makes the first sufficient: the allowlist says this is a
    # platform cache namespace, this says the entry is the caller's (R2-54).
    assert_key_in_tenant(
        inp.key,
        tenant_id=ctx.principal.tenant_id,
        error=InvalidateCacheKeyError,
    )

    deleted_count = await ctx.redis.delete(inp.key)
    deleted = bool(deleted_count)
    logger.warning(
        "action invalidate_cache_key",
        extra={"key": inp.key, "deleted": deleted},
    )
    return InvalidateCacheKeyOutput(key=inp.key, deleted=deleted)
