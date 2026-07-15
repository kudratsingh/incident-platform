# ADR 0008 — Chaos framework is triple-gated and never enabled in production

**Status:** Proposed (agent-platform Step 0) · **Date:** 2026 Q3 · **Owner:** Platform

## Context

Wave 1 of the agent-platform program adds a chaos framework — a set of tools the agent can invoke to inject controlled failures into a running platform (kill a consumer, poison a message, saturate Redis, inject latency, roll a bad deploy). The goal is to let the agent *practice* incident response against realistic conditions without waiting for an actual outage.

That's valuable during development and evaluation. It's catastrophic in production. A tool that says "kill the dispatcher consumer" is functionally indistinguishable from an attacker with the same token, and neither the tool nor the caller has any way to know which environment they're in from the platform's perspective.

We need a design that makes accidental production chaos impossible, not just unlikely.

## Decision

The chaos framework is **triple-gated**. All three gates must be open, independently, for any chaos tool to execute:

1. **Environment flag** — `CHAOS_ENABLED=true` in the platform's own environment. Default `false`. Terraform hardcodes `false` for the production workspace and refuses to accept `true` there (a validation rule in `variables.tf`). Baked into the container's env at deploy time, not toggleable at runtime.

2. **Scope** — the calling principal must carry the `chaos:invoke` scope ([ADR 0007](0007-machine-principal-scope-model.md)). No human role grants this; only a service account with the scope explicitly minted onto its token. The seed `incident-commander` principal does not have it.

3. **Per-tool authorization** — each chaos tool declares its blast radius; the middleware refuses to dispatch if the current environment's `CHAOS_MAX_BLAST_RADIUS` is lower. A `kill_consumer` on a single consumer group is low; `saturate_redis` is higher; `bad_deploy` is highest.

If gate 1 is closed, the chaos tools are not registered on the MCP server at startup — they don't appear in `agent-tools.json` for that environment. If gate 1 is open but gate 2 is closed, the call returns 403 with `error_code: scope_required`. If gates 1 and 2 are open but gate 3 refuses the tool, the call returns 403 with `error_code: blast_radius_exceeded`.

### Why three gates and not one strict one

Any single gate is one accident away from failure:

- **Env flag alone:** a misconfigured Terraform variable or a copied `.env` file flips the switch. It's happened in this codebase's history (see `feat/frontend-ci`'s scoped-token misconfiguration, since fixed).
- **Scope alone:** a token minted with too broad a scope stays minted for 90 days by default; the mistake is discovered when someone runs a chaos tool in prod.
- **Per-tool alone:** relies on the tool author correctly declaring blast radius. Human error.

Three gates that fail in different ways (config, credential, code) means the chaos tools stay dark unless three independent decisions have all been made deliberately.

### Non-prod-only invariant

The Terraform validation is the load-bearing one:

```hcl
variable "chaos_enabled" {
  type    = bool
  default = false
  validation {
    condition     = !(var.chaos_enabled && var.environment == "production")
    error_message = "CHAOS_ENABLED must be false in production."
  }
}
```

Terraform apply fails if a maintainer flips this in the wrong workspace. Backed by an `assert` in `app/config.py` that refuses to boot the app if `CHAOS_ENABLED=true` and `ENVIRONMENT=production` — belt and braces.

## Audit surface

Chaos actions get their own audit event stream, distinct from `agent.tool_invoked`:

- `chaos.tool_invoked` — every chaos tool call (successful or not)
- `chaos.tool_denied` — when a gate refused execution (which gate, which principal, which tool)

Filterable independently on the admin Audit tab so a game-day exercise shows up as a clear activity band without polluting the general operational audit stream.

## Alternatives considered

### One env flag, no scope, trust the operator

Simplest. Every non-prod env sets the flag; every prod env doesn't; done.

**Why not:** a bug in a non-prod env that leaks a chaos tool call into a shared dependency (a shared Redis cluster during a staging test that reaches production Redis by DNS mistake) has no second line. Also gives the agent's principal the same power as any other authenticated caller, which is precisely what the scope model exists to prevent.

### Enable chaos in production but only through the approvals subsystem

Wave 3 adds propose → approve → execute for high-impact actions. Chaos could ride the same rail: an operator approves each chaos invocation.

**Why not:**
- Chaos is *by definition* meant to break things. Making it approvable-in-prod normalizes the "yes, break prod on purpose" workflow, which is exactly the workflow we want to make impossible.
- Even with approval, a mis-scoped tool (e.g. `saturate_redis` targets the wrong Redis) causes real customer impact. The approvals subsystem prevents wrong-agent decisions, not wrong-tool decisions.
- Real production chaos is done at the infrastructure layer via game days with pre-declared blast radius and communication — a fundamentally different workflow with its own tooling. It doesn't belong in the agent's tool set.

### Rely on network segmentation (chaos tools only reachable from staging VPC)

Add a network policy: chaos endpoints only accepted from the staging VPC.

**Why not:** network policy is a good *additional* layer but a bad *primary* one. Networking configs drift; VPC peering exists; the agent could be run from anywhere. In-code refusal is authoritative regardless of where the caller sits.

### Ship chaos tools but require a per-invocation break-glass code

Each call requires a fresh MFA-style token.

**Why not:** turns the agent's automation into a human-in-the-loop workflow, which defeats the purpose of an autonomous agent practicing incident response. If a human is minting break-glass codes per call, the agent isn't practicing anything the human isn't already doing.

## Consequences

### Positive

- **Production is provably safe.** Terraform validation prevents `CHAOS_ENABLED=true` in the production workspace; app boot refuses the combination even if Terraform were somehow bypassed. Two independent checks on the same invariant.
- **Chaos tools are invisible to prod agents.** They're not in `agent-tools.json` for the production environment. The agent can't even *try* to call them because it doesn't know they exist.
- **Kill switch is per-scope.** Revoke the `chaos:invoke` scope on the principal → chaos tools become uncallable within one middleware pass. Rest of the agent's capabilities keep working.
- **Auditability of chaos activity is a first-class signal.** Separate `chaos.*` event stream, admin Audit tab filter, and (eventually) a CloudWatch alarm on the rate of chaos denials.

### Negative

- **Three gates means three places to verify when chaos "doesn't work".** Debugging a legitimately-disabled chaos tool in a staging env involves checking the flag, the scope, and the blast-radius setting. Mitigated by a startup log line that dumps the state of all three gates on boot.
- **Adds an environment variable that must be set explicitly per environment.** Every future ECS task definition and every developer's `.env.example` must carry it. Documented in the setup guide.
- **The MCP adapter has to filter its tool registry by environment.** When `CHAOS_ENABLED=false`, chaos tools aren't just refused — they're not surfaced. This means the adapter reads the flag at startup. If the flag changes, the adapter needs a restart; acceptable given the flag is deploy-time.

### Reversibility

Removing the framework entirely is deletion of `mcp-server/chaos_tools/`, deletion of the `chaos:invoke` scope, deletion of the `CHAOS_ENABLED` variable. No downstream code depends on chaos being available. Fully reversible in one PR.

## Verification

- Unit tests: each gate rejects independently; two open gates + one closed → 403 with the right `error_code`.
- Startup test: `CHAOS_ENABLED=true, ENVIRONMENT=production` raises `AssertionError` before uvicorn binds.
- Terraform test: `terraform plan` with `chaos_enabled=true, environment=production` fails with the validation message.
- Integration test: MCP adapter in a chaos-enabled test env exposes `kill_consumer`; adapter in a chaos-disabled env does not list it in its tool registry.

## Pointers

- `backend/app/config.py` — the boot-time assertion (to be added in Wave 1 PR #4)
- `infra/variables.tf` — the Terraform validation
- `mcp-server/chaos_tools/` — the tool implementations (to be created in Wave 1 PR #4)
- Related ADRs: [0006 — MCP server as thin adapter](0006-mcp-server-thin-adapter.md), [0007 — Machine-principal scope model](0007-machine-principal-scope-model.md)
