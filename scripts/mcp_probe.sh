#!/usr/bin/env bash
# Smoke-test the MCP surface without fighting zsh's terminal wrapping.
#
# Usage:
#     export TOKEN=sa_...
#     ./scripts/mcp_probe.sh audit
#     export READ_TOKEN=sa_...           # `forbidden` preset only
#     ./scripts/mcp_probe.sh forbidden
#     ./scripts/mcp_probe.sh <tool_name> '<arguments_json>'
#
# Environment:
#     TOKEN       required — the seeded sa_ bearer every preset calls with.
#     READ_TOKEN  required by `forbidden` ONLY, and it must be a DIFFERENT
#                 bearer that does NOT hold actions:execute. The seeded
#                 incident-commander principal acquired actions:execute in
#                 wave 3, so reusing TOKEN for the negative probe performs a
#                 REAL restart of the worker-dispatcher consumer group.
#     MCP_URL     endpoint override (default http://localhost:8001/mcp).
#
# Presets:
#     initialize    — server info handshake
#     tools         — list every registered tool
#     lag           — get_consumer_lag
#     dlq           — list_dlq_messages
#     audit         — list_audit_events (agent.* prefix)
#     forbidden     — negative probe: restart_consumer_group with READ_TOKEN,
#                     which must be refused with MCP_FORBIDDEN (JSON-RPC
#                     error code -32002). Exits 4 if it was NOT refused.

set -euo pipefail

: "${TOKEN:?Set TOKEN to the seeded sa_ bearer first}"
URL="${MCP_URL:-http://localhost:8001/mcp}"

# Issue a JSON-RPC request and echo the raw, unparsed response body.
# $1 = request body, $2 = bearer token. Split out of `call` so the
# `forbidden` preset can both use a different token and assert on the
# envelope instead of pretty-printing it away.
raw_call() {
    curl -sS -X POST "$URL" \
        -H "Authorization: Bearer $2" \
        -H 'Content-Type: application/json' \
        -d "$1"
}

call() {
    local body="$1"
    local extract="${2:-}"
    local raw
    raw="$(raw_call "$body" "$TOKEN")"
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
        # Negative probe. restart_consumer_group declares
        # required_scope=actions:execute, and the handler's scope check runs
        # BEFORE argument parsing and before any side effect, so a bearer
        # without that scope is refused with MCP_FORBIDDEN / -32002.
        #
        # READ_TOKEN is mandatory and must not be $TOKEN. The seeded
        # incident-commander principal is no longer read-only — it carries
        # actions:execute — so running this preset with TOKEN really restarts
        # the worker-dispatcher consumer group. Requiring a separate
        # read-scoped bearer makes the mutation impossible rather than merely
        # reported after the fact; this probe has to stay safe to run at any
        # time, including mid-eval.
        : "${READ_TOKEN:?forbidden preset needs READ_TOKEN — a token WITHOUT actions:execute. The seeded incident-commander TOKEN now carries actions:execute and would REALLY restart worker-dispatcher.}"
        # idempotency_key is FIXED at probe-1234 on purpose. If a mis-scoped
        # bearer ever does slip through, the platform's idempotency record
        # collapses every probe run into one restart instead of one per run.
        # Do not "improve" this to $RANDOM.
        raw="$(raw_call '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"restart_consumer_group","arguments":{"consumer_group":"worker-dispatcher","idempotency_key":"probe-1234"}}}' "$READ_TOKEN")"
        echo "$raw" | jq
        # Assert numerically on .error.code, never on message text. Both curl
        # and jq exit 0 on an application-level SUCCESS, which is precisely
        # why `set -euo pipefail` never caught a probe that executed the
        # restart and printed the happy-path result.
        code="$(echo "$raw" | jq -r '.error.code // empty')"
        if [ "$code" != "-32002" ]; then
            echo "FORBIDDEN PROBE FAILED — the call may have EXECUTED restart_consumer_group; check audit log (agent.tool_invoked, outcome)" >&2
            exit 4
        fi
        echo "OK — refused with MCP_FORBIDDEN (-32002), no restart performed"
        ;;
    *)
        echo "unknown preset: $1"
        echo "presets: initialize | tools | lag | dlq | audit | forbidden"
        exit 1
        ;;
esac
