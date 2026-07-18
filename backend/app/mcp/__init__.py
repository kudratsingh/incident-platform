"""
MCP server — machine-principal front door for the platform.

Deployed as a standalone process from the same image (see
`docs/ADR/0006-mcp-server-standalone-process.md`). Handlers call the
service layer directly; import-linter enforces `app.mcp → app.services`
as one-directional.

Public shape:
  - `standalone.app` — FastAPI ASGI app with a single POST /mcp endpoint
  - `registry.tool` — decorator for tool implementations
  - `protocol.*` — JSON-RPC 2.0 + MCP method types

Import `app.mcp.tools` (side-effect) to register every shipped tool.
"""

from app.mcp.registry import ToolDefinition, tool

__all__ = ["ToolDefinition", "tool"]
