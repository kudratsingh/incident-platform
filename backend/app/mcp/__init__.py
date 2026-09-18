"""
MCP server — machine-principal front door for the platform (ADR 0006).

A standalone process from the same image. Import-linter keeps
`app.mcp → app.services` one-directional (`[tool.importlinter]`, pyproject.toml).
Import `app.mcp.tools` (side-effect) to register every shipped tool.
"""

from app.mcp.registry import ToolDefinition, tool

__all__ = ["ToolDefinition", "tool"]
