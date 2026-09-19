"""
Import every tool module here so the `@tool` decorator side-effects fire at load.
A new tool file means one more import below.
"""

from app.mcp.tools import (  # noqa: F401
    actions,
    cache_key_info,
    chaos,
    circuit_breakers,
    consumer_lag,
    dag_state,
    deploy_history,
    health,
    incidents,
    list_active_alerts,
    list_audit_events,
    list_dlq_messages,
    outbox_status,
    slo_status,
    traces,
)

__all__: list[str] = []
