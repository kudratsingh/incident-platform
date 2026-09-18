"""
Redis JSON cache helpers — a thin TTL layer, values serialised as JSON.

Read-through via `JobCache.get` / `.set` on tenant-scoped keys, and
`JobCache.invalidate` once a mutation has COMMITTED.
"""

import json
import uuid
from typing import Any

from app.core.logging import get_logger
from redis.asyncio import Redis

logger = get_logger(__name__)

_JOB_TTL = 10  # seconds — short TTL; jobs change status frequently

# How long `invalidate` keeps the slot closed to writers; must outlive an
# in-flight `GET /jobs/{id}`.
_NO_CACHE_TTL = 30  # seconds

# The tombstone `invalidate` parks in the slot. Not valid JSON, so a reader on
# an older deploy falls into `get`'s broad `except` and reads it as a miss.
_INVALIDATED = "__invalidated__"


class JobCache:
    """Cache layer for individual job objects.

    Keys are tenant-scoped (``cache:job:{tenant_id}:{job_id}``), so a hit
    cannot cross a tenant boundary (E2-01).
    """

    @staticmethod
    def _key(job_id: uuid.UUID | str, tenant_id: uuid.UUID | str) -> str:
        # The ``cache:`` namespace is load-bearing (E2-02): docs/REDIS.md catalogs
        # it, and it is the allowlisted prefix the MCP `invalidate_cache_key`
        # compensator needs to force-refresh a stale job read.
        return f"cache:job:{tenant_id}:{job_id}"

    @classmethod
    async def get(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
    ) -> dict[str, Any] | None:
        """Return the cached job dict, or None on miss / Redis error / bad payload.

        A value that is not a JSON object is a miss, not a handoff (R2-20):
        `GET /jobs/{id}` validates it and used to 500 for the entry's whole life.
        """
        try:
            raw = await redis.get(cls._key(job_id, tenant_id))
            if raw is None:
                return None
            if raw in (_INVALIDATED, _INVALIDATED.encode()):
                # A committed mutation's tombstone: a quiet miss, not a
                # corrupt payload.
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
        """Store a job dict in the cache with a TTL. Silently ignores errors.

        `nx=True` is what makes `invalidate` stick (R2-23): this write may carry a
        row already stale, so it must not bury the tombstone. The cost is that a
        live entry is never refreshed mid-TTL.
        """
        try:
            await redis.set(
                cls._key(job_id, tenant_id), json.dumps(data), ex=ttl, nx=True
            )
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
        """Drop a cached job outright. Silently ignores errors.

        Leaves the slot empty — see `invalidate` after a status change.
        """
        try:
            await redis.delete(cls._key(job_id, tenant_id))
        except Exception:
            logger.warning(
                "cache_delete_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )

    @classmethod
    async def invalidate(
        cls,
        redis: Redis,
        job_id: uuid.UUID | str,
        tenant_id: uuid.UUID | str,
        ttl: int = _NO_CACHE_TTL,
    ) -> None:
        """Invalidate after a COMMITTED mutation. Silently ignores errors.

        `delete` alone loses a race (R2-23): a reader holding the pre-mutation row
        can `set` it back for a full TTL. So this parks a tombstone, which against
        `set`'s `nx=True` closes the slot to writers for `ttl`. Call it *after* the
        commit, never inside it.
        """
        try:
            await redis.set(cls._key(job_id, tenant_id), _INVALIDATED, ex=ttl)
        except Exception:
            logger.warning(
                "cache_invalidate_failed",
                extra={"job_id": str(job_id), "tenant_id": str(tenant_id)},
            )
