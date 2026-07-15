# ADR 0006 — MCP server as a thin adapter over the service layer

**Status:** Proposed (agent-platform Step 0) · **Date:** 2026 Q3 · **Owner:** Platform

## Context

The next program on top of Phases 1–12 is making this platform safely operable by a machine principal — an autonomous agent that lives in a separate repo and consumes contracts published from this one. The agent talks to the platform through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/): a JSON-RPC-over-stdio (or WebSocket) surface with a tools/resources/prompts vocabulary.

We need to decide *where the MCP server runs* and *what it's allowed to touch*. Two shapes were on the table:

1. **In-process** — mount the MCP handlers inside the existing FastAPI app, share the same DB session and Redis client, and expose them on a new `/mcp` path.
2. **Out-of-process** — a separate `mcp-server/` service (its own container, its own process) that speaks MCP outbound and calls the platform's HTTP API for every operation.

Either way the agent must never bypass tenant isolation, quota enforcement, audit logging, or the rate limiter — the guarantees the HTTP surface already provides.

## Decision

Ship the MCP server as a **separate process (`mcp-server/`) that is a thin adapter over the existing service layer, invoked exclusively via the platform's HTTP API**. It gets no direct database, Redis, or Kafka access. Every tool call becomes an HTTP request against `/api/v1/*` authenticated with the agent's scoped token.

Concretely:

- `mcp-server/` is a new top-level directory with its own Dockerfile, its own dependency set, and its own release cadence.
- Each MCP tool is a ~10-line function that maps arguments to an HTTP request and formats the response into an MCP tool result.
- The generated `agent-tools.json` contract artifact is derived from the same Pydantic models the API uses — that's the shared surface between the two repos.
- No SQLAlchemy import in `mcp-server/`. No `app.repositories.*`. No `app.workers.*`. If the adapter needs a capability, the capability ships as a new HTTP endpoint first.

## Why an adapter, not a mount

### Isolation of blast radius

The agent is autonomous and, per the program plan, will eventually be able to *execute* Tier 1 actions (restart consumer group, replay DLQ, pause DAG, invalidate cache key). An adapter with API-only access has the same blast radius as any other authenticated caller — a bug in the adapter cannot forget a tenant filter, skip an audit log, or scribble on a Redis key. An in-process mount would share transaction boundaries and could accidentally bypass any of them.

### Independent deploy cadence

Agent iteration is fast. MCP tool signatures churn during Wave 2 and Wave 3 as new scenario families surface. A separate service can be deployed independently of the platform, which matters when the platform has an active SLO and a merge freeze policy but the agent needs a new tool by Friday.

### Contract-driven, not implementation-driven

`agent-tools.json` is generated from Pydantic → JSON Schema at build time. The agent repo consumes that artifact and never sees the Python source. An in-process mount blurs this boundary: it becomes easy to add a tool that reads a field the schema doesn't publish, and easy for the agent to drift onto internal surface. A separate process forces every capability through the artifact.

### Auth model matches other clients

The frontend, the CI test suite, and load tests all speak to the platform over HTTP with a bearer token. The MCP adapter is a fourth such client — a well-understood shape, using the same middleware stack (rate limit, tenant context, RLS, audit log, request tracing). One less code path to reason about.

### Failure isolation

If the MCP server wedges (a bad tool implementation, an OOM under a huge tool result), the platform continues serving humans normally. An in-process mount couples the agent's stability to the platform's.

## Alternatives considered

### In-process FastAPI mount

Cheapest to build (one router). Same runtime, same DB session, same everything.

**Why not:** rejected on the isolation-of-blast-radius argument above. The whole point of the program is to make the platform safely operable by a non-human principal; sharing runtime with the human-serving API defeats the guarantee before we start.

### MCP server that talks to the DB directly, bypassing HTTP

Faster per call (no HTTP hop, no JSON encode/decode). Some MCP integrations do this.

**Why not:**
- Every guarantee the API middleware provides (RLS via `set_config`, rate limit, quota check, tenant-scoped audit log entry) has to be reimplemented, tested, and kept in sync. That's a duplicate service layer just to save a millisecond.
- The tenant context (`app.tenant_id` GUC) is set inside `get_current_user` in the API. Reproducing it in the MCP process means either duplicating the auth flow or leaking auth internals across the boundary.
- HTTP round-trips are ~1ms on the loopback — the agent's LLM inference dwarfs that by three orders of magnitude. There is no latency budget to save here.

### MCP inside the worker process

Mount the MCP handlers in the worker (`worker_loop`) so the adapter has direct access to Kafka consumer state (`consumer.in_flight`, `consumer.consumer_lag()`).

**Why not:** the worker is the code path least tolerant of new failure modes — it drives job execution and holds Kafka offsets. Adding an inbound network handler that's driven by an autonomous agent inverts the trust model. Instead, the platform exposes read endpoints (`GET /admin/consumer-lag`, forthcoming) that the adapter calls.

## Consequences

### Positive

- **Guarantee inheritance.** Everything the HTTP surface enforces — tenant scoping, quotas, rate limits, audit trail, request tracing — is enforced for the agent by construction. No parallel implementation.
- **Contract testability.** The MCP adapter can be exercised end-to-end without the agent by running it against a stubbed platform. The agent can be exercised without the platform by mocking the HTTP responses. Two independent test surfaces.
- **Cross-repo boundary is a JSON artifact.** `agent-tools.json` published on tag; the agent repo pins a version. No shared source tree, no hidden coupling.
- **Independent scaling.** MCP adapter can run at different replica count than the API. Not important today (single-agent use case) but a lever we keep.

### Negative

- **Extra HTTP hop per tool call.** ~1–2ms overhead vs an in-process mount. Negligible against the LLM's inference latency.
- **Two dependency sets to maintain.** `mcp-server/pyproject.toml` and `backend/pyproject.toml` both pull Pydantic, both need SDK updates. Mitigated by keeping the adapter's dep list tiny (Pydantic + MCP SDK + `httpx` — that's it).
- **A new deploy target.** Adds an entry to CI, a new ECR repo, a new ECS task definition. Owned by the same team so operationally small, but real.
- **Adds a network-visible surface** to the platform for agent traffic. Same rate limiter, same tenant model, but a new caller class in the logs. Distinguishable via the machine-principal audit trail (see [ADR 0007](0007-machine-principal-scope-model.md)).

### Reversibility

If we ever want to collapse the adapter into the API, mount the same functions as FastAPI routes and delete the `mcp-server/` container. The HTTP interface stays; the process boundary is the only thing that changes.

## Verification

- Adapter unit tests: mock `httpx`, assert each MCP tool maps args → correct HTTP call.
- Integration test: run the adapter against a real backend container in CI, exercise the read-only tools shipped in Wave 1.
- Contract test: `agent-tools.json` generated on each PR; CI fails if the artifact drifts from the Pydantic models without a matching agent-repo consumer update.

## Pointers

- `mcp-server/` (to be created in Wave 1 PR #3)
- `agent-tools.json` — generated contract artifact (to be defined in Wave 1 PR #6)
- Related ADRs: [0007 — Machine-principal scope model](0007-machine-principal-scope-model.md), [0008 — Chaos gating](0008-chaos-gating.md)
