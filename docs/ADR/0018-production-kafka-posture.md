# ADR 0018 — Production Kafka is not provisioned

**Status:** Accepted · **Date:** 2026-08-09 · **Owner:** Platform

> **This ADR records an absence.** It corrects documentation that described infrastructure which
> was never written, and it deliberately declines to write that infrastructure now. The point is
> that a future reader can tell a decision from an oversight — and that the docs cannot silently
> drift back, because a CI step now fails if they do.

## Context

The 2026-08 audit raised **F2-07**: four documents (`CLAUDE.md`, `README.md`, `docs/KAFKA.md`,
`docs/ARCHITECTURE.md`) stated that production Kafka runs on Amazon MSK, provisioned by a
Terraform module under `infra/`. No such file has ever existed in any ref of this repository.
`docs/ARCHITECTURE.md` went further and described a recovery story — "MSK auto-recovers (multi-AZ, 3-broker replication)" —
for a cluster that was never created.

The gap is not only cosmetic. Two things followed from it:

- `infra/ecs.tf` passed **no** `KAFKA_BOOTSTRAP_SERVERS` to the backend task definition, and
  `backend/app/config.py` defaults `kafka_bootstrap_servers` to `localhost:9092`. A real
  `terraform apply` + ECS deploy therefore produced a platform that answers health checks, accepts
  job submissions, writes outbox rows — and executes nothing, because every producer and consumer
  dials a broker on its own loopback interface. The API returns 202 and the job never moves. There
  is no alarm for "the broker you were configured to reach does not exist".
- `OTLP_ENDPOINT` was likewise never passed, so the traces the observability docs describe were
  never exported from a deployed task either.

Nothing in ADRs 0001–0017 claims MSK exists; the claim lived only in prose. **This ADR therefore
contradicts no accepted decision** — it contradicts documentation, which is the weaker artifact and
the one that was wrong.

## Decision

**Production Kafka is deliberately not provisioned. Do not write the MSK Terraform module.**

Three parts, all landed together with this ADR:

1. **The documentation says what is true.** Every claim that MSK exists, or that an MSK Terraform
   module exists, is replaced with: Kafka runs as Redpanda locally; production Kafka is not yet
   provisioned. A CI step in the `infra` job greps `CLAUDE.md`, `README.md` and `docs/` for the
   phantom module's filename and fails the build on any hit. Prose-only corrections decay — this
   finding is the evidence — so the correction is enforced mechanically.

2. **The ECS deploy path is opt-in.** The `Build & Deploy to ECS` job in `.github/workflows/ci.yml`
   is gated on the `ENABLE_ECS_DEPLOY` **repository variable** (a `vars.*` condition, not a secret,
   so the gate is legible in the workflow file). The variable is unset, so the job reports
   *skipped* rather than attempting a rollout to infrastructure nobody consumes. Re-enabling means
   configuring the AWS credential secrets and setting the variable to `'true'` — and means every
   master merge deploys to real infrastructure.

3. **The first real deploy is a variable away, not a rediscovery.** `infra/variables.tf` gains
   `kafka_bootstrap_servers` and `otlp_endpoint` (both `string`, both defaulting to `""`), and
   `infra/ecs.tf` builds the backend container's `environment` list with `concat()`: each variable
   contributes its env entry **only when non-empty**. Setting `kafka_bootstrap_servers` to an MSK
   cluster's bootstrap string, a self-managed broker, or an external service is all that stands
   between the current state and a deployment that executes jobs.

   The omission-when-empty shape is load-bearing. An unconditional
   `{ name = "KAFKA_BOOTSTRAP_SERVERS", value = "" }` would *override* the application default with
   a differently-broken value: instead of dialing `localhost:9092` it would dial nothing at all,
   with a client-side parse error at a different layer. That is a new failure mode, not a fix.
   Omitting the entry leaves exactly one documented behaviour — the app default — and leaves this
   ADR as the explanation for why job execution does not work on an ungated deploy.

## Alternatives rejected

**Write the MSK Terraform module now.** Rejected on three grounds.

- *Zero consumers.* Nothing consumes the AWS deployment today. The evals and the demo run against
  `docker compose` with a digest-pinned image; the ECS path has no user.
- *Cost.* MSK is by far the most expensive resource in this stack, and it would sit idle.
- *It would ship never-applied and never-tested* — the same fiction in a new form. The audit's
  F2-06 names this class directly (decorative infrastructure: a module that claims to work and has
  never been exercised). Converting a false document into a false Terraform module is not progress;
  it moves the untruth somewhere harder to notice.

**Change the application default from `localhost:9092` to something that fails loudly.** Not taken
here. The local-dev default is correct for local dev, which is where the platform actually runs,
and changing it is a behaviour change affecting every developer to fix a deployment path that is
now gated off. If a production deploy is ever enabled, a boot-time refusal to start with an
unconfigured broker in `ENVIRONMENT=production` is the right follow-up — in the same slice that
flips `ENABLE_ECS_DEPLOY`, not before it.

## Consequences

- **A `terraform apply` today builds a cluster with no Kafka.** Deployed workers would accept jobs
  and never execute them. This is stated here, in `CLAUDE.md`, in `docs/KAFKA.md`, in
  `docs/ARCHITECTURE.md`'s failure-mode catalog, and in the `kafka_bootstrap_servers` variable
  description — five places a reader might arrive from. It is a known, accepted state, not a bug
  to be re-filed.
- **`docs/ARCHITECTURE.md`'s Kafka recovery narrative no longer describes MSK multi-AZ
  auto-recovery.** The parts that are true of any broker — outbox rows accumulate rather than being
  lost, the producer lazily restarts, consumers resume from committed offsets — are retained,
  because they are properties of this codebase and hold against Redpanda locally.
- **`docs/ROADMAP.md` carries the deferral** as an explicit item (provision a production broker,
  then flip `ENABLE_ECS_DEPLOY`), so the decision is tracked rather than lost.
- **Terraform validation stays static.** The new variables are inert at `terraform validate` time;
  the `infra` CI job's `fmt -check` → `init -backend=false` → `validate` sequence covers the
  `concat()` change without credentials.

## Revisit trigger

Reopen this decision when a **real AWS consumer** appears — concretely, the Phase 8 staging
environment or the Phase 13 disaster-recovery work. At that point the sequence is: choose a broker
(MSK, or Redpanda on ECS if the cost of MSK is still not justified by the traffic), provision it,
set `kafka_bootstrap_servers`, apply, verify a job executes end-to-end on the deployed stack, and
only then set `ENABLE_ECS_DEPLOY`. Flipping the deploy gate before job execution is verified
reproduces exactly the state this ADR exists to end.
