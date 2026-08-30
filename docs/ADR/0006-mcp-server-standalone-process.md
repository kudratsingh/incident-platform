# ADR 0006: Serve MCP as a standalone process from the platform codebase

* Status: accepted
* Date: 2026-07-15
* Decider: Kudrat Singh

## Context and problem statement

The platform is gaining an agent-facing MCP surface so that Incident Commander, and any other MCP client, can operate it through typed tools (agent repo ADR 0001). MCP defines the wire protocol only: streamable HTTP transport, an initialize handshake, tools/list, and tools/call. It says nothing about deployment topology. The open question is where the server code lives and how it runs: mounted inside the existing FastAPI app, as its own process, behind an API proxy, or in a separate repository. The choice affects operations and failure isolation, not the agent, which connects to a URL with a bearer token in every case.

## Decision drivers

* The security boundary is the contract plus the constrained principal, never process topology. Whatever ships must reuse the existing authz middleware, principal resolution, and audit path.
* The MCP surface must not drift from the service layer and schemas it fronts.
* Production remote MCP servers (GitHub, Sentry, Stripe, Linear) run as standalone endpoints. Matching that operational shape keeps the demo and the project narration aligned with what the industry deploys.
* Tool calls from an agent are bursty machine traffic. Isolating them from the user-facing API protects latency and allows independent scaling and rate limiting.
* Solo-scale ops budget. Every additional deployable must justify its health checks and alarms.

## Considered options

1. Mounted sub-application inside the existing FastAPI app
2. Standalone process built from the same codebase with direct service layer imports (chosen)
3. Separate service proxying the platform REST API, the Sentry middleware shape
4. Separate repository for the MCP server

## Decision outcome

Option 2. The MCP server is code in this repo at backend/app/mcp/ and a process of its own in every deployed environment.

* Handlers call service layer functions directly, the same functions the REST endpoints call. No SQL in handlers, no reaching into other routers. Imports are one directional: app.mcp imports app.services, and nothing imports app.mcp. Two import-linter contracts in CI enforce this: a `layers` contract for the direction and a `forbidden` contract for the "nothing imports app.mcp" half. Both live in `[tool.importlinter]` in pyproject.toml. (They were added in WO-R2-62 — this sentence claimed CI enforcement for some time while import-linter was not installed, had no config and ran in no job.)
* A dedicated ASGI entrypoint at backend/app/mcp/standalone.py builds the MCP app with the shared auth dependencies and DB session wiring. The same Docker image runs it with a different command on its own port with its own health check.
* Every request authenticates as a machine principal through the same dependency chain as REST. Scopes are enforced per tool. Every call writes an audit record. An unauthenticated tools/list returns 401, and PR-1 carries that test.
* Clients configure PLATFORM_MCP_URL separately from PLATFORM_REST_URL. In compose, the MCP surface is a second container from the pinned platform image.

### Why the alternatives lose

**Mounted sub-application.** Fully supported by the ecosystem and the fewest moving parts, one process and one deploy. Rejected because it shares a process lifecycle with the user-facing API, so agent traffic contends with human traffic and a crash takes down both surfaces. The standalone shape costs roughly one extra file and one compose stanza and buys the isolation now.

**API proxy.** Sentry's remote MCP runs as middleware over its public API because Cloudflare Workers cannot import the Sentry backend and the server brokers OAuth for millions of third parties at the edge. Neither constraint exists here. Proxying my own API would add a network hop, a second auth pass, and a duplicated schema layer for nothing.

**Separate repository.** Splits the MCP surface from the service layer and Pydantic models it fronts, guaranteeing drift and double maintenance. The repository boundary that matters is around the agent, per agent ADR 0001, not inside the platform.

### Consequences

Positive:

* Agent traffic is isolated from the user-facing API, with independent scaling and rate limits available when needed.
* The surface cannot version-drift from the service layer because both ship in the same image at the same commit.
* Direct service calls keep one DB transaction and one trace per tool call, with no proxy hop.
* The deployed shape matches production remote MCP servers, so the demo tells the industry-standard story without translation.

Negative:

* A second deployable exists: compose stanza, ECS service, health check, alarms. Accepted, since the image is shared and there is no second build.
* Two processes means DB pool sizing is set per process, and the MCP process gets a small pool matched to its rate limits.
* Platform PR-1 grows slightly. Accepted, it remains one coherent slice.

Revisit trigger: if operating two services at solo scale proves to be real friction, collapse to the mounted topology by mounting the same ASGI app inside the main application. That change is one line, alters no contracts, no auth, and no agent code, which is exactly why this decision is safe to make now.

## More information

Agent repo ADR 0001 defines the external client boundary this surface serves. Agent repo ADR 0007, planned, covers contract snapshot testing against the published image. Implemented in platform PR-1.
