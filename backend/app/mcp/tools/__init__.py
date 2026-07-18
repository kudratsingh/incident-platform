"""
Import every tool module here so the `@tool` decorator side-effects fire
at module load. Adding a new tool file means importing it below — one
line per tool.

The MCP standalone app imports this package once at startup, populating
the registry before the first request lands.
"""

from app.mcp.tools import consumer_lag  # noqa: F401

__all__: list[str] = []
