"""Contract tests for `scripts/mcp_probe.sh` against a stub MCP endpoint.

Two defects pinned here:

  1. The extract path piped the response through
     `jq -r '.result.content[0].text'` without ever looking at
     `.error`. A `lag` / `dlq` / `audit` probe against a broken stack
     printed `null` and exited 0 — the operator's smoke test reported
     success for a call that failed. `set -euo pipefail` cannot catch
     this: both curl and jq exit 0 on an application-level JSON-RPC
     error.

  2. The usage block documented a generic
     `mcp_probe.sh <tool_name> '<arguments_json>'` form that the case
     statement's default branch rejected as an unknown preset.

The stub speaks just enough JSON-RPC to drive both paths, so these run
in the unit tier with no stack, no Docker and no network.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PROBE = _REPO_ROOT / "scripts" / "mcp_probe.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("curl") is None,
    reason="mcp_probe.sh needs curl and jq on PATH",
)

# Any tool the stub does not know is answered with a JSON-RPC error,
# mirroring MCP_TOOL_NOT_FOUND from the real dispatch layer.
_KNOWN_TOOL = "get_consumer_lag"


class _StubHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(body)

        params = body.get("params") or {}
        name = params.get("name")
        if body.get("method") == "tools/call" and name != _KNOWN_TOOL:
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32601, "message": f"unknown tool: {name}"},
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"content": [{"type": "text", "text": json.dumps({"probe": "ok"})}]},
            }

        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def stub_url() -> Any:
    _StubHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def _run(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_PROBE), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "TOKEN": "sa_stub",
            "MCP_URL": url,
        },
    )


def test_a_json_rpc_error_on_an_extract_preset_exits_non_zero(
    stub_url: str,
) -> None:
    """`lag` calls get_consumer_lag, which the stub knows — so force the
    error path by pointing the generic form at a tool it does not."""
    result = _run(stub_url, "definitely_not_a_tool", "{}")

    assert result.returncode != 0, (
        "a failed probe exited 0 — this is the defect: the operator's smoke "
        f"test reported success.\nstdout:\n{result.stdout}"
    )
    combined = result.stdout + result.stderr
    assert "-32601" in combined
    assert "unknown tool" in combined
    assert combined.strip() != "null"


def test_the_documented_generic_form_works(stub_url: str) -> None:
    """The usage block promises `mcp_probe.sh <tool_name> '<args_json>'`."""
    result = _run(stub_url, _KNOWN_TOOL, '{"consumer_group":"worker-dispatcher"}')

    assert result.returncode == 0, (
        f"documented generic invocation failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "unknown preset" not in (result.stdout + result.stderr)
    assert "probe" in result.stdout

    sent = [r for r in _StubHandler.requests if r.get("method") == "tools/call"]
    assert sent, "no tools/call was issued"
    params = sent[-1]["params"]
    assert params["name"] == _KNOWN_TOOL
    assert params["arguments"] == {"consumer_group": "worker-dispatcher"}


def test_the_generic_form_defaults_arguments_to_an_empty_object(
    stub_url: str,
) -> None:
    result = _run(stub_url, _KNOWN_TOOL)

    assert result.returncode == 0, result.stderr
    sent = [r for r in _StubHandler.requests if r.get("method") == "tools/call"]
    assert sent and sent[-1]["params"]["arguments"] == {}


def test_a_preset_still_works(stub_url: str) -> None:
    """Adding the generic form must not shadow the named presets."""
    result = _run(stub_url, "lag")

    assert result.returncode == 0, result.stderr
    sent = [r for r in _StubHandler.requests if r.get("method") == "tools/call"]
    assert sent and sent[-1]["params"]["name"] == "get_consumer_lag"


def test_malformed_arguments_json_is_rejected_before_the_call(
    stub_url: str,
) -> None:
    result = _run(stub_url, _KNOWN_TOOL, "{not json")

    assert result.returncode != 0
    assert "JSON" in (result.stdout + result.stderr)
    assert not _StubHandler.requests, "a malformed probe still hit the server"
