"""
Tier 1 action tools — idempotent, `actions:execute` scope.

Every tool here declares `idempotency_key: str` and `is_idempotent=True`, so a
repeat call replays the cached response. Tier 2 lives in a sibling package.
"""

from app.mcp.tools.actions import (  # noqa: F401
    invalidate_cache_key,
    mark_dlq_permanent,
    pause_dag,
    replay_dlq_by_category,
    replay_dlq_by_ids,
    replay_dlq_messages,
    restart_consumer_group,
)

__all__: list[str] = []
