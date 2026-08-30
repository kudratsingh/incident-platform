"""
JSON-RPC 2.0 + MCP method envelopes.

Only the subset the incident-commander agent needs today:
  - `initialize` — client capabilities handshake
  - `tools/list` — enumerate registered tools
  - `tools/call` — invoke a tool by name

Everything is Pydantic so FastAPI parses inbound requests and validates
outbound responses without additional plumbing. Errors follow the
JSON-RPC error-object shape; a mapping from `AppError` → JSON-RPC error
lives in `handlers.py`.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelopes
# ---------------------------------------------------------------------------

# The MCP spec uses standard JSON-RPC error codes for protocol errors and
# reserves the -32000..-32099 range for server-defined errors. We use those
# for scope + authz failures so clients can distinguish protocol issues
# from application-level rejections without string-matching messages.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
MCP_UNAUTHORIZED = -32001
MCP_FORBIDDEN = -32002
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
    """Outbound JSON-RPC 2.0 response envelope. Exactly one of `result`
    or `error` is set."""

    model_config = ConfigDict(populate_by_name=True)

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None


# ---------------------------------------------------------------------------
# MCP method payloads (params / result shapes)
# ---------------------------------------------------------------------------


class InitializeParams(BaseModel):
    """Client capabilities handshake — we accept anything and echo back
    our supported protocol version. Kept intentionally loose; the agent
    can iterate its client shape without needing our schema to move.

    Field names are the MCP-spec camelCase — required on the wire.
    """

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
    """One entry in `tools/list`. `inputSchema` is a JSON Schema object;
    we generate it from each tool's Pydantic input model at registration
    time (see `registry.py`).

    `outputSchema` is a platform extension (v0.4.8) not standardized by
    the MCP spec: it's the JSON Schema of each tool's declared output
    model. Clients that don't know about it will ignore it; clients that
    do (the commander's contract-snapshot job) can diff the actual
    platform-advertised output shape against their pinned snapshot
    without treating the local registry as a source-of-truth proxy.

    `required_scope` and `is_idempotent` are platform extensions too
    (WO-R2-32), and they exist for the same reason `outputSchema` does:
    the commander's contract snapshot can only catch a change it can
    see. Both were registry-only, so re-scoping a tool or silently
    dropping its idempotency was invisible to the snapshot diff and
    could not be caught by the contract test.

    `is_idempotent` is the one that bites. It is what makes a Tier-1
    recovery re-invoke return the cached response verbatim; if it is
    dropped, the retry actually re-runs, returns a *different* payload,
    and verification reads that as a spurious escalation — a failure
    that surfaces far from its cause. Advertising it makes the drop a
    snapshot diff instead.

    Both are snake_case, unlike `inputSchema`/`outputSchema`. The
    camelCase convention belongs to the MCP spec's own fields; these
    mirror `ToolDefinition`'s attribute names, which is what the
    commander's `_tool_view` reads on the other side."""

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
    """Single content block in a tool-call response. We only emit `text`
    content today; the field is future-proofed for `image` / `resource`
    per the MCP content-type union."""

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
