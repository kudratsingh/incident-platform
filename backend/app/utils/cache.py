"""
Redis JSON cache helpers.

Provides a thin TTL-based cache layer on top of Redis.  Values are serialised
as JSON so any JSON-serialisable object can be stored.

Usage
-----
    from app.utils.cache import JobCache

    # Read-through pattern (keys are tenant-scoped)
    cached = await JobCache.get(redis, job_id, tenant_id)
    if cached is None:
        job = await repo.get_by_id(job_id)
        await JobCache.set(redis, job_id, tenant_id, job_data)

    # Invalidate on mutation
    await JobCache.delete(redis, job_id, tenant_id)
"""

import json
import uuid
from typing import Any

from app.core.logging import get_logger
from redis.asyncio import Redis

logger = get_logger(__name__)

_JOB_TTL = 10  # seconds — short TTL; jobs change status frequently


class JobCache:
    """Cache layer for individual job objects.

    Keys are tenant-scoped (``cache:job:{tenant_id}:{job_id}``) so a cache hit
    can never cross a tenant boundary: a caller from another tenant computes a
    different key, misses, and falls through to the tenant-scoped DB path
    (E2-01). The tenant therefore never needs to live in the cached payload.
    """

    @staticmethod
    def _key(job_id: uuid.UUID | str, tenant_id: uuid.UUID | str) -> str:
        # The ``cache:`` namespace is load-bearing, not cosmetic (E2-02):
        # it is the name docs/REDIS.md already catalogs, and it is what puts
        # this key under an allowlisted prefix of the MCP `invalidate_cache_key`
        # compensator — so an agent can force-refresh a stale job read without
        # widening that tool's allowlist (which would drift its inputSchema
        # against the pinned contract snapshot).
        #
        # Deploy-safe rename: the 10s TTL means keys orphaned under the old
        # ``job:{tenant}:{id}`` name expire within one TTL of rollout. No
        # migration, no backfill.
        return f"cache:job:{tenant_id}:{job_id}"

    @classmethod
    async def get(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
    ) -> dict[str, Any] | None:
        """Return the cached job dict, or None on miss / Redis error /
        unusable payload.

        A cached value that is not a JSON object is treated as a miss
        rather than handed to the caller (R2-20). `GET /jobs/{id}` feeds
        this straight into `JobResponse.model_validate`, so anything
        else — a JSON array, a scalar, a truncated write — surfaced as
        an unhandled ValidationError and a 500 for as long as the entry
        lived. A cache is an optimisation: when its contents are
        unusable the honest behaviour is to miss and read Postgres, not
        to fail the request.
        """
        try:
            raw = await redis.get(cls._key(job_id, tenant_id))
            if raw is None:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                logger.warning(
                    "cache_get_unexpected_shape",
                    extra={
                        "job_id": str(job_id),
                        "tenant_id": str(tenant_id),
                        "payload_type": type(payload).__name__,
                    },
                )
                return None
            return payload
        except Exception:
            logger.warning(
                "cache_get_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )
            return None

    @classmethod
    async def set(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
        data: dict[str, Any],
        ttl: int = _JOB_TTL,
    ) -> None:
        """Store a job dict in the cache with a TTL. Silently ignores errors."""
        try:
            await redis.set(cls._key(job_id, tenant_id), json.dumps(data), ex=ttl)
        except Exception:
            logger.warning(
                "cache_set_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )

    @classmethod
    async def delete(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
    ) -> None:
        """Invalidate a cached job. Silently ignores errors."""
        try:
            await redis.delete(cls._key(job_id, tenant_id))
        except Exception:
            logger.warning(
                "cache_delete_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )
