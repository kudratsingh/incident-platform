"""
Tier 1 action tools — idempotent, `actions:execute` scope.

Every tool in this package declares `idempotency_key: str` in its input
schema and is registered with `is_idempotent=True`. The dispatch layer
looks the key up before invoking the handler; repeat calls with the
same (tenant, principal, key) return the cached response verbatim.

Tier 2 actions (Wave 3 PR F) additionally require an approval
reference — those live in a sibling package.
"""

from app.mcp.tools.actions import (  # noqa: F401
    invalidate_cache_key,
    pause_dag,
    replay_dlq_messages,
    restart_consumer_group,
)

__all__: list[str] = []
