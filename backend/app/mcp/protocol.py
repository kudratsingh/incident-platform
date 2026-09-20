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


# The lab's own field on a `tools/call` request, and it is a SIBLING of `arguments`
# rather than a member of it (WO-R3-333, ADR 0038). Inside `arguments` it would be
# parsed by the tool's input model — refused by `extra="forbid"` on most of them, and
# on the wire in `tools/list` for every one — so the agent's prompt could carry it and
# the contract snapshot would move. Beside `arguments` it reaches the envelope only:
# nothing in `tools/list` changes, and no tool handler can see it.
LAB_PROBE_FIELD = "_lab_probe"


class ToolCallParams(BaseModel):
    """One `tools/call` request's params.

    `_lab_probe` carries a short reason string when the caller is the lab probing the
    platform *under the agent's own token* — which the principal guards and the world
    audit do on purpose, because what that token can and cannot do is the thing they
    prove. Honoured only against an `X-Lab-Principal` credential
    (`app/mcp/lab_probe.py`); the audit row for the call is then `lab.probe` instead of
    `agent.tool_invoked`, so the console can tell the evaluator's reads from the
    agent's. The field is validated by its alias alone: `lab_probe` without the
    underscore is not a second spelling, it is an unknown key that changes nothing.
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # The alias is the literal because mypy requires one here; the constant above is what
    # every other reader imports, and `test_lab_probe.py` pins the two together.
    lab_probe: str | None = Field(default=None, alias="_lab_probe")


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
    "LAB_PROBE_FIELD",
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
