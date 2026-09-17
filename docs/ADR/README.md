# Architecture decision records

Every decision that constrains future work gets a record here. An ADR is written once and then left alone: when a decision changes, a later ADR amends or supersedes it and both stay on the shelf, so the reasoning that was true at the time is still readable.

Status is stated in each file's own header; the table below mirrors it. All 26 are accepted — the interesting column is the last one, which says where a decision has since been narrowed.

| # | Decision | Status | Later movement |
|---|---|---|---|
| [0001](0001-outbox-vs-cdc.md) | Outbox pattern over CDC for state → event publishing | Accepted | — |
| [0002](0002-json-schema-vs-protobuf.md) | JSON Schema over Protobuf for Kafka message contracts | Accepted | — |
| [0003](0003-rls-as-defense-in-depth.md) | Postgres row-level security as defense-in-depth, not primary tenant isolation | Accepted | Superseded in part by 0026 |
| [0004](0004-tenant-id-in-kafka-partition-key.md) | Composite `{tenant_id}:{user_id}` Kafka partition key | Accepted | — |
| [0005](0005-llm-features-fail-open.md) | LLM-driven features fail open | Accepted | — |
| [0006](0006-mcp-server-standalone-process.md) | Serve MCP as a standalone process from the platform codebase | Accepted | — |
| [0007](0007-machine-principal-scope-model.md) | Machine principals with a scope model separate from human roles | Accepted | — |
| [0008](0008-chaos-gating.md) | Chaos framework is triple-gated and never enabled in production | Accepted | Extended by Wave 2 PR #58 |
| [0009](0009-consumer-lifecycle-and-supervision.md) | Consumer lifecycle and supervision | Accepted | Self-amended 2026-08-30 — worker liveness moved off the deep health check |
| [0010](0010-idempotency-record-lifecycle.md) | Idempotency record lifecycle | Accepted | 2026-08-30 addendum: the key is claimed before the action runs |
| [0011](0011-dag-pause-enforcement.md) | DAG pause is enforced by the resolver, not just recorded | Accepted | Amended by 0022 |
| [0012](0012-the-lab-is-invisible-to-the-agent.md) | The lab is invisible to the agent | Rule 1 accepted and shipped; rule 2 accepted-deferred | 2026-09-15 amendment extends rule 1 to response bodies |
| [0013](0013-release-before-rerun.md) | Release before rerun: ship the release, re-pin, then evaluate | Accepted | — |
| [0014](0014-sse-stream-token-transport.md) | SSE stream auth is a short-lived, job-bound stream token | Accepted | The v2 signature scheme it defines is superseded by agent-repo ADR 0023 |
| [0015](0015-force-rls-and-nonowner-app-role.md) | FORCE RLS, the non-owner `incident_app` runtime role, DB-level `audit_logs` immutability | Accepted | Superseded in part by 0026 |
| [0016](0016-defer-principal-scoped-tools-list.md) | Defer principal-scoped `tools/list` and blast-radius gate 3 | Accepted | Records two standing contradictions it knowingly accepts |
| [0017](0017-saga-compensation-settlement.md) | Compensation steps are real jobs; a COMPENSATING saga settles COMPENSATED or FAILED | Accepted | — |
| [0018](0018-production-kafka-posture.md) | Production Kafka is not provisioned | Accepted | — |
| [0019](0019-stale-running-recovery-sweep.md) | The stale-RUNNING recovery sweep dead-letters, never re-publishes | Accepted | Amended by 0021 §3 and 0023 |
| [0020](0020-outbox-relay-single-writer.md) | The outbox relay is single-writer via a Postgres advisory-lock leader gate | Accepted | — |
| [0021](0021-bounded-execution-and-non-blocking-dispatch.md) | Processor execution is bounded, and dispatch never blocks the poll loop | Accepted | Amends 0019 §3 |
| [0022](0022-promotable-only-resume-sweep-and-dependency-cascade.md) | Promotable-only resume sweep; a stranded parent cascades CANCELLED | Accepted | Amends 0011 §2 |
| [0023](0023-dispatcher-sweep-ownership.md) | A sweep acts only on a row it can prove it owns, and only once per window | Accepted | Amends 0019 §3 and 0021 §2 |
| [0024](0024-tenant-enrolment-policy.md) | Public registration may found a tenant or join the default one, and nothing else | Accepted | — |
| [0025](0025-alert-severity-vocabulary.md) | The alert severity vocabulary is `low \| info \| warning \| critical` | Accepted | — |
| [0026](0026-strict-tenant-isolation-and-declared-platform-scope.md) | Strict `tenant_isolation`: an unscoped statement is refused, cross-tenant work declares itself | Accepted | Amends 0003 and 0015 |

## Writing one

Number it next in sequence, state the status in the header, and say what was decided, what was rejected and what it costs. Once merged, the file is not rewritten — amend it from a new ADR and link both ways.
