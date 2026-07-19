#!/usr/bin/env bash
# Smoke-test the MCP surface without fighting zsh's terminal wrapping.
#
# Usage:
#     export TOKEN=sa_...
#     ./scripts/mcp_probe.sh audit
#     ./scripts/mcp_probe.sh forbidden
#     ./scripts/mcp_probe.sh <tool_name> '<arguments_json>'
#
# Presets:
#     initialize    — server info handshake
#     tools         — list every registered tool
#     lag           — get_consumer_lag
#     dlq           — list_dlq_messages
#     audit         — list_audit_events (agent.* prefix)
#     forbidden     — restart_consumer_group with the read-only token
#                     (expected to fail with MCP_FORBIDDEN)

set -euo pipefail

: "${TOKEN:?Set TOKEN to the seeded sa_ bearer first}"
URL="${MCP_URL:-http://localhost:8001/mcp}"

call() {
    local body="$1"
    local extract="${2:-}"
    local raw
    raw="$(curl -sS -X POST "$URL" \
        -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: application/json' \
        -d "$body")"
    if [ -n "$extract" ]; then
        echo "$raw" | jq -r '.result.content[0].text' | jq
    else
        echo "$raw" | jq
    fi
}

case "${1:-initialize}" in
    initialize)
        call '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{}}'
        ;;
    tools)
        call '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}' \
            | jq '.result.tools | map(.name)' 2>/dev/null \
            || call '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}'
        ;;
    lag)
        call '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"get_consumer_lag","arguments":{}}}' 1
        ;;
    dlq)
        call '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"list_dlq_messages","arguments":{}}}' 1
        ;;
    audit)
        call '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"list_audit_events","arguments":{"action_prefix":"agent.","limit":10}}}' 1
        ;;
    forbidden)
        # Expect MCP_FORBIDDEN — seed token has only telemetry:read + incidents:read
        call '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"restart_consumer_group","arguments":{"consumer_group":"worker-dispatcher","idempotency_key":"probe-1234"}}}'
        ;;
    *)
        echo "unknown preset: $1"
        echo "presets: initialize | tools | lag | dlq | audit | forbidden"
        exit 1
        ;;
esac
