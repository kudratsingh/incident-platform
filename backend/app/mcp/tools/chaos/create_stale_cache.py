"""`create_stale_cache` — put obviously-fake stale content in a Redis cache key
so `remediate_stale_cache_success` has a condition to observe and invalidate.

Boot seeding of the same key is `seed_eval_fixtures.py::_seed_hot_set`; this is
the per-scenario counterpart. The compensator is `invalidate_cache_key` — the
scenario's success path IS the cleanup. TTL-bounded; pure Redis; ADR 0008 gated.
"""

import json
import uuid

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

# The scenario's canonical hot_set key, also written by
# `scripts/seed_eval_fixtures.py` — keep the two aligned.
_DEFAULT_HOT_SET_KEY = "cache:jobs:worker-dispatcher:hot_set"

# Mirrors `invalidate_cache_key`'s allowlist so the compensator can
# clear anything this hook writes.
_ALLOWED_PREFIXES = ("cache:", "jobs:cache:", "read_model:")

# Carved back out of `cache:` (R2-20): `cache:job:{tenant}:{job}` is the live
# per-job read cache behind `GET /jobs/{id}`, which this hook's JSON array
# would break for the whole TTL. Deny-inside-allow keeps the subset invariants
# in `test_cache_key_allowlist.py` meaningful;
# `test_chaos_hook_cannot_write_the_live_job_read_cache` imports `JobCache._key`.
_FORBIDDEN_PREFIXES = ("cache:job:",)


def _key_admitted(key: str) -> bool:
    """The hook's whole admission rule, in one importable place so the
    cross-module tripwire test asserts the real decision rather than a
    re-implementation of it."""
    if any(key.startswith(p) for p in _FORBIDDEN_PREFIXES):
        return False
    return any(key.startswith(p) for p in _ALLOWED_PREFIXES)


class CreateStaleCacheError(AppError):
    status_code = 400
    # One code for both halves of `_key_admitted`; the message says which
    # half fired.
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
            "arbitrary keys), and must NOT start with "
            f"{list(_FORBIDDEN_PREFIXES)} — that is the platform's live "
            "per-job read cache, which serves `GET /jobs/{id}` to real "
            "users. Default is the hot_set key the "
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
        "self-clears. Refuses `cache:job:` keys "
        "(`stale_cache_key_forbidden`): that is the live per-job read "
        "cache behind `GET /jobs/{id}`, not a fixture namespace."
    ),
    input_model=CreateStaleCacheInput,
    output_model=CreateStaleCacheOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def create_stale_cache(
    inp: CreateStaleCacheInput, ctx: ToolContext
) -> CreateStaleCacheOutput:
    if any(inp.key.startswith(p) for p in _FORBIDDEN_PREFIXES):
        raise CreateStaleCacheError(
            f"Key {inp.key!r} is inside the platform's live per-job read "
            f"cache ({list(_FORBIDDEN_PREFIXES)}), which serves "
            "`GET /jobs/{id}`. This hook writes a JSON array, so the "
            "write would break that endpoint for real users until the "
            "TTL lapsed. Use the default hot_set fixture key instead."
        )
    if not _key_admitted(inp.key):
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
