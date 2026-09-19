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
| [0027](0027-control-loop-pause-closed-enum.md) | One hook pauses a background loop, and the enum of loops is closed | Accepted | Adds `BlastRadius.SINGLE_LOOP`; the enum excludes the Kafka consumer groups `kill_consumer` already stops; self-amended 2026-09-17 — pausing the resume sweep suspends the backstop 0022 and 0011 rely on, and only that pause's TTL heals the world |
| [0028](0028-outbox-relay-heartbeat-and-delivery-reading.md) | The outbox relay records each pass, and one reading reports delivery | Accepted | Adds `outbox:relay:last_tick` and the `get_outbox_status` tool; builds on 0020 and 0027 |
| [0029](0029-stranded-chain-and-lab-pause-are-manufactured.md) | A stranded chain and a lab pause are manufactured, not found | Accepted | Adds `pause_dag_chaos` and three `create_stuck_dag` inputs; the boot-seeded DAG is drained, not a fixture; the lab pause writes the operator's own `dag:paused:*` flag and so sits outside `chaos:*` deliberately; builds on 0011, 0012, 0022 and 0027 |
| [0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md) | Breaker state is published where every process can read it, and no reading is invented to fill a promised field | Accepted | Adds `breaker:state:<name>` and the `get_circuit_breakers` / `get_slo_status` tools, plus twelve `get_postgres_health` output fields; two of them are null with a reason because `pg_stat_statements` cannot answer a one-minute percentile; builds on 0006, 0012, 0015 and 0028 |

| [0031](0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md) | A held pool and a degraded dependency are flagged, not broken | Accepted | Adds `saturate_db_pool` and `degrade_downstream`; the pool that starves is the API/worker process's, not the MCP process's own, so a pool reading has to say which one it read — and WO-R3-217's reading says the answering process's, which leaves the held worker pool unobservable ([0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md), "What this does not close"); the holder is a lab task and not a twelfth member of 0027's enum; under the downstream flag a sync that synced nothing fails the job, because otherwise nothing the agent reads moves; builds on 0006, 0008, 0012, 0026 and 0027 |
| [0032](0032-a-sticky-kill-re-arms-and-its-window-is-absolute.md) | A sticky kill re-arms, and its window is absolute | Accepted | Adds a `sticky` flag to `kill_consumer` and the `chaos:kill_sticky:*` key, so a Tier-1 `restart_consumer_group` can genuinely fail; the flag is deleted and found back rather than hidden, which is what keeps that action's reply truthful and leaves 0012 rule 1 intact; the window is absolute, so no number of restarts extends it; builds on 0008, 0009 and 0012 |
| [0034](0034-a-slow-query-is-manufactured-where-the-server-can-see-it.md) | A slow query is manufactured where the database server can see it, not where the application would feel it | Accepted | Adds `slow_db_queries` and the `chaos:db_query:slow` key; an application-level delay would move no reading the agent has, so the fault is real long-running statements in the worker process, seen through `pg_stat_activity` — which is also why the MCP process's `pool_*` counters stay normal and A1's "queries slow, pool fine" signature falls out of where the fault lives; two queries offset by half a chunk keep the reading above the slow threshold at every instant instead of a quarter of the time; restates WP-8.2's `p95_query_ms_1m` evidence in the terms [0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md) left available, and pairs with [0031](0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md); builds on 0006, 0008, 0012, 0026 and 0027 |

## Writing one

Number it next in sequence, state the status in the header, and say what was decided, what was rejected and what it costs. Once merged, the file is not rewritten — amend it from a new ADR and link both ways.
