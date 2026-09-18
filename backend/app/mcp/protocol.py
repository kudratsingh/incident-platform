"""
JSON-RPC 2.0 + MCP envelopes for `initialize`, `tools/list` and `tools/call`.

All Pydantic, so FastAPI does the parsing and validation. The `AppError` →
JSON-RPC error mapping lives in `handlers.py`.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelopes
# ---------------------------------------------------------------------------

# -32000..-32099 is the spec's server-defined range; scope + authz use it so clients
# can tell them from protocol errors.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
MCP_UNAUTHORIZED = -32001
MCP_FORBIDDEN = -32002
# Per-principal rate limit exceeded (WO-R2-30): its own code so a client can tell
# back-off-and-retry from a bad request.
MCP_RATE_LIMITED = -32003
MCP_TOOL_NOT_FOUND = -32010
MCP_TOOL_ERROR = -32011


class JsonRpcRequest(BaseModel):
    """Inbound JSON-RPC 2.0 request envelope."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
    """Outbound envelope; one of `result`/`error`."""

    model_config = ConfigDict(populate_by_name=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None


# ---------------------------------------------------------------------------
# MCP method payloads (params / result shapes)
# ---------------------------------------------------------------------------


class InitializeParams(BaseModel):
    """Client handshake; deliberately loose. Field names are MCP-spec camelCase."""

    model_config = ConfigDict(extra="allow")

    protocolVersion: str | None = None  # noqa: N815 (MCP spec)
    clientInfo: dict[str, Any] | None = None  # noqa: N815 (MCP spec)


class ServerInfo(BaseModel):
    name: str
    version: str


class InitializeResult(BaseModel):
    protocolVersion: str  # noqa: N815 (MCP spec)
    serverInfo: ServerInfo  # noqa: N815 (MCP spec)
    capabilities: dict[str, Any] = Field(default_factory=lambda: {"tools": {}})


class ToolInfo(BaseModel):
    """One `tools/list` entry; `outputSchema`, `required_scope`, `is_idempotent`
    are platform extensions the commander's contract snapshot pins."""

    name: str
    description: str
    inputSchema: dict[str, Any]  # noqa: N815 (MCP spec)
    outputSchema: dict[str, Any] = Field(  # noqa: N815 (platform extension)
        default_factory=dict,
        description=(
            "JSON Schema of the tool's declared output model. Platform "
            "extension — not in the MCP spec proper."
        ),
    )
    required_scope: str | None = Field(
        default=None,
        description=(
            "Scope a principal must hold to call this tool, as the bare "
            "scope string (e.g. 'actions:execute'). None for the few "
            "tools that require no scope. Platform extension."
        ),
    )
    is_idempotent: bool = Field(
        default=False,
        description=(
            "Whether a repeat call with the same idempotency_key returns "
            "the cached response instead of re-running the tool. "
            "Platform extension."
        ),
    )


class ToolsListResult(BaseModel):
    tools: list[ToolInfo]


class ToolCallParams(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallContent(BaseModel):
    """Single content block in a tool-call response; only `text` is emitted today."""

    type: Literal["text"] = "text"
    text: str


class ToolCallResult(BaseModel):
    content: list[ToolCallContent]
    isError: bool = False  # noqa: N815 (MCP spec)


__all__ = [
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "InitializeParams",
    "InitializeResult",
    "JsonRpcError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "MCP_FORBIDDEN",
    "MCP_TOOL_ERROR",
    "MCP_TOOL_NOT_FOUND",
    "MCP_UNAUTHORIZED",
    "ServerInfo",
    "ToolCallContent",
    "ToolCallParams",
    "ToolCallResult",
    "ToolInfo",
    "ToolsListResult",
]
