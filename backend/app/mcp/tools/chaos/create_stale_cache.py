"""
`create_stale_cache` — populate a Redis cache key with obviously-fake
stale content so the `remediate_stale_cache_success` scenario has a
pre-condition to observe + invalidate on the live evaluation path.

Boot-time seeding of the same key lives in
`scripts/seed_eval_fixtures.py::_seed_hot_set` — that's what makes the
scenario winnable on a fresh compose stack. This chaos hook is the
per-scenario counterpart, invoked from the commander's `chaos_setup`
hook so each scenario declaratively owns its own pre-condition (see
commander PR #54 for the scenario-side hook shape).

Compensator: `invalidate_cache_key` (Tier-1 action) — the scenario's
success path IS the compensation call. Round-trip test:
`test_create_stale_cache_round_trip_with_invalidate_cache_key`
in `tests/api/test_mcp_wave2_chaos_hooks.py`.

Every write is bounded by TTL (default 600s / 10 minutes, max 1 hour)
so a forgotten cleanup self-clears without operator action. Doesn't
touch Kafka or Postgres — pure Redis.

Chaos-only surface: gated behind `CHAOS_ENABLED=true` + `chaos:invoke`
scope + `environment_wide` blast radius label. See ADR 0008 gating.
"""

import json
import uuid

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# The scenario's canonical hot_set key. Boot-time seeding writes the
# same key from `scripts/seed_eval_fixtures.py`; keeping the constants
# aligned across the two files is a review-time check (both cite this
# ADR/hook pair, so any future rename shows up in both).
_DEFAULT_HOT_SET_KEY = "cache:jobs:worker-dispatcher:hot_set"

# Must be under one of `invalidate_cache_key`'s allowlisted prefixes so
# the compensator can actually clear anything this hook writes. Mirror
# of `backend/app/mcp/tools/actions/invalidate_cache_key.py::_ALLOWED_PREFIXES`.
_ALLOWED_PREFIXES = ("cache:", "jobs:cache:", "read_model:")


class CreateStaleCacheError(AppError):
    status_code = 400
    error_code = "stale_cache_key_forbidden"


class CreateStaleCacheInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(
        default=_DEFAULT_HOT_SET_KEY,
        min_length=1,
        max_length=512,
        description=(
            "Redis key to populate. Must start with one of "
            f"{list(_ALLOWED_PREFIXES)} so `invalidate_cache_key` can "
            "serve as the compensator (its allowlist would refuse "
            "arbitrary keys). Default is the hot_set key the "
            "`remediate_stale_cache_success` scenario reads."
        ),
    )
    stale_count: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Number of fake stale entries in the JSON array "
        "written to the key.",
    )
    ttl_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="TTL on the cache key — no permanent damage. "
        "Default 600s (10min), max 1h.",
    )


class CreateStaleCacheOutput(BaseModel):
    key: str
    size_bytes: int
    ttl_seconds: int
    accepted: bool


@chaos_tool(
    "create_stale_cache",
    description=(
        "Populate a Redis cache key with obviously-fake stale content "
        "so the `remediate_stale_cache_success` scenario has a "
        "condition to observe and invalidate. Compensator is "
        "`invalidate_cache_key` — the scenario's success action IS the "
        "cleanup. Value is a JSON array of fabricated IDs so an "
        "operator inspecting Redis doesn't confuse this with real "
        "cache. Bounded TTL (max 1h) so a forgotten cleanup "
        "self-clears."
    ),
    input_model=CreateStaleCacheInput,
    output_model=CreateStaleCacheOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def create_stale_cache(
    inp: CreateStaleCacheInput, ctx: ToolContext
) -> CreateStaleCacheOutput:
    if not any(inp.key.startswith(p) for p in _ALLOWED_PREFIXES):
        raise CreateStaleCacheError(
            f"Key {inp.key!r} is not under an allowlisted prefix. "
            f"Allowed: {list(_ALLOWED_PREFIXES)}. `invalidate_cache_key` "
            "(the compensator) would refuse to clear a key outside "
            "these prefixes, so the round-trip would be broken."
        )
    fake_ids = [
        f"stale-fixture-{uuid.uuid4().hex[:12]}"
        for _ in range(inp.stale_count)
    ]
    payload = json.dumps(fake_ids)
    await ctx.redis.set(inp.key, payload, ex=inp.ttl_seconds)

    size = len(payload.encode())
    logger.warning(
        "chaos create_stale_cache populated",
        extra={
            "key": inp.key,
            "size_bytes": size,
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return CreateStaleCacheOutput(
        key=inp.key,
        size_bytes=size,
        ttl_seconds=inp.ttl_seconds,
        accepted=True,
    )
