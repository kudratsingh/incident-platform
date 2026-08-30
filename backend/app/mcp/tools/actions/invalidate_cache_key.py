"""
`invalidate_cache_key` — delete one Redis key.

Two guardrails, and both are load-bearing.

*Namespace*: only keys under the allowlisted prefixes below can be
deleted. An unrestricted DEL against a shared Redis is an
availability-affecting action; scoping the tool to cache-y prefixes
keeps it safe by construction. Adding a new prefix is a code change and
a PR review.

*Tenant* (R2-54): the allowlist says a key is a platform cache
namespace, not that it is *yours*. `cache:job:{tenant}:{job_id}` is
deliberately deletable — force-refreshing a stale job read is what this
tool is for — so without a tenant check a service account in one tenant
could evict another tenant's cached reads. The tenant segment comes from
the authenticated principal; see `app/mcp/tools/_cache_scope.py`, shared
with `get_cache_key_info` so the two cannot drift.

`actions:execute` + idempotent.
"""

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools._cache_scope import assert_key_in_tenant
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# Prefixes the tool is allowed to delete. Anything else is refused
# with a validation error before the Redis call runs.
#
# The tuple is BAKED VERBATIM into the tool's inputSchema (see the `key`
# field description below) and therefore into the commander's pinned
# contract snapshot. Editing it is contract drift — if a platform key
# needs to become reachable, rename the KEY under `cache:` instead (that
# is what `cache:job:{tenant}:{job_id}` is, E2-02).
_ALLOWED_PREFIXES = (
    # Real read-through caches. Covers the per-job read cache
    # (`cache:job:{tenant_id}:{job_id}`, app/utils/cache.py) and the
    # eval hot_set fixture.
    "cache:",
    # Synthetic fixture namespace: nothing in the platform writes it.
    # Reachable only via the `create_stale_cache` chaos hook, whose
    # mirror list (chaos/create_stale_cache.py::_ALLOWED_PREFIXES) must
    # stay a SUBSET of this tuple so the compensator can always clear
    # what the hook wrote (asserted in tests/unit/test_cache_key_allowlist.py).
    "jobs:cache:",
    "kafka:consumer_lag:",  # metrics-loop cache; safe to force refresh
    # Also synthetic (same chaos-hook-only reachability). It does NOT
    # match any key the platform writes: the real CQRS read-model sets
    # are `jobs:tenant:*` / `jobs:user:*`, and they are projections, not
    # caches — ReadModelProjector only moves ids on lifecycle events, so
    # a deleted set never fully repopulates and admin stats silently
    # undercount. Do not add those prefixes here; repairing a projection
    # needs a rebuild tool, not DEL.
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
    # Second gate, and the one that makes the first sufficient: the
    # allowlist says this is a platform cache namespace, this says the
    # entry is the caller's. `cache:job:{tenant}:{job}` is deliberately
    # deletable, so without it one tenant's service account could evict
    # another tenant's cached reads (R2-54).
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
