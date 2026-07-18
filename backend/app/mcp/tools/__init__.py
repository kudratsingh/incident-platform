"""
Import every tool module here so the `@tool` decorator side-effects fire
at module load. Adding a new tool file means importing it below — one
line per tool.

The MCP standalone app imports this package once at startup, populating
the registry before the first request lands.
"""

from app.mcp.tools import (  # noqa: F401
    actions,
    chaos,
    consumer_lag,
    dag_state,
    deploy_history,
    health,
    incidents,
    list_active_alerts,
    list_audit_events,
    list_dlq_messages,
    traces,
)

__all__: list[str] = []
