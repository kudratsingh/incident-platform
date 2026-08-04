"""DAG pause flags — shared by the tool that sets them, the consumer
that enforces them, and the tool that reports them.

Lives in `app/utils/` rather than next to `pause_dag` because
`app.workers` must not import from `app.mcp`. The MCP layer calls into
the platform, never the other way around.

The flag is a Redis key `dag:paused:<job_id>` with a TTL. A WAITING
child is held back if *it* or any of its ancestors carries the flag,
which is what makes `pause_dag(root)` pause a whole saga chain rather
than just the root's direct children.

**Fail-open on Redis errors.** If the pause lookup raises, the DAG
promotes as if unpaused. Consistent with the platform's treatment of
Redis as a performance/UX dependency and never a correctness one (see
ADR 0005) — a Redis outage must not silently freeze every DAG in the
system with no way to notice. The caller logs the fall-through.
"""

import uuid
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Ancestor walks are bounded so a pathological graph can't turn one
# promotion into an unbounded Redis+DB crawl. Sagas are linear chains;
# ad-hoc deps are shallow. 64 is far above anything real.
_MAX_ANCESTOR_NODES = 64


def pause_key_for(root_id: uuid.UUID | str) -> str:
    """Redis key `pause_dag` sets and the resolver probes."""
    return f"dag:paused:{root_id}"


async def collect_ancestors(
    dep_repo: Any, job_id: uuid.UUID, *, include_self: bool = True
) -> list[uuid.UUID]:
    """Breadth-first walk up the dependency edges from `job_id`.

    Returns the job plus every transitive parent, deduplicated and
    capped at `_MAX_ANCESTOR_NODES`. Cycles are impossible by
    construction (deps only reference already-existing jobs), but the
    `seen` set makes the walk safe regardless.
    """
    seen: set[uuid.UUID] = set()
    order: list[uuid.UUID] = []
    frontier = [job_id] if include_self else list(await dep_repo.parents(job_id))

    while frontier and len(order) < _MAX_ANCESTOR_NODES:
        node = frontier.pop(0)
        if node in seen:
            continue
        seen.add(node)
        order.append(node)
        frontier.extend(await dep_repo.parents(node))

    return order


async def find_blocking_pause(
    redis: Any, dep_repo: Any, job_id: uuid.UUID
) -> uuid.UUID | None:
    """Return the id of the job whose pause flag holds `job_id` back, or
    None if nothing in its ancestry is paused.

    One `MGET` over the whole ancestor set — the walk costs DB reads but
    the pause lookup is a single Redis round-trip regardless of depth.
    """
    try:
        nodes = await collect_ancestors(dep_repo, job_id)
        if not nodes:
            return None
        values = await redis.mget([pause_key_for(n) for n in nodes])
        for node, value in zip(nodes, values, strict=False):
            if value is not None:
                return node
        return None
    except Exception as exc:  # noqa: BLE001 — fail open, see module docstring
        logger.warning(
            "dag pause lookup failed; promoting as unpaused",
            extra={"job_id": str(job_id), "error": str(exc)},
        )
        return None


async def pause_state(redis: Any, job_id: uuid.UUID) -> tuple[bool, int | None]:
    """Direct pause flag for one job: `(paused, expires_in_seconds)`.

    Only the job's own key — ancestor blocking is a separate question
    answered by `find_blocking_pause`. TTL is reported as None when the
    key is absent or carries no expiry.
    """
    try:
        key = pause_key_for(job_id)
        ttl = await redis.ttl(key)
        # redis-py: -2 = missing, -1 = present but no expiry.
        if ttl == -2:
            return False, None
        if ttl == -1:
            return True, None
        return True, int(ttl)
    except Exception as exc:  # noqa: BLE001 — fail open, see module docstring
        logger.warning(
            "dag pause state lookup failed; reporting unpaused",
            extra={"job_id": str(job_id), "error": str(exc)},
        )
        return False, None
