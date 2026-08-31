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
#
# Generic form:
#     Any argument that is not one of the presets above is treated as a tool
#     name and dispatched with `tools/call`. The second argument is that
#     tool's `arguments` object as JSON (default `{}`); it is validated
#     locally before anything is sent.
#
#         ./scripts/mcp_probe.sh get_consumer_lag
#         ./scripts/mcp_probe.sh list_dlq_messages '{"limit":5}'
#
# Exit codes:
#     0  the call returned a JSON-RPC result
#     1  usage error (no such tool name is not a usage error — see 5)
#     2  the <arguments_json> argument is not valid JSON
#     4  the `forbidden` preset was NOT refused (see above)
#     5  the server answered with a JSON-RPC error member

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

# Abort on a JSON-RPC error member. This has to be explicit: `set -euo
# pipefail` cannot catch an application-level failure because both curl
# and jq exit 0 for a well-formed response whose payload happens to be an
# error. Without this, `.result.content[0].text` on a failed call selects
# nothing, jq prints `null`, and the probe reports success.
check_error() {
    local raw="$1"
    local code
    code="$(echo "$raw" | jq -r '.error.code // empty')"
    if [ -n "$code" ]; then
        local message
        message="$(echo "$raw" | jq -r '.error.message // "(no message)"')"
        echo "$raw" | jq >&2 || echo "$raw" >&2
        echo "MCP call FAILED — JSON-RPC error $code: $message" >&2
        exit 5
    fi
}

call() {
    local body="$1"
    local extract="${2:-}"
    local raw
    raw="$(raw_call "$body" "$TOKEN")"
    check_error "$raw"
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
        # Not `call ... | jq`: a pipeline runs `call` in a subshell, so
        # check_error's `exit 5` would kill only that subshell and the
        # probe would still report success.
        raw="$(raw_call '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}' "$TOKEN")"
        check_error "$raw"
        echo "$raw" | jq '.result.tools | map(.name)' 2>/dev/null \
            || echo "$raw" | jq
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
    -h | --help)
        # The usage block at the top of this file is the help text.
        sed -n '2,/^$/p;/^# Usage:/,/^# Exit codes:/p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        # The generic form the usage block documents: treat the argument
        # as a tool name and dispatch it. Previously this branch rejected
        # everything outside the six presets, so the documented
        # `mcp_probe.sh <tool_name> '<arguments_json>'` invocation could
        # never work as written.
        tool="$1"
        args="${2-}"
        [ -n "$args" ] || args='{}'
        if ! echo "$args" | jq -e . >/dev/null 2>&1; then
            echo "invalid JSON for <arguments_json>: $args" >&2
            echo "usage: $0 <tool_name> '<arguments_json>'" >&2
            echo "presets: initialize | tools | lag | dlq | audit | forbidden" >&2
            exit 2
        fi
        # Built with jq rather than string interpolation so a tool name or
        # argument containing a quote cannot break out of the envelope.
        body="$(jq -nc --arg name "$tool" --argjson args "$args" \
            '{jsonrpc:"2.0",id:"1",method:"tools/call",params:{name:$name,arguments:$args}}')"
        call "$body" 1
        ;;
esac
