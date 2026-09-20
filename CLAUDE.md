# CLAUDE.md — Incident & Workflow Platform

## Project Overview

A production-style **Incident & Workflow Platform** — an internal enterprise operations tool where teams submit jobs (CSV upload, report generation, bulk API sync, document analysis), watch live progress, inspect failures/retries/audit history, and where admins can replay failed jobs and inspect request traces.

This is NOT a generic CRUD app. It intentionally forces: concurrency model decisions, structured logging with trace IDs, retry/idempotency patterns, background job orchestration, event-driven architecture, real debugging workflows, and production deployment concerns.

The project is structured as a sequence of milestone phases (Phase 1 through Phase 13). Each phase ships as one or more pull requests against `master`. The plan is *aspirational on the right-hand side* (Phases 8, 9, 11, 13 are not yet built) and *historical on the left* (Phases 1–7, 10, 12 are merged and running) — see the per-phase status markers in the milestone plan below.

---

## Documentation map

This file (`CLAUDE.md`) is the high-signal index. Treat it as the entry point — everything below points at deeper docs when detail matters.

**Prose conventions** (documented so reviews don't rediscover them as violations):
- Em-dashes ( — ) are used freely across docs, ADRs, and PR bodies. Not stylistic churn — match the pattern.
- ADR status labels appear in the H1 subtitle only. The doc-map above intentionally omits *(proposed)* / *(accepted)* markers to avoid drift when statuses flip.
- Machine-principal identity on `audit_logs` uses `principal_type` (string discriminator) + `principal_id` (plain UUID, no FK). Not `service_account_id` — that shape was rejected because the same column has to reference either `users.id` or `service_accounts.id` depending on the discriminator. See [ADR 0007](docs/ADR/0007-machine-principal-scope-model.md).
- The agent contract with this platform is validated by snapshot-testing the agent repo against the pinned platform image (agent-repo ADR 0007). There is deliberately no `agent-tools.json` artifact — that shape was proposed in early drafts and dropped when [ADR 0006](docs/ADR/0006-mcp-server-standalone-process.md) landed.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — runtime topology, request lifecycles (annotated traces for the 5 most-touched paths), concurrency model, failure mode catalog, auth & tenant matrix, cost model
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — every table, every column, every index, every constraint, with a one-line *why*
- [`docs/KAFKA.md`](docs/KAFKA.md) — topic catalog, schema-evolution rules, partition strategy, consumer-group catalog with failure isolation
- [`docs/REDIS.md`](docs/REDIS.md) — key catalog (writer / reader / TTL / eviction-safe?), what degrades when Redis dies
- [`docs/ADR/`](docs/ADR/) — architecture decision records. Read these to understand *why* the platform looks the way it does. [`docs/ADR/README.md`](docs/ADR/README.md) is the full index with every status; all 32 are listed below:
  - [0001 — Outbox over CDC](docs/ADR/0001-outbox-vs-cdc.md)
  - [0002 — JSON Schema over Protobuf](docs/ADR/0002-json-schema-vs-protobuf.md)
  - [0003 — Postgres RLS as defense-in-depth](docs/ADR/0003-rls-as-defense-in-depth.md)
  - [0004 — Composite tenant_id:user_id Kafka partition key](docs/ADR/0004-tenant-id-in-kafka-partition-key.md)
  - [0005 — LLM features fail open](docs/ADR/0005-llm-features-fail-open.md)
  - [0006 — MCP server as a standalone process from the platform codebase](docs/ADR/0006-mcp-server-standalone-process.md)
  - [0007 — Machine principals with a scope model separate from human roles](docs/ADR/0007-machine-principal-scope-model.md)
  - [0008 — Chaos framework is triple-gated and never in production](docs/ADR/0008-chaos-gating.md)
  - [0009 — Consumer lifecycle and supervision](docs/ADR/0009-consumer-lifecycle-and-supervision.md) — best-effort start with backoff, and the 2026-08-30 self-amendment that moved worker liveness off the deep health check
  - [0010 — Idempotency record lifecycle](docs/ADR/0010-idempotency-record-lifecycle.md)
  - [0011 — DAG pause is enforced by the resolver, not just recorded](docs/ADR/0011-dag-pause-enforcement.md)
  - [0012 — The lab is invisible to the agent](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md) — rule 1 shipped v0.4.9 and now covers response bodies as well as descriptions (2026-09-15 amendment); rule 2 deferred to post-rerun; the withheld audit set is **two** prefixes since the 2026-09-20 amendment — `chaos.` (a fault going in) and `lab.` (the environment reset's `lab.world_reset` boundary row, whose payload is the reset's counters and therefore the mechanism list), both under the one `chaos:invoke` condition. A second prefix rather than `chaos.world_reset` because to the `/demo` console the newest `chaos.*` row *is* the fault, so a boundary filed there would read as one
  - [0013 — Release before rerun](docs/ADR/0013-release-before-rerun.md) — the 2026-08 campaign ships a release first, then the commander re-pins, then the eval runs
  - [0014 — SSE stream auth is a short-lived, job-bound stream token](docs/ADR/0014-sse-stream-token-transport.md) — `POST /jobs/{id}/stream-token` mints it; the stream takes it as a query parameter because `EventSource` cannot send headers
  - [0015 — FORCE RLS, the non-owner `incident_app` role, and DB-level `audit_logs` immutability](docs/ADR/0015-force-rls-and-nonowner-app-role.md) — amended in part by 0026
  - [0016 — Defer principal-scoped `tools/list` and blast-radius gate 3](docs/ADR/0016-defer-principal-scoped-tools-list.md) — records the two standing contradictions it accepts
  - [0017 — Saga compensation steps are real jobs, and a COMPENSATING saga settles COMPENSATED or FAILED](docs/ADR/0017-saga-compensation-settlement.md)
  - [0018 — Production Kafka is not provisioned](docs/ADR/0018-production-kafka-posture.md) — no broker in `infra/`, ECS deploy gated off, `KAFKA_BOOTSTRAP_SERVERS` omitted unless set
  - [0019 — Stale-RUNNING recovery sweep dead-letters, never re-publishes](docs/ADR/0019-stale-running-recovery-sweep.md) — worker-crash orphans go to the DLQ, not back onto `job.submitted`; revisit once a job can prove it did not partially execute
  - [0020 — The outbox relay is single-writer via a Postgres advisory-lock leader gate](docs/ADR/0020-outbox-relay-single-writer.md) — the sweeps stay compare-and-set guarded
  - [0021 — Processor execution is bounded, and dispatch never blocks the poll loop](docs/ADR/0021-bounded-execution-and-non-blocking-dispatch.md) — amends 0019 §3
  - [0022 — Promotable-only resume sweep, and a stranded parent cascades CANCELLED](docs/ADR/0022-promotable-only-resume-sweep-and-dependency-cascade.md) — amends 0011; the sweep's limit now bounds promotable work, and `CANCELLED` gains a second, non-saga writer
  - [0023 — A dispatcher sweep only acts on a row it can prove it owns, and only once per window](docs/ADR/0023-dispatcher-sweep-ownership.md) — amends 0019 and 0021; `requeued_at` de-duplicates the stale-PENDING backstop, `heartbeat_at` plus a compare-and-set stop one replica dead-lettering another's running job
  - [0024 — Public registration may found a tenant or join the default one, and nothing else](docs/ADR/0024-tenant-enrolment-policy.md) — unauthenticated `tenant_slug` no longer enrols into an arbitrary existing tenant (403); existing-tenant enrolment moves behind `POST /auth/tenant/members`, admin-only, tenant taken from the token
  - [0025 — The alert severity vocabulary is `low | info | warning | critical`](docs/ADR/0025-alert-severity-vocabulary.md) — user decision; `low` added so the commander's noise branch is reachable from a real alert, `medium`/`high` declined as duplicates of `warning`/`critical`, `unknown` declined as a receiver's default rather than a producer's assertion
  - [0026 — Strict `tenant_isolation`: an unscoped statement is refused, and cross-tenant work declares itself](docs/ADR/0026-strict-tenant-isolation-and-declared-platform-scope.md) — the ADR 0003 bootstrap branch made every tenant policy fail open (plat #192 proved it live); policies now match on the tenant alone, the worker loops / migrations / seed scripts declare `app.tenant_scope = 'platform'`, and only the pre-auth `service_accounts` read keeps a narrow SELECT-only exception
  - [0027 — One hook pauses a background loop, and the enum of loops is closed](docs/ADR/0027-control-loop-pause-closed-enum.md) — `pause_control_loop` writes `chaos:pause:<loop>`; the enum is the eleven background loops, the three Kafka consumer groups an earlier draft carried are dropped (`kill_consumer` already stops those), the resume sweep is added because Family C cannot strand a `WAITING` child without it, and `BlastRadius` gains `single_loop`. **2026-09-17 amendment:** stranding that child takes *both* mechanisms — `kill_consumer('dependency-resolver')` and this pause — and only the pause's TTL heals the world, because the resolver's `job.completed` is already consumed; pausing this one member suspends the correctness backstop 0011 and 0022 depend on, which is bounded and swept in a lab and would fail silently in production
  - [0028 — The outbox relay records each pass, and one reading reports delivery](docs/ADR/0028-outbox-relay-heartbeat-and-delivery-reading.md) — the relay stamps `outbox:relay:last_tick` inside every pass it completes (one-second resolution, where the CloudWatch gauge has sixty), and the `get_outbox_status` read tool reports the waiting count, the oldest and newest waiting ages, the last real delivery and that pass time, all against the database's clock; an absent pass time is unknown with a reason, never an age
  - [0029 — A stranded chain and a lab pause are manufactured, not found](docs/ADR/0029-stranded-chain-and-lab-pause-are-manufactured.md) — the boot-seeded three-node DAG is a *drained* DAG, not a fixture (its parent is `completed`, so the resolver or the resume sweep promotes both children within seconds of first boot, and `make eval-reset` re-anchors their timestamps without restoring their statuses — restoring them would race the 10 s sweep); so `create_stuck_dag` gains `root_status` / `child_age_seconds` / `failed_step` to write the `resolver_stall` and `downstream_child_failed` shapes, and the completed-root shape is stated NOT to hold by itself — it needs `kill_consumer('dependency-resolver')` plus `pause_control_loop('resume_unblocked_waiting')`. `pause_dag_chaos` writes the operator's own `dag:paused:<root>` flag with the same value, default and bounds `pause_dag` uses, so it is the one chaos hook whose keys sit outside `chaos:*` deliberately (teardown is `_clear_dag_pauses`, which already existed) — and the one thing the agent can tell apart is an absence, not a name: a lab pause writes `chaos.tool_invoked`, which is withheld, where an operator pause writes `agent.tool_invoked`
  - [0030 — Breaker state is published where every process can read it, and no reading is invented to fill a promised field](docs/ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md) — the breaker registry is a module-level dict inside the worker while the MCP reader is another process (ADR 0006), so a read tool walking it reported every breaker closed; each breaker now records its state under `breaker:state:<name>` (platform namespace, outside `chaos:*`, 24 h TTL, written on every state change and at most once a minute while calls flow) and gains a wall-clock `last_state_change_at` beside the monotonic `_opened_at` plus a failure *class* — `timeout` / `connection` / `other`, never the message. The second half is about what is *not* built: `pg_stat_statements` is absent from this repo and from the eval world's Postgres, and it could not produce `p95_query_ms_1m` if it were present (the view is cumulative per statement since the last reset, with no percentiles and no way to narrow to a minute), so that field and `slow_query_count_1m` ship null with one of three fixed reasons and two readings that *can* be taken — `longest_active_query_ms` and `active_queries_over_slow_threshold`, both from `pg_stat_activity` and therefore the same answer from any process — are added beside them. The `pool_*` fields describe the pool of the process that answered the call and say so in every description, because the API and worker pools are invisible from the read surface
  - [0031 — A held pool and a degraded dependency are flagged, not broken](docs/ADR/0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md) — the two Family A hooks. `saturate_db_pool` writes `chaos:db_pool:hold` and a chaos-only task in the **worker** process holds that many sessions open, because the MCP process's own pool is the one every tool call needs — saturating it blinds the reader instead of producing a fault; the clamp always leaves four connections acquirable (`MIN_FREE_CONNECTIONS`), so the eleven loops slow down rather than stop, and the consequence is that `get_postgres_health` has to report the worker's pool or this hook has no observable (divergence H7's sibling, WO-R3-217). The holder is a lab task, deliberately **not** a twelfth member of 0027's closed enum: its off switch is its own key. `degrade_downstream` writes `chaos:downstream:bulk_api_sync`, read once per job, so the shipped `bulk-api-sync` breaker does the tripping at its own threshold of 3 — and only `mode=fail` opens it. A job whose every endpoint call failed is itself failed, because the per-endpoint error counts live in a result payload no operational tool reads, so otherwise the breaker opened and every tool still read healthy — flag-gated as shipped, **unconditional since the 2026-09-19 amendment** (owner decision O-31 D4, WO-R3-322): the flag chooses the injection, never whether a sync that synced nothing was a success. A partial failure still completes with its errors counted; the objective a dead-lettered one spends is `job_completion_rate`, and no tool contract moves
  - [0032 — A sticky kill re-arms, and its window is absolute](docs/ADR/0032-a-sticky-kill-re-arms-and-its-window-is-absolute.md) — the one fault that outlives the action that normally fixes it, so a first remediation attempt can genuinely fail. `kill_consumer(sticky=true)` writes `chaos:kill_sticky:<group>` beside the kill flag, holding the absolute deadline `ttl_seconds` gives; the kill-state read fetches both in one MGET and re-arms the flag with `PXAT` at that instant whenever it finds the flag gone and the window open. So `restart_consumer_group` is **unmodified**: it deletes the flag, truthfully answers `kill_key_cleared: true`, and the supervisor finds the flag back before it restarts anything — which is what keeps [ADR 0012](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md) rule 1 intact where a refusal or an explanation would have leaked the lab. The window is absolute in two independent ways (the deadline in the marker's value, the marker's own Redis TTL), so no number of restarts extends it; an unreadable marker fails open; the re-arm writes only under `CHAOS_ENABLED`
  - [0033 — Each process publishes its own pool gauge, and an absent process is not a healthy one](docs/ADR/0033-each-process-publishes-its-own-pool-gauge.md) — the gap [ADR 0030](docs/ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md) named and [ADR 0031](docs/ADR/0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md) recorded as D1, closed by the pattern 0030 already used for breakers. `saturate_db_pool` holds the API/worker process's connections and every `pool_*` field describes the process that answered the call, so the fault was real and every tool the agent has read healthy. Each process now records its own pool under `pool:state:<process>` — a closed pair, `api_worker` and `mcp` — and `get_postgres_health` gains a `pools` group beside the five unchanged `pool_*` fields, with an empty group always carrying `pool_gauges_unknown_reason`, because an empty list that reads as "no process has a pool problem" is the failure the order exists to remove. Two deliberate inversions of the breaker pattern, both from a pool reading being a *sample* where a breaker's state is *latched*: the TTL is 60 s rather than 24 h, so a process that stops publishing drops out of the listing instead of freezing at its last healthy number, and a publisher task per process writes it rather than a hook on a state change — not the metrics emitter, which is a no-op outside production, and not the worker's metrics loop, which `pause_control_loop` can stop, because a lab hook must not be able to blind the reading another lab hook exists to produce. The key is a platform key, so the environment reset does not carry it and does not need to
  - [0035 — The agent reports its run to the platform, and the platform never shows the agent what it reported](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md) — the live demo's platform half. The console had to show two things that existed only inside the responder's process (what it is doing now, what it concluded) and inferring them from the audit log is wrong in exactly the moments a demo is about — three reads of `get_consumer_lag` look identical whether the responder is investigating or verifying. So the responder *reports*: two `[commander: telemetry]` MCP tools under a new sixth scope `agent_runs:write` write the `agent_runs` table, four rules govern the writes (upsert by run id, append-only phase history that appends on a state *change*, a terminal state that closes the run and refuses a later report, a briefing written once), and human operators read it over REST. The prefix is the contract: no model chooses these calls — the responder's loop makes them and its planner filters `[commander:` out of the tool list it offers its model, exactly as it filters `[chaos:`. The half that took the thinking is that **the audit log is a read surface for this table under another name**, so the calls audit as their own `agent.run_reported` stream and `hidden_audit_action_prefixes` withholds it *from* the principal holding the write scope — the inverse of the `chaos.` condition, in the same function. One wire name deliberately differs from its column (`run_label` → `agent_runs.scenario`) because [ADR 0012](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md)'s registry screen bans that word from any non-chaos tool's `tools/list` surface and a new exemption was declined: the responder's principal holds the scope, so it can read these descriptions, and whether its model does depends on a filter in the other repository. Recorded rather than solved: `agent_runs` is not swept by the eval reset, and nothing here can tell a responder that stopped reporting from one whose reporter failed — there is no heartbeat, by design
  - [0036 — The environment reset closes a breaker, and a registry it cannot restart honours it](docs/ADR/0036-the-reset-closes-a-breaker-and-a-registry-it-cannot-restart-honours-it.md) — three gaps in `make eval-reset` (WO-R3-310/311/315) with one shape: state outside the namespaces it sweeps, and nothing in its own output to say so. So every step now reports a count (`hot_set_reseeded`, `breakers_reset`, `agent_runs_closed`) and a tripwire fails if one is added without one. A breaker is reset by rewriting `breaker:state:<name>` *closed with the failure fields null* rather than deleting it, because an absent record is an unknown and not a closed breaker ([ADR 0030](docs/ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md)) — and because the registry is a module-level dict in a process the reset cannot restart ([ADR 0006](docs/ADR/0006-mcp-server-standalone-process.md)), which would write the same failure straight back, the reset raises `breaker:reset:at` first and every breaker honours it before it publishes again and before it refuses another call. The signal carries *when* the reset happened rather than a count, because the question is whether what a breaker remembers is older than the reset: a fault that arrives after it belongs to the world now running and is kept. An open `agent_runs` row is closed `failed` with a `closed_by: reset` entry appended to its own append-only `phase_history` and never deleted — the gap [ADR 0035](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md) recorded. No tool name, description, schema or scope moves, so there is no contract delta
  - [0037 — A run record carries the run: excerpts of the reasoning, not a copy of the trace](docs/ADR/0037-a-run-record-carries-the-run.md) — the demo's second take, platform half (WO-R3-328). [ADR 0035](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md) gave the console *where* the responder is; the first live take proved one state is not a story — the agent panel was empty, and even filled in there was nowhere to put the ranked explanations, the plan, or whether the verify poll agreed. So `agent_runs` gains seven columns in two shapes, and the shape is the decision: **latest-reading** (`hypotheses` ranked best-first, `plan`, `verification`, `budget`) and **append-only** (`verifications`, `steps`, with `steps_dropped`). The rejected option is the tempting one — storing the responder's tool output whole, which would make this table a second copy of an artefact that already exists and is already archived, and would put job payloads and error bodies on a screen nobody curated. Hence excerpts the caller truncates (280 / 280 / 400) with the wire **refusing** a longer value rather than cutting it: a silent cut stores something the caller did not write, and the refusal is what stops a trace store arriving by accident. One `step` per call, identified by `seq` — a repeat changes nothing (the reporter is fail-open and retries), the ledger is served sorted by `seq` rather than in stored order, and `/steps?after_seq=` is a tail read because offset pagination over a list that grows from the end hands a poller duplicates. The four new readings are filled in and **never cleared** by a report that omits them, where `current_hypothesis` / `last_step` keep 0035's replace-or-clear: the reporter now reports after every tool call, and clearing on omission would blank the panel this order exists to fill — changing the older pair to match was declined as the worse surprise. Also 15 minutes of lag history on the platform's side of the wire, under a TTL *longer* than the value key's on purpose (the value must be fresh-or-absent because backpressure gates on it; history is most wanted when the pass that writes it stopped), and comma lists on the human audit filter. [ADR 0012](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md) rule 1 is untouched: nothing here is readable by the principal that writes it, and there is still no read tool for `agent_runs`
- [`docs/postmortems/`](docs/postmortems/) — one file per incident (backfilled or written at the time). Format: Impact / Timeline / Root cause / Detection gap / Fix / Prevention rule adopted. Two so far: [0001 — v0.4.1 schema drift](docs/postmortems/0001-v0.4.1-schema-drift.md) and [0002 — the phantom supervisor](docs/postmortems/0002-phantom-supervisor.md). Both are dated records of what happened; they are history, not current state.
- [`docs/lessons/`](docs/lessons/) — case studies. [parallel-agent-campaigns.md](docs/lessons/parallel-agent-campaigns.md) is what went wrong running several agents over one checkout.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — open extension ideas, sized + categorized
- [`docs/README.md`](docs/README.md) — one line per document, for when you do not know which of the above you want
- [`runbooks/`](runbooks/) — machine-readable on-call playbooks; 8 files covering the 10 CloudWatch alarms and both SLOs
- **Workspace hub first:** this repo lives inside the `audit-ws` workspace, whose auto-loaded
  `CLAUDE.md` makes `../context/START-HERE.md` → `STATE.md` → `LESSONS.md` → `PROTOCOL.md` the
  mandatory reading order for every session, before this repo's own index below. Cross-repo state,
  the paid-run protocol, and the consolidated lessons ledger live there (created 2026-09-07).
- [`context/INDEX.md`](context/INDEX.md) — **session history; read it at the start of a session.**
  One line per session plus the findings that cost real time once: that the digest to pin is the
  index and not the linux/amd64 child, that `tools/list` needs no rows so an empty database was
  indistinguishable from a seeded one for the life of the project, that `AlertPayload` declares two
  fields the webhook never sends. Most of the campaign history is agent-side in
  `../incident-commander/context/INDEX.md` — the interesting failures have been on the seam between
  the two repos, so read both. See "Session history" below for how to add one.

---

## Current Implementation Status

What's actually shipped (as of the most recent merge):

| Phase | Status | Anchor PRs |
|---|---|---|
| 1 — Clean Backend Core | ✅ Complete | early `#1`–`#10` range |
| 2 — Background Execution | ✅ Complete | retry / dispatcher / processors |
| 3 — Frontend + Debugging Realism | ✅ Complete | dashboard, job detail, admin console |
| 4 — Production Deployment | ✅ Complete | Docker, Terraform, ECS, CloudWatch metrics |
| 5 — Hardening | ✅ Complete | rate limit, cache, load tests, mypy strict |
| 6 — Observability & Reliability | ✅ Complete | OTel `#21`, circuit breaker `#23`, SLOs + runbooks `#32` |
| 7 — Kafka + Advanced Patterns | ✅ Complete | foundations `#28`, CQRS + event sourcing `#29`, DAG + sagas `#30`, frontend `#31` |
| 8 — Platform Engineering & Scale | 🟡 Not started | — |
| 9 — Security Hardening | 🟡 Not started | — |
| 10 — AI / LLM Integration | ✅ Complete | DLQ triage `#34`, retry policy `#39`, NL queries `#40`, digests `#41` |
| 11 — Real-time Stream Analytics | 🟡 Not started | — |
| 12 — Multi-tenancy | ✅ Complete | model + auth `#35`, enforcement `#36`, RLS + partitioning + quotas `#37`, platform admin `#38` |
| 13 — Disaster Recovery & Chaos | 🟡 Not started | — |
| 14 — Real Job Processors | 🟡 Deferred (post-agent) | — |

**Runtime topology that's actually running** (post-Phase-12):

- **One FastAPI app** behind the ALB. `POST /jobs` is rate-limited (per-client + per-tenant), backpressure-gated, quota-checked, and writes both the job row and an `outbox_events` row in a single DB transaction.
- **Eight Kafka consumer groups** running concurrently inside the worker process (`worker_loop` in `app/workers/dispatcher.py`):
  1. `worker-dispatcher` — pops `job.submitted`, runs the actual processor
  2. `audit-writer` — appends `event.*` rows to `audit_logs`
  3. `sse-broadcaster` — bridges Kafka events to the Redis pub/sub channel SSE clients read
  4. `event-log` — appends every lifecycle event to the immutable `job_events` table (event sourcing)
  5. `read-model` — maintains Redis-backed denormalized per-tenant + per-user job-status sets (CQRS read side; tenant-keyed since Phase 12 PR D)
  6. `dependency-resolver` — promotes `WAITING` jobs to `PENDING` when their parents complete
  7. `saga-coordinator` — drives saga-level state and compensation on failure
  8. `llm-triage` (Phase 10) — calls Claude on every `job.dlq` to write a `JobTriage` row
- **Eleven background loops** also running in the same process:
  - **Outbox relay** — polls `outbox_events` every second and publishes to Kafka. Each completed pass stamps `outbox:relay:last_tick` in Redis, which is what lets a reader in another process tell an idle relay from a stopped one; `get_outbox_status` reports it ([ADR 0028](docs/ADR/0028-outbox-relay-heartbeat-and-delivery-reading.md))
  - **Delayed-retry promote** — moves exponentially-backed-off retries from a Redis sorted-set back into Kafka via the outbox
  - **DLQ replay promote** — fires operator-scheduled DLQ replays whose delay window has elapsed. **Claims, never pops** (WO-R2-21): due entries move to `jobs:dlq_replay_inflight` under a 60s claim and are `ack`ed on every outcome the pass can observe, so only a worker that dies mid-replay leaves a claim — and that one is reclaimed on a later tick instead of being silently discarded. It still does not re-enqueue a replay that failed on its merits. Writer side is ordered to match: audit row first, then the ZSET entry, in a savepoint with a `ZREM` compensation ([`docs/REDIS.md`](docs/REDIS.md#reader-semantics-for-jobsdlq_replay_delayed--claim-dont-pop))
  - **Resume-unblocked-waiting sweep** — promotes `WAITING` children once their DAG pause lifts; backstops missed promotions. Selects only rows with no unmet parent, oldest first behind a rotating cursor, so permanently-blocked children cannot starve it ([ADR 0022](docs/ADR/0022-promotable-only-resume-sweep-and-dependency-cascade.md))
  - **Stale-PENDING backstop** — re-publishes `PENDING` jobs left with no `jobs:delayed` timer by a crash window. Stamps `requeued_at` in the same transaction as the outbox insert, so a job is re-published at most once per 300s window instead of every pass for as long as the dispatcher is behind ([ADR 0023](docs/ADR/0023-dispatcher-sweep-ownership.md))
  - **Stale-RUNNING sweep** — dead-letters `RUNNING` jobs orphaned by a hard worker crash, after `STALE_RUNNING_THRESHOLD_SECONDS` (default 900). Never re-publishes them ([ADR 0019](docs/ADR/0019-stale-running-recovery-sweep.md)). Skips any job whose lease (`heartbeat_at`) is still live, so one replica cannot dead-letter another's running job, and compare-and-sets the recovery write against what its scan observed ([ADR 0023](docs/ADR/0023-dispatcher-sweep-ownership.md)). Its exclusion for this process's own in-flight jobs is time-bounded, not permanent — a local job stuck past its execution deadline is reclaimed too ([ADR 0021](docs/ADR/0021-bounded-execution-and-non-blocking-dispatch.md))
  - **Lease renewal** — checks in every 20s on the `RUNNING` jobs this worker holds, which is what makes the sweep above able to tell live work from a crash orphan across replicas. Stops renewing once a job is past its deadline plus grace, so a wedged worker cannot defend its own stuck job forever ([ADR 0023](docs/ADR/0023-dispatcher-sweep-ownership.md))
  - **SLO evaluation** — computes both objectives every `SLO_EVALUATION_INTERVAL_SECONDS` (default 300) and raises a `critical` Alert, and therefore a signed webhook, on a ≥14.4× error-budget burn. De-duplicated per window by `alerts.dedup_key` under a unique constraint, so a sustained burn pages once an hour rather than once a tick, and two replicas evaluating the same window cannot both alert. This is the alert webhook's only non-chaos producer — before it, `compute_all` had exactly one caller (a read-only admin endpoint) and no real platform condition ever created an alert (WO-R2-29)
  - **Metrics loop** — emits CloudWatch gauges (`QueueDepth`, `InFlightJobs`, `ConsumerLag`) and caches the lag in Redis for the backpressure check
  - **Digest loop** (Phase 10) — every `LLM_DIGEST_INTERVAL_HOURS` (default 24), generates a per-tenant incident summary via Claude and persists it to `incident_summaries`
  - **Idempotency reaper** — hourly DELETE of expired `idempotency_records` rows (closes ADR 0010's "no reaper" follow-up)

  Each of those eleven loops reads `chaos:pause:<loop>` once per iteration and skips that iteration's work while the key is set — one hook (`pause_control_loop`), one closed enum, one check per loop ([ADR 0027](docs/ADR/0027-control-loop-pause-closed-enum.md)). Registered only under `CHAOS_ENABLED=true`, and the check short-circuits on that flag before any Redis call. The eight consumer groups above are deliberately **not** in that enum: `kill_consumer` stops any consumer group by its group id, so a second mechanism for the same three would mean two keys and two ways for a teardown to miss one. One more task rides along under `CHAOS_ENABLED=true` and is **not** an enum member either — the pool holder (`app/workers/db_pool_hold.py`), which holds as many pooled connections as `chaos:db_pool:hold` asks for and always leaves four acquirable so these loops slow down rather than stop. It is a lab task, not a twelfth background loop, and its off switch is its own key ([ADR 0031](docs/ADR/0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md)).

**An MCP tool call that cannot be audited does not commit** (WO-R2-51). `agent.tool_invoked` is the only record that an action ran, so the `tools/call` envelope treats a failed audit write as fatal: `AuditWriteFailedError` is the one exception it deliberately lets escape, `get_db` rolls the request back, and the client gets a JSON-RPC internal error instead of a 200 for something nothing recorded. `record_tool_invocation` itself is unchanged in spirit — still savepoint-wrapped, still never raises — it just returns whether the row landed and lets the caller decide. The trigger that made this reachable on demand was `X-Request-ID`: caller-supplied, copied onto the row, and wider than the `String(255)` column, so a long enough header suppressed the audit record for an action that committed. `RequestContextMiddleware` now validates the header (bounded charset, 128 chars, fresh UUID if unusable — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#correlation-ids-are-validated-not-trusted-r2-51)).

**Swallowing a DB error means taking a SAVEPOINT first** (WO-R2-59). Postgres aborts the whole transaction on a failed statement, so a handler that converts a DB exception into a degraded result has also broken every later write in that request. Use `app/core/db_degrade.degrade_on_db_error` — a bare `except SQLAlchemyError` around a query whose failure you intend to survive is the bug, and SQLite will not tell you (`tests/conftest.py::AbortingSession` will).

**The cache tools are tenant-scoped, not just prefix-scoped** (WO-R2-54). `invalidate_cache_key` and `get_cache_key_info` take an exact Redis key from the caller. The prefix allowlist says a key is a platform cache namespace; it never said it was *yours*, and `cache:job:{tenant_id}:{job_id}` is allowlisted deliberately — force-refreshing a stale job read is the remediation the pair exists for. The tenant segment now comes from the authenticated principal (`app/mcp/tools/_cache_scope.py`, shared by both tools so the check cannot drift between the one that reads and the one that deletes). Note the read side is not a lesser case: existence, TTL and size of another tenant's cached job is an existence oracle, which withholding the payload does not close.

**MCP idempotency is a claim, not a receipt** (WO-R2-27). `tools/call` reserves the key *before* running the action — one `INSERT ... ON CONFLICT DO NOTHING`, and winning it is what authorises execution — then attaches the response with an UPDATE it cannot lose. The old lookup-then-store shape let two concurrent calls on one key both execute, with the loser dying on `uq_idempotency_scope` after its Tier-1 effect had landed. Consequences worth knowing before touching `app/mcp/handlers.py`: `response_json` is nullable and NULL means "claimed, not yet answered"; on Postgres the second caller *blocks* on the first's uncommitted row rather than failing, which is the serialisation; and every path that does not complete a claim must release it, because the envelope commits the request transaction even on tool errors. See [ADR 0010](docs/ADR/0010-idempotency-record-lifecycle.md)'s 2026-08-30 addendum.

See [`docs/KAFKA.md`](docs/KAFKA.md) for the full consumer-group catalog (failure isolation, partition strategy, schema-evolution rules) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#per-task-responsibilities) for the worker-loop responsibilities table.

Per-PR breakdown of Phase 7 specifically (for reference when reading the code):

- **`#28` — foundations**: JSON Schema registry validating every Kafka payload, `BackpressureError` (503) on `POST /jobs` when consumer lag exceeds threshold, Redpanda-via-Testcontainers integration test.
- **`#29` — read side**: `job_events` table + `EventLogConsumer` (`UNIQUE (kafka_topic, kafka_partition, kafka_offset)` for redelivery dedup), `ReadModelProjector` maintaining Redis sets keyed by `job_id` (idempotent under at-least-once), `GET /admin/jobs/{id}/timeline` and `GET /admin/stats`.
- **`#30` — orchestration**: `job_dependencies` table, `JobStatus.WAITING` / `CANCELLED`, `SagaStatus` enum, `POST /sagas` creates a chain of dependent jobs, `SagaCoordinator` cancels downstream and enqueues `{type}.compensate` jobs on dead-letter.
- **`#31` — frontend**: Sagas browse / create / detail pages, Kafka event timeline on `JobDetailPage` (admin only), CQRS stats overview tab, optional dependencies field on the job form, `backpressure` 503 toast.
- **`#32` — Phase 6 gaps**: `GET /admin/slos` with budget-remaining + burn-rate per objective, `runbooks/*.yaml` (7 runbooks for every CloudWatch alarm and SLO), `GET /admin/runbooks/{id}`, SLO scorecards + runbook modal in the admin UI, two new fast-burn alarms in Terraform.

Phase 10 — AI / LLM Integration:

- **`#34` — DLQ triage**: `app/services/triage.py` calls Claude on every `job.dlq`. Pydantic-typed analysis (root_cause_category, summary, suggested_fix, is_retryable, confidence) persisted to `job_triages`. Admin UI shows the analysis on the DLQ row + the job detail page.
- **`#39` — LLM-guided retry policy**: after the first deterministic retry, the worker consults Claude to decide retry-with-backoff (with recommended seconds) vs dead-letter-now. Falls back to deterministic on any error. Audit log records `dead_lettered_by: llm_retry_policy` + reasoning, and the same value is stamped on `jobs.dead_lettered_by` so the admin DLQ tab can badge the row without a per-row audit join. Small purple LLM badge in the admin DLQ tab.
- **`#40` — Natural-language admin queries**: `POST /admin/query` translates plain English into a constrained `JobFilterSpec` Pydantic model, then runs through `JobService.list_jobs`. Injection-safe by construction (the LLM can only fill enum/literal fields). Off-by-default + 503 on any failure.
- **`#41` — Periodic incident summaries**: `_digest_loop` runs every N hours, aggregates per-tenant failure stats (counts + top recurring error fingerprints), asks Claude for a one-paragraph narrative + key concerns + recommended actions, persists to `incident_summaries`. New admin Digests tab.

Phase 12 — Multi-tenancy:

- **`#35` — model + auth context**: `tenants` table, `tenant_id` on every domain table, `DEFAULT_TENANT_ID` bootstrap (mixed-hex UUID for SQLite compat). JWT carries `tenant_id` claim. `tenant_id_var` contextvar logged in every structured entry.
- **`#36` — enforce tenant_id everywhere**: every repository / service / outbox call site threads tenant_id through. Per-tenant composite UNIQUE on `(tenant_id, idempotency_key)` replaces the global UNIQUE. Cascading signature changes across ~30 call sites.
- **`#37` — RLS + Kafka partition key + quotas**: Postgres row-level security policy on 6 tables (since extended by migration `a7e3d9c41f28` to all 11 tenant tables with `FORCE ROW LEVEL SECURITY` — see [ADR 0015](docs/ADR/0015-force-rls-and-nonowner-app-role.md)); `get_current_user` sets `app.tenant_id` via `set_config`; Kafka partition key changes to composite `{tenant_id}:{user_id}` across all 9 producer call sites; `tenants.rate_limit_per_minute` + `tenants.quota_jobs_per_month` columns; `check_tenant_limits` runs at top of `POST /jobs`. Header chip + admin Tenants tab. Testcontainers Postgres integration test.
- **`#38` — platform admin role**: `users.is_platform_admin` boolean (data migration backfills for default-tenant admins); `require_platform_admin` dependency; `?tenant_id=` cross-tenant scope override on list endpoints; CQRS read-model keyed by tenant_id (fixed a Phase 12 leak); self-service tenant creation at `/auth/register` via `new_tenant_name` (a *free* slug only — joining an existing tenant is 403 since [ADR 0024](docs/ADR/0024-tenant-enrolment-policy.md)); admin Tenants tab with create-modal + drill-down page.

---

## Agent-facing surface

The platform exposes an MCP server for machine principals such as `incident-commander`. The code lives at `backend/app/mcp/` and deploys as a standalone process from the same image, with handlers calling the service layer directly. Every MCP request authenticates as a scoped service account, is rate-limited per principal (`MCP_RATE_LIMIT_PER_PRINCIPAL`, default 120/min, keyed on `Principal.id` and enforced in `standalone.py` between parsing and dispatch — refusals return JSON-RPC `MCP_RATE_LIMITED` with HTTP 429), and writes an immutable audit record. Topology rationale and rejected alternatives (mounted sub-app, API-proxy à la Sentry, separate repo) are in [ADR 0006](docs/ADR/0006-mcp-server-standalone-process.md). Tool changes always start with a PR here, never in the agent repo.

### Repo boundary

- **This repo (`incident-platform`)** owns everything on the platform side of the wire: backend, frontend, service layer, `backend/app/mcp/` server code, chaos hooks, approvals subsystem, audit log, infra. The MCP server is part of the lock, not the visitor.
- **Agent repo (`incident-commander`)** owns the MCP *client* and the orchestrator around it: hypothesis engine, memory, skills, evals, demo compose that pulls the platform image by digest. It never imports platform code — it talks to `PLATFORM_MCP_URL` with a bearer token.

Same mental model as Sentry / GitHub / Stripe / Linear: the MCP server ships inside the org whose data it fronts; callers live wherever their builders keep them.

### Where it fits

- **Step 0 (merged, PR #51)** — ADRs 0006–0008, agent-facing surface section, naming locked in.
- **Wave 1 (~6 PRs, blocking):** machine principals + scoped tokens *(PR #52, merged)*; operator audit log; MCP scaffold at `backend/app/mcp/` + `get_consumer_lag` only; chaos framework + `kill_consumer` (`CHAOS_ENABLED=false` by default); alert emission (HMAC-signed webhook + `list_active_alerts` poll fallback); release engineering — pinned platform image (`ghcr.io/kudratsingh/incident-platform`) on tag, agent repo consumes by digest.
- **Wave 2 (lands during agent Phases 1–3):** full read tool set (`list_dlq_messages`, `get_trace` / `search_traces`, `get_deploy_history`, `get_dag_state`, `get_redis_health`, `get_postgres_health`, `get_incident` / `list_incidents`); remaining chaos hooks (`poison_message`, `saturate_redis`, `inject_latency`, `bad_deploy`) added JIT per scenario family.
- **Wave 3 (before agent Phase 6):** Tier 1 actions (`restart_consumer_group`, `replay_dlq_messages`, `pause_dag`, `invalidate_cache_key`) with `Idempotency-Key`; approvals subsystem (propose / approve / execute state machine with param-hash binding, expiry, single-use); approvals inbox view in the existing frontend; Tier 2 actions (`scale_service`, `rollback_deploy`, `modify_retry_policy`, `trigger_saga_compensation`) requiring approval reference + global kill switch on the agent principal.

### Design decisions locked in Step 0

- [ADR 0006 — MCP server as a standalone process from the platform codebase](docs/ADR/0006-mcp-server-standalone-process.md) — code at `backend/app/mcp/`, standalone process built from the same image, handlers call the service layer directly. Import-linter rule enforces `app.mcp → app.services` one-directional — contracts in `[tool.importlinter]` (pyproject.toml), run by the `Import contracts` step of the `lint` CI job and `make lint-imports`. Revisit trigger: collapse to mounted if operating two services proves to be real friction (one-line change).
- [ADR 0007 — Machine principals with a scope model separate from human roles](docs/ADR/0007-machine-principal-scope-model.md) — `service_accounts` table; opaque `sa_<random>` bearer tokens; five fixed scopes, non-hierarchical, additive, orthogonal to the human role enum. Shipped in PR #52. **Six since WO-R3-312** — `agent_runs:write` was added by [ADR 0035](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md), which amends this one's count; the ADR text is history and is not rewritten.
- [ADR 0008 — Chaos framework is triple-gated and never in production](docs/ADR/0008-chaos-gating.md) — `CHAOS_ENABLED` env flag + `chaos:invoke` scope + per-tool blast-radius check; Terraform validation refuses `CHAOS_ENABLED=true` in the production workspace.

### Naming conventions (normative)

**Tool names** — verb-first `snake_case`, one function per tool. Examples: `get_consumer_lag`, `list_dlq_messages`, `list_audit_events`, `restart_consumer_group`, `replay_dlq_messages`, `pause_dag`, `invalidate_cache_key`, `kill_consumer`, `poison_message`, `saturate_redis`, `inject_latency`, `bad_deploy`, `create_stuck_dag`, `create_mislabeled_dlq_job`, `pause_control_loop`, `pause_dag_chaos`, `saturate_db_pool`, `degrade_downstream`. `snake_case` matches Pydantic field style and serializes cleanly through the MCP `tools/list` response. `pause_dag_chaos` is the only name carrying a surface suffix, and it is not a precedent to copy: the lab hook sets the *same* flag as the `pause_dag` Tier-1 action and differs only in which scope may call it, so the two cannot share a registry name and nothing else about the second one is a different verb ([ADR 0029](docs/ADR/0029-stranded-chain-and-lab-pause-are-manufactured.md)). A new chaos hook with a behaviour of its own gets a verb of its own.

**Tool descriptions** — normative, and treated as code. The agent cannot read this file or the docstrings; the `description` string in the `@tool` decorator *is* the whole interface. A description that does not match the query behind it is therefore a functional defect, not a documentation nit, and it fails in the worst possible way: silently, with a confident-looking answer. Three rules from WO-R2-53, and a fourth from WO-R2-166:

- **Say which clock.** "Most recent first" is ambiguous where a row has more than one timestamp. `list_dlq_messages` claimed it while ordering by job *submission* time, so the newest dead-letters could sit past the end of the only page the agent could fetch. It now orders by dead-letter time and says so, and emits `created_at` and `dead_lettered_at` as separate fields.
- **Never promise completeness you cap.** `get_trace` promised "every artifact carrying a given trace_id" and hard-capped at 50 jobs / 200 audit rows with nothing to signal it had stopped short — an agent reading 50 of 4000 and concluding anything about the trace was misled by the tool. Either return everything, or state the cap in the description AND return a `truncated` flag with the true total. A capped result the caller knows is capped is useful; one it does not is worse than an error.
- **Filter before the limit, not after.** `search_traces` applied `limit` in SQL and dropped NULL-trace rows in Python afterwards, so untraced jobs spent the result budget and the tool answered "no traces" for traces that existed. Any predicate that decides whether a row belongs in the answer belongs in the query.
- **Never advertise a safety property the tool cannot deliver.** `poison_message` said it dropped a `replay_safe` dead-letter row, and it did stamp that column — but the fault it injects is a payload that fails schema validation, which fails identically on every attempt. "Safe to replay" is a *routing claim*, the strongest one this platform's vocabulary has, and it was being made about a row nothing could fix by replaying. Cost: a paid live run graded an agent's correct refusal as a failure (`efdc3b2a9864`), then a repair that moved the error text instead of the hint, which made the row self-consistent and left it lying about what had happened. Two things to take from it. A claim about what a caller may safely *do* is held to a higher bar than a claim about what a field contains, because the caller acts on it and the action is not free. And when a description and the behaviour disagree, ask which half describes what the code actually did before deciding which half to move — the honest half is not always the one that is easier to change.

State pagination explicitly too: whether an `offset` exists, and whether `total` can exceed what was returned. "No offset" is a fine answer — an unstated one is not.

**Scopes** — `<domain>:<verb>`, fixed enum. Adding a scope is a decision; renaming or splitting one is a token migration. The six scopes:

| Scope | Grants |
|---|---|
| `telemetry:read` | Observability read surface — consumer lag, queue depth, in-flight counts, traces, health snapshots. |
| `incidents:read` | Incident-response read surface — DLQ contents, incident summaries, saga state, per-job history. |
| `actions:propose` | Create a proposal for a Tier 1 or Tier 2 action; does not execute. |
| `actions:execute` | Execute an approved proposal (Tier 1 idempotent, Tier 2 requires an approval reference). |
| `chaos:invoke` | Invoke chaos framework tools. Additionally gated by `CHAOS_ENABLED`. |
| `agent_runs:write` | Report the caller's own run to the platform — the two `[commander: telemetry]` tools. **Write-only, and the one scope whose holder is deliberately denied a read:** there is no read tool for `agent_runs`, and the `agent.run_reported` audit stream these calls write is withheld from any principal holding this scope ([ADR 0035](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md)). Grantable through the admin API, unlike `chaos:invoke` — it is not the lab. |

**The eval runs as two principals, and the split is load-bearing** (WO-R3-187, owner decision O-4, 2026-09-15). `scripts/seed_incident_commander.py` (`make seed-incident-commander`) mints one token for each:

| Service account | Scopes the seeder grants by default | `.env` name | Who it is |
|---|---|---|---|
| `incident-commander` | `telemetry:read`, `incidents:read` | `PLATFORM_TOKEN` | the agent under test |
| `incident-commander-chaos` | `telemetry:read`, `incidents:read`, `chaos:invoke` | `PLATFORM_CHAOS_TOKEN` | the evaluator that seeds, verifies and resets the world |

Read that first column exactly as written: it is the **seeder's default**, not what a running stack holds. On a re-run the script *unions* its defaults into whatever the live account already has and never drops a grant silently (`SA_REPLACE_SCOPES=1` is the deliberate-narrowing escape hatch), so an account minted before the split keeps `actions:execute` and the remediation scenarios keep working. A **fresh** bootstrap does not grant it, and every Tier-1 action tool declares `required_scope=Scope.ACTIONS_EXECUTE` — so a brand-new agent account can read but not act until `actions:execute` is added, e.g. `SA_SCOPES=telemetry:read,incidents:read,actions:execute make seed-incident-commander`. The one narrowing the script always performs is removing `chaos:invoke` from the agent account, and it says so on stderr.

The agent account held `chaos:invoke` until this split — it arrived with the self-seeding chaos scenarios so live remediation evals could break the platform and fix it without an operator in the loop, and `actions:execute` arrived with the Wave 3 Tier-1 actions (it was read-only at Step 0). Holding it is now a defect the seeder corrects: the MCP read tools withhold the `chaos.` audit stream from any principal without `chaos:invoke` (`app/services/operator_audit.py::hidden_audit_action_prefixes`), and a single all-scope token makes that predicate inert — which is exactly how the leak survived (`list_audit_events` told an investigating agent which hook had injected its fault, and with what arguments). Verify the live grants with `SELECT name, scopes FROM service_accounts` rather than trusting this table; it drifted once already.

Notably **not** granted, to either principal: `actions:propose`. Tier-2 actions and the approvals subsystem are still unbuilt, so nothing needs it yet — the agent executes Tier-1 directly under an `Idempotency-Key`.

**Audit events** — same `<resource>.<verb>` snake-case shape as existing events. Every machine-principal action carries `principal_type='service_account'` on the audit row:

- `service_account.created` / `service_account.token_minted` / `service_account.token_revoked`
- `agent.tool_invoked` — every MCP tool call; `extra_data` carries `tool_name`, `arguments`, `scope_used`, `latency_ms`, `outcome`.
- `agent.action_proposed` / `agent.action_approved` / `agent.action_executed` / `agent.action_rejected`
- `chaos.tool_invoked` / `chaos.tool_denied` — chaos activity is a separate stream from `agent.tool_invoked` so it filters cleanly on the Audit tab. **Read-visible only to a principal holding `chaos:invoke`**: `list_audit_events` and `get_trace` exclude the prefix, in SQL and out of `total`, for everyone else (WO-R3-187; the rule is `app/services/operator_audit.py::hidden_audit_action_prefixes`, the reasoning is the 2026-09-15 amendment to [ADR 0012](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md)). Since WO-R3-327 that same condition also withholds `lab.` — the two prefixes go in and out together. Human operators read the REST audit API and still see every row.
- `agent.run_reported` — the third machine stream, and the one that points the other way. Written by the two `[commander: telemetry]` tools (`report_agent_run`, `report_agent_briefing`) instead of `agent.tool_invoked`, with the identical `extra_data` shape, because that stream is what the responder did *to* the platform and a status report is not an action. **Withheld from any principal holding `agent_runs:write`** — the writer of a stream is not its reader, and without the exclusion `list_audit_events` would be a read surface for `agent_runs` under another name ([ADR 0035](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md)). Same mechanism as the `chaos.` rule, same file, opposite condition. The row is routed in the `tools/call` envelope rather than written by the tool, which is what makes an unauditable report roll back (R2-51) and what keeps the phase strip rebuildable from the audit log alone: `extra_data.arguments` carries the run id, the state and the caller's time.
- `lab.world_reset` — the fourth machine stream, and the only one no tool call writes. `scripts/reset_eval_state.py` appends exactly one per reset, last, with that reset's whole summary of counters as `extra_data` (WO-R3-327). It exists because `audit_logs` is append-only and the `/demo` console reads the newest `chaos.*` row as *the fault*: after a reset that row was still the previous take's kill, so a freshly wiped world opened the page at `agent remediating` with a clock counting from an incident that no longer existed. The console now reads rows and runs strictly newer than the newest one and draws it as a grey divider. **Withheld from any principal without `chaos:invoke`**, beside `chaos.` and under the identical condition — whoever may fire the lab may read the lab — because the payload names every mechanism the reset sweeps, which is a broader disclosure than one hook name (ADR 0012's 2026-09-20 amendment). It carries its **own** prefix rather than joining `chaos.` deliberately: a boundary filed under that prefix would be read as a fault by the one consumer it was written for. Not a tool, so nothing in `tools/list` moves and there is nothing to rebless; not a chaos hook, so no `BlastRadius` member and no Redis key; not idempotent, because one row per reset is the point. Principal: the evaluator's service account (`SA_CHAOS_NAME`, default `incident-commander-chaos`) resolved in the seed tenant, or a null `principal_id` on a stack where that account has not been seeded. Unlike every other writer here it is not savepoint-wrapped and does raise — there is no response to protect, and a reset whose boundary was never recorded leaves the console reading the previous take as current.

### Runtime shape

- Two deployables from one image: `api` (the existing FastAPI app) and `mcp` (the ASGI entrypoint at `backend/app/mcp/standalone.py`). Same commit, same schemas, same service layer — but **not the same process boot**, and that distinction has bitten once. Anything the API sets up at import time the MCP entrypoint has to set up too; the MCP process ran no observability bootstrap at all until WO-R2-60, so the agent-facing surface emitted unstructured logs with every INFO dropped and exported zero spans while `OTLP_ENDPOINT` was configured for it. Both entrypoints now call `app/core/observability.py::bootstrap_process_observability` (structured logging + tracing + the Redis instrumentor) and `instrument_app` after mounting routes. A third entrypoint gets it by calling one function rather than by remembering four.
- Each process builds its own default SQLAlchemy pool: `pool_size=5` with `max_overflow=10`; the MCP and worker pools are separate.
- Both processes (and the worker loops inside the API process) connect as the **non-owner `incident_app` DB role** — DML only, no DDL, no UPDATE/DELETE on `audit_logs`. Migrations run as the owner (RDS master) via `ALEMBIC_DATABASE_URL`; each lifespan runs `assert_rls_posture` after the migration check and refuses to serve in production if the connection would silently bypass RLS ([ADR 0015](docs/ADR/0015-force-rls-and-nonowner-app-role.md)).
- The agent points at `PLATFORM_MCP_URL` for tools and `PLATFORM_REST_URL` for anything else (there shouldn't be much — everything the agent needs should surface as an MCP tool over time).
- Contract stability between agent and platform is verified by contract snapshot testing against the pinned image, per agent-repo ADR 0007.

---

## Stack

### Backend
- **Python 3.12+ / FastAPI** — async API gateway
- **PostgreSQL** — system of record. Tables: `users`, `tenants`, `jobs`, `audit_logs`, `outbox_events`, `job_events`, `job_dependencies`, `sagas`, `job_triages`, `incident_summaries`, `agent_runs`. Full reference in [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).
- **Redis** — cache, locks, rate limits, pub/sub progress events, CQRS read-model sets, cached backpressure lag value.
- **Kafka** (Redpanda locally; production Kafka not yet provisioned — see [ADR 0018](docs/ADR/0018-production-kafka-posture.md)) — durable event log; decouples job submission from execution, powers event sourcing and fan-out. Topics: `job.submitted` / `job.progress` / `job.completed` / `job.failed` / `job.dlq`.
- **JSON Schema** — every Kafka topic has a schema in `backend/app/schemas/kafka/`; producer and consumer validate on every message.
- **Object storage** — S3 in production, MinIO locally — for uploaded files and artifacts.
- **Worker layer** — asyncio tasks for I/O-heavy work, a threading adapter for blocking SDKs, a multiprocessing pool for CPU-heavy transforms.
- **Anthropic SDK** (Phase 10, complete) — Claude API for DLQ triage, LLM-guided retry policy, natural-language admin queries, and periodic incident summaries. All features off-by-default; fail open on any API issue. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).

### Frontend
- **React + Vite + TypeScript + Tailwind**
- Ten pages: `LoginPage`, `RegisterPage`, `DashboardPage` (job list + create form), `JobDetailPage` (live SSE progress + Kafka event timeline), `AdminPage` (overview / jobs / DLQ / runbooks / users / tenants / digests / audit tabs), `AdminTenantDetailPage` (per-tenant drill-down), `SagasPage` / `SagaNewPage` / `SagaDetailPage` (multi-step workflow management), `DemoPage` (`/demo`, support+ — the live-demo screen; see [`docs/DEMO.md`](docs/DEMO.md)).
- Three live-data patterns, and they are not interchangeable: `useJobStream` (SSE, one job), `useAsyncData` (one load, with the loading/error/empty states a list page must not confuse), and `usePolling` (`useAsyncData` on a timer — the /demo panels at 2s and the Audit tab at 5s). A polled panel keys its skeleton off `loading && data === null`, because `loading` goes true on every tick.
- The console also ships as its own image, `ghcr.io/<owner>/incident-platform-console`, published by `release.yml` on the same version as the backend. Its nginx config is a template whose `/api/` upstream is `${API_UPSTREAM}` (default `http://api:8000`), so one image serves this repo's compose (service `app`) and the commander's demo compose (service `api`).
- Shared components: `Layout`, `StatusBadge`, `ProgressBar`, `TraceId`, `Toast`, `JobForm`, `ProtectedRoute`, `ErrorBoundary`, `Skeleton`.

### Infrastructure
- **Docker / Docker Compose** for local dev (Postgres + Redis + Redpanda + MinIO + backend + frontend).
- **Terraform** for the AWS stack in `infra/` — VPC, ECS Fargate, RDS, ElastiCache, ALB, ECR, IAM, Secrets Manager, S3, CloudWatch alarms with runbook URLs in their descriptions. No Kafka broker: see [ADR 0018](docs/ADR/0018-production-kafka-posture.md).
- **CI/CD** — `.github/workflows/ci.yml`: frontend tsc + tests, ruff + mypy on backend, pytest with coverage gate, a Docker-gated `integration` job running the Testcontainers tier (real Postgres + Redpanda), Terraform static checks, actionlint, and a Docker build → ECR → ECS deploy job that is opt-in behind the `ENABLE_ECS_DEPLOY` repository variable (unset — the job skips; see [ADR 0018](docs/ADR/0018-production-kafka-posture.md)).
- Cloud target: **AWS ECS/Fargate**; **RDS Postgres**; **ElastiCache Redis**; **S3** for artifacts. **Kafka has no production broker** — nothing in `infra/` provisions one ([ADR 0018](docs/ADR/0018-production-kafka-posture.md)).

### Testing
- `pytest` with fixtures, parametrization, factories.
- Layers: **unit** (`backend/tests/unit/`), **API contract** (`backend/tests/api/`), **integration** (`backend/tests/integration/` — Testcontainers, Docker-gated, opt-in locally via the three `RUN_*` variables). Load tests in `backend/tests/load/` (Locust).
- `mypy --strict` in CI on the `app` package.
- `ruff check backend/` in CI.
- Coverage gate at 70% (see `pyproject.toml`) — enforced on the unit + API job only; the integration job runs `--no-cov`.
- No headline test count is written down here, for the reason `README.md` gives: it has been refreshed three times and was stale within a release every time. `make test` reports the count for your checkout; `.github/workflows/ci.yml` is the authority on what runs.
- The integration tier runs in its own CI job (`integration`), which exports every `RUN_*` gate and then **fails if any test skipped** — a fully-skipped run exits 0 in pytest, which is how this tier stayed invisible until it was wired up.

---

## Architecture

The actual runtime topology after Phase 7:

```
                    Frontend (React)
                          │
                          ▼
           ┌──────── FastAPI Gateway ────────┐
           │  POST /jobs                     │
           │   ├─ rate limit (Redis)         │
           │   ├─ check_backpressure (Redis) │
           │   └─ tx: jobs row + outbox row  │
           │  GET  /jobs/{id}/stream  (SSE)  │
           │  GET  /admin/stats, /slos, …    │
           └────────────────┬────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ▼                   ▼                   ▼
   PostgreSQL          Redis (cache,        Kafka (Redpanda)
   ─────────────       pub/sub, sets,       ────────────────
   users               rate limits)         Topics:
   jobs                                       job.submitted
   audit_logs          Keys:                  job.progress
   outbox_events         jobs:status:*        job.completed
   job_events            jobs:user:*:…        job.failed
   job_dependencies      job:progress:{id}    job.dlq
   sagas                 kafka:consumer_lag…
   job_triages           jobs:create:* (RL)
                            ▲
                            │ (CQRS read model)
                            │
           ┌────────────────┼────────────────────────────────┐
           │   Worker process (single ECS task)              │
           │                                                 │
           │   Eight Kafka consumer groups (concurrent):     │
           │     1. worker-dispatcher   → _run_job           │
           │     2. audit-writer        → audit_logs rows    │
           │     3. sse-broadcaster     → Redis pub/sub      │
           │     4. event-log           → job_events rows    │
           │     5. read-model          → Redis sets         │
           │     6. dependency-resolver → promote children   │
           │     7. saga-coordinator    → compensation       │
           │     8. llm-triage          → job_triages rows   │
           │                                                 │
           │   Eleven supporting loops:                      │
           │     • outbox relay (DB → Kafka)                 │
           │     • delayed-retry promote (Redis → outbox)    │
           │     • dlq replay promote (scheduled replays)    │
           │     • resume-waiting sweep (pause lifted)       │
           │     • stale-PENDING backstop (lost timers)      │
           │     • stale-RUNNING sweep (crash orphans → DLQ) │
           │     • lease renewal (heartbeat on RUNNING jobs) │
           │     • SLO evaluation (fast-burn → Alert)        │
           │     • metrics loop (gauges + lag cache)         │
           │     • digest loop (per-tenant LLM summary)      │
           │     • idempotency reaper (expired records)      │
           │                                                 │
           │   Three concurrency models:                     │
           │     • asyncio  → bulk_api_sync                  │
           │     • thread   → csv_upload                     │
           │     • process  → doc_analysis, report_gen       │
           └─────────────────────────────────────────────────┘

   Object storage (S3 / MinIO)          OpenTelemetry → AWS X-Ray
   Structured JSON logs → CloudWatch    CloudWatch alarms → SNS → email
```

---

## Core Features

### 1. Auth + Session Model
- Login with access + refresh tokens (JWT).
- Roles: `user`, `support`, `admin`. Support and admin share the privileged-read path; admin alone can list users.
- Audit trail for important actions (job creation, replay, incident resolved, saga created); the audit consumer adds a second `event.*` row from Kafka so the audit log is also event-sourced.
- Dependency-based auth guards via FastAPI `Depends`.
- Clean error handling via a custom `AppError` hierarchy with predictable JSON shape (`error_code`, `message`, `details`, `request_id`).

### 2. Job Submission Pipeline
- `POST /jobs` validates input → checks backpressure → writes job row + outbox row in one DB transaction → outbox relay publishes `job.submitted` to Kafka → dispatcher consumer runs the processor → state transitions emit `job.progress` / `job.completed` / `job.failed` (also via outbox).
- Async API endpoints throughout.
- Strategy-pattern processor map (`async_tasks.py`, `thread_adapters.py`, `cpu_processors.py`).
- Retry with exponential backoff. Failed jobs stay in the system; exhausted jobs go to `DEAD_LETTER` and `job.dlq`.
- Idempotency keys on job creation (DB unique constraint).
- Dependencies: jobs can declare parent jobs; the DependencyResolver consumer promotes them.

### 3. Live Progress Streaming
- Server-Sent Events at `GET /jobs/{id}/stream`.
- Worker publishes only to Kafka; SSE consumer bridges Kafka → Redis pub/sub → SSE clients. UI shows real-time progress bar + scrolling event log.
- Non-DLQ `job.failed` events render as `retrying` (with backoff countdown text); DLQ as `dead_letter`. This distinction comes from the SSE consumer mapping, not the worker.

### 4. Admin Incident Console
- Eight tabs, in render order: **Overview** (CQRS stats cards + SLO scorecards), **Jobs** (filter by status / trace ID / type), **DLQ** (with per-type breakdown pills and per-row Replay/Resolve), **Runbooks** (clickable list with a modal showing the diagnosis steps), **Users**, **Tenants** (platform admins only), **Digests** (LLM incident summaries), **Audit** (clickable rows opening a metadata modal). The `Tab` union in `AdminPage.tsx` is the authority.
- Replay (`/admin/jobs/{id}/replay`) resets `retry_count` to 0 and records the previous values in the audit log's `extra_data`.
- Event-sourced **job timeline** on `JobDetailPage` (admin only) hits `/admin/jobs/{id}/timeline` and renders every Kafka event for that job in offset order with topic/partition/offset metadata.

### 5. Sagas — Multi-Step Workflows
- `POST /sagas` creates a saga + an ordered chain of jobs, each depending on the previous. It runs the same admission control as `POST /jobs` — per-IP rate limit (same bucket), backpressure, per-tenant quota — through the shared `check_job_admission` guard, counting the saga as its `len(steps)` jobs. Anything that creates `jobs` rows must go through that guard or the per-tenant caps stop being enforceable; `steps` is capped at `MAX_SAGA_STEPS`.
- `SagaDetailPage` polls every 2s and shows the step chain as a vertical timeline with status dots that go green as each step completes.
- On dead-letter of any step: saga goes `COMPENSATING`, downstream waiting jobs are cancelled, and `{type}.compensate` jobs are created (real `jobs` rows) for already-completed prior steps in reverse order. Once every compensation step is terminal the saga settles: all `completed` → `COMPENSATED`, any `dead_letter`/`cancelled` → `FAILED` (ADR 0017).

### 6. Concurrency — Use All Three Models Deliberately

| Model | Use For |
|---|---|
| `asyncio` | API calls to third-party services, high-concurrency I/O, live updates, streaming status |
| `threading` | Blocking SDKs, file upload helpers, log shipping, wrapping legacy blocking functions |
| `multiprocessing` | CPU-heavy CSV parsing, document transformation, PDF/text extraction, data aggregation |

The processor map in `app/workers/dispatcher.py` routes each job type to the right concurrency model.

---

## Code Style & Conventions

### General
- Full type hints across all app code — FastAPI derives real value from typing.
- `mypy --strict` must pass.
- Service / repository layer separation.
- Explicit request/response Pydantic models (DTOs in `app/schemas/`).
- Resource-oriented API routes.
- Correlation/trace IDs on every request via `RequestContextMiddleware`.

### Python Patterns to Use Naturally (not artificially)

- **Decorators** — auth checks, timing/profiling, retry wrappers, audit logging, feature flags, caching.
- **Context managers** — DB sessions/transactions, timing blocks, temporary files, distributed lock acquire/release, structured logging scopes.
- **Dataclasses / Pydantic models** — typed domain models (`TriageAnalysis`, `SagaStep`, `SLOState`), value objects, job command objects.
- **Repository / service pattern** — `JobRepository`, `JobService`, `SagaService`, `OutboxRepository`, etc.
- **Custom exception hierarchy** — `AppError` → `NotFoundError`, `AuthorizationError`, `JobError`, `RateLimitError`, `BackpressureError`, etc.
- **Strategy pattern** — pluggable job processors via `_PROCESSORS` dict in `dispatcher.py`.
- **Mixins** (limited, one subsystem) — `TimestampMixin` on models; explicitly reason about MRO if more added.
- **Descriptors** (one meaningful use) — open opportunity; not yet used.
- **`**kwargs`** — in configurable base service classes, adapters, logging helpers.

---

## Structured Logging

Every log entry should carry:
- `request_id` / `trace_id`
- `job_id`
- `user_id`
- `route`
- `latency`
- `retry_count`

Implementation: `app/core/logging.py` uses `python-json-logger`. `app/core/middleware.py` sets `request_id_var` and `trace_id_var` (contextvars) on each request; the formatter pulls them in.

OTel auto-instrumentation is enabled for FastAPI, SQLAlchemy, and Redis. Spans propagate from API → worker via the OTel `traceparent` carrier serialized into the job payload at create time and re-extracted in `_run_job`.

Logs must be queryable by trace ID end-to-end: browser → API → worker → result.

---

## API Design Principles

- Resource-oriented routes.
- Explicit request/response schemas (Pydantic).
- Predictable error shapes (`AppError` produces a consistent envelope).
- Correlation IDs on all responses (`X-Request-ID`, `X-Trace-ID` headers).
- Idempotent job creation via `idempotency_key`.
- Pagination, filtering, sorting on list endpoints (`PaginationParams` base).
- Backwards-compatible versioning (everything under `/api/v1/`).
- OpenAPI docs auto-generated by FastAPI at `/api/v1/docs`.

---

## Testing Strategy

### Layers
1. **Unit tests** (`backend/tests/unit/`) — services, processors, validators, repositories, consumers. No I/O.
2. **API contract tests** (`backend/tests/api/`) — full FastAPI app with dependency overrides; SQLite in-memory DB; mocked Redis.
3. **Integration tests** (`backend/tests/integration/`) — Testcontainers with real Postgres 16 (RLS enforcement, eval-reset SQL, migration advisory lock, outbox relay exclusivity, outbox dead-letter, the MCP envelope, the migration role, the paused-relay contrast, the stalled resume sweep, the held connection pool, the degraded downstream dependency), Redpanda (Kafka round-trip) and — since WO-R3-225 — Redis (the sticky kill's `PXAT` window and the `chaos:*` sweep, which are Redis semantics rather than ours). Fourteen test files. Docker-gated; run locally with `make test-integration`, and in CI by the `integration` job.
4. **Load tests** (`backend/tests/load/`) — Locust scenarios for the job submission path.
5. **Failure-mode tests** — circuit-breaker open/close, schema validation rejecting bad payloads, redelivery dedup via unique constraints.

### Tooling
- `pytest` fixtures and parametrization.
- Factories for test data (inline `_make_*` helpers; could grow into proper factories later).
- Testcontainers for the whole integration tier — Postgres 16 in twelve files, Redpanda in one, Redis in one (Docker availability is skip-gated).
- Coverage gate at 70% in `pyproject.toml`.
- Unit + API tests run on every PR via the `test` job in `ci.yml`; the integration tier runs on every PR via the `integration` job, which sets `RUN_RLS_TEST` / `RUN_EVAL_RESET_TEST` / `RUN_MIGRATION_LOCK_TEST` and then asserts that the files it names contributed tests and none skipped. That census had fallen behind the directory — it listed only the five original files for several releases — and is complete again as of WO-R3-259 (`#208`); `test_outbox_stall.py` joined it with WO-R3-200 and `test_resolver_stall.py` with WO-R3-213. `test_env_example.py::test_ci_census_covers_every_integration_module` now fails when `EXPECTED` and the directory disagree, so the omission is caught in the unit tier rather than by a quietly shrinking count. **A new file in `backend/tests/integration/` has to be added to `EXPECTED` in the same PR**, or it can silently stop contributing without failing the job.

---

## Data Structures & Algorithms (Natural Usage)

| DS/A | Where It Appears |
|---|---|
| Hash maps/sets | Deduplication, membership checks, idempotency keys, **CQRS read-model sets in Redis** |
| Queues | Job processing pipeline (Kafka topics), outbox |
| Priority queues / heaps | Redis sorted set for the priority queue (legacy path, still used by delayed-retry) |
| Fixed window (counter + TTL) | Rate limiting (Redis INCR + EX) — per client IP, per MCP principal, per admin on the paid endpoints, per tenant. `2 * limit` is reachable across a window boundary; ceilings are sized for it |
| Sorting | Result ordering, pagination, ordering events by `(recorded_at, kafka_offset)` |
| Caching (LRU, TTL) | Redis cache layer (`JobCache`) |
| Binary search | Time-series pagination helpers (`Job.created_at` indexed) |
| **Graph thinking** | **Job dependency DAG** (Phase 7) — children, transitively unmet count, cycle-free by construction |
| **Topological order** | Saga step chain (each depends on the previous) |

---

## Performance Tradeoffs to Explore

- Sync vs async endpoints.
- Eager vs lazy loading from DB (`lazy="noload"` on relationships; we explicitly fetch when needed).
- Query count vs memory usage (N+1 awareness).
- Batching vs latency.
- Caching vs consistency (Redis TTL strategies, CQRS eventual consistency).
- Process pool overhead vs CPU speedup.
- JSON serialization size.
- WebSocket vs polling (we use SSE).
- Precomputed aggregates vs live queries (the CQRS overview tab uses precomputed sets).

---

## Advanced Python Patterns (Senior / Principal Level)

These go beyond Phase 1–5 and should be introduced naturally in later phases.

- **Structured concurrency** — `asyncio.TaskGroup` (Python 3.11+) for fan-out/fan-in; cancel all sibling tasks on first failure. The `worker_loop` currently uses `asyncio.gather` + per-task `cancel()`; converting to `TaskGroup` is a clean refactor.
- **Protocols + structural subtyping** — replace ABCs with `typing.Protocol` where duck typing is the right model (e.g. storage backends, queue backends).
- **ParamSpec + Concatenate** — type-safe decorator factories preserving the wrapped function's full signature (retry wrappers, audit decorators).
- **`__init_subclass__`** — self-registering plugin pattern for job processors; adding a new processor class auto-registers it without touching the dispatcher.
- **`tracemalloc` + memory profiling** — instrument long-lived workers to detect leaks; track top allocations per snapshot delta.
- **Slot classes** — `__slots__` on hot-path domain objects (`ProgressEvent`) to reduce per-instance memory at scale.
- **Custom pickling** — `__reduce__` / `__getstate__` / `__setstate__` on objects passed to the multiprocessing pool.
- **`contextlib.AsyncExitStack`** — dynamic composition of async context managers in the worker lifecycle.
- **Generic repositories** — `Repository[ModelT, PKT]` with bounded type vars; we already have `BaseRepository[ModelT]` — could tighten the bound.
- **Descriptor protocol** — validated config fields using `__set_name__` / `__get__` / `__set__`; open opportunity.

---

## System Design Patterns Demonstrated

Concrete implementation pointers for each pattern this project demonstrates end-to-end.

| Concept | Where It Lives | Notes |
|---|---|---|
| **At-least-once delivery** | `BaseKafkaConsumer._process_one` commits `{TopicPartition: offset + 1}` per message only after `handle_message` returns; on failure `_process_batch` seeks back so the next poll redelivers | Duplicate deliveries are safe: the dispatcher claims PENDING→RUNNING via atomic conditional UPDATE (`JobRepository.claim_for_running`) so exactly one executes; idempotency keys dedupe job *creation* only |
| **Exactly-once dedup via unique constraint** | `job_events.uq_job_events_kafka_coord` on `(topic, partition, offset)`; sibling `audit_logs.uq_audit_logs_kafka_coord` (nullable coords — inline audit writes exempt) | Kafka redelivery → `IntegrityError` → consumer swallows + commits |
| **Backpressure** | `app/utils/backpressure.py` checks Redis-cached `ConsumerLag` from the dispatcher; `POST /jobs` raises `BackpressureError` (503) | Threshold in settings; metrics loop populates the cache |
| **Circuit breaker** | `app/utils/circuit_breaker.py` wraps external API calls | Open / half-open / closed states; metrics emitted |
| **Read/write split (CQRS)** | `app/workers/read_model.py` (write path) + `GET /admin/stats` / `/admin/users/{id}/stats` (read path) | Sets keyed by `job_id` are idempotent under at-least-once |
| **Outbox pattern** | `outbox_events` table; written in same tx as `jobs` mutation; `_outbox_relay_loop` publishes to Kafka | Survives crash between DB commit and broker publish |
| **Event sourcing** | `job_events` table; `EventLogConsumer` appends every lifecycle event; `GET /admin/jobs/{id}/timeline` replays | Immutable; `recorded_at` order preserved by per-partition serial processing |
| **Saga pattern** | `app/services/saga.py` (creation) + `app/workers/saga_coordinator.py` (lifecycle + compensation) | `{type}.compensate` jobs enqueued in reverse order on DLQ |
| **Job dependency DAG** | `job_dependencies` table; `DependencyResolver` consumer; `JobStatus.WAITING` | Cycle-free by construction (deps reference only existing jobs) |
| **Schema evolution** | `backend/app/schemas/kafka/*.schema.json` validated on both producer and consumer | `additionalProperties: true` for backward compatibility |
| **Dead-letter queue** | `job.dlq` topic; `dead_letter` job status; `GET /admin/dlq/stats` for the counts and `GET /admin/jobs?status=dead_letter` for the rows; `LlmTriageConsumer` (Phase 10) analyses each entry | Admin replay resets `retry_count`; unregistered `.compensate` types also route here |
| **Fan-out / fan-in** | Seven Kafka consumer groups subscribed to the lifecycle topics, all processing independently | No coordination needed; each group has its own offset |
| **Consumer group isolation** | Each consumer in `worker_loop` is its own group; failure of one doesn't affect others | `_supervise_consumer` owns start(): a consumer that fails to start at boot is retried with backoff, not dropped |
| **Distributed locking** | Redis `SETNX` for job deduplication (open opportunity in the rate-limit code path) | Idempotency key is the primary dedup mechanism today |
| **Connection pool sizing** | SQLAlchemy `pool_pre_ping=True`; pool tuning is a Phase 8 item (PgBouncer) | — |
| **Time-series partitioning** | Phase 8 item: partition `audit_logs` by month | — |
| **SLOs + error budgets** | `app/services/slo.py` computes from `jobs` table; `GET /admin/slos` returns budget remaining + burn rate; `_slo_evaluation_loop` evaluates on a schedule and raises a de-duplicated Alert on a 14.4× burn | 14.4× fast-burn alarms in `infra/cloudwatch.tf`, matching the in-app threshold |
| **Structured runbooks** | `runbooks/*.yaml` at repo root; `GET /admin/runbooks{,/{id}}`; CloudWatch alarm descriptions reference runbook URLs | Admin UI surfaces them next to the SLO scorecards |

---

## Memory & Resource Awareness

- Streaming / chunked processing for large uploads — don't hold entire files in memory.
- Ensure large uploaded objects are not accidentally retained by closures or callbacks.
- Avoid reference cycles in callback-heavy or closure-heavy worker code.
- Monitor memory growth in long-lived workers (Phase 8: `tracemalloc` snapshots in performance tests).

---

## LLM Integration (Phase 10 — Complete)

The project uses the **Anthropic Python SDK** to add four LLM-powered features. All shipped, all off by default, all fail open (the platform runs fine without an `ANTHROPIC_API_KEY`).

### The four features

1. **DLQ triage** (PR #34) — `LlmTriageConsumer` subscribes to `job.dlq`; Claude classifies the failure. Persisted to `job_triages`; surfaced on the admin DLQ tab and `JobDetailPage`. It also maps the analysis onto `jobs.remediation_hint` — the coarse category the agent's DLQ tools filter on — in the same transaction, and only into a NULL column so it cannot overwrite a `mark_dlq_permanent` fence (R2-24). Because triage is off by default, that column stays NULL for organically dead-lettered jobs on a stock deployment; the tool descriptions say so rather than implying coverage that isn't there.
2. **LLM-guided retry policy** (PR #39) — after the first deterministic retry, the worker asks Claude retry-with-backoff vs dead-letter-now. Falls back to deterministic on any error; audit log records `dead_lettered_by: llm_retry_policy` + reasoning.
3. **Natural-language admin queries** (PR #40) — `POST /admin/query` translates plain English into a Pydantic `JobFilterSpec` (enum/literal fields only → injection-safe by construction).
4. **Periodic incident summaries** (PR #41) — the `_digest_loop` runs every `LLM_DIGEST_INTERVAL_HOURS` and writes a one-paragraph per-tenant digest to `incident_summaries`.

### Shared conventions

- **Default model**: `claude-opus-4-7` with `thinking: {type: "adaptive"}`. Failure diagnosis and retry classification are intelligence-sensitive; the per-call cost is small.
- **Structured outputs** — every LLM call uses `client.messages.parse()` with a Pydantic `output_format`. The response is shape-checked before it ever reaches the DB.
- **Prompt caching** — every service caches its frozen system prompt via `cache_control: {"type": "ephemeral"}`. Volatile per-call context goes into the user message, after the cache breakpoint.
- **Feature flags** — `LLM_TRIAGE_ENABLED`, `LLM_RETRY_POLICY_ENABLED`, `LLM_NL_QUERY_ENABLED`, `LLM_DIGEST_ENABLED`. All default `False`. Tests pass without network access.
- **Fail open** — see [ADR 0005](docs/ADR/0005-llm-features-fail-open.md). Any API error / timeout / schema mismatch falls back to a deterministic non-LLM path (or 503 for NL queries, or a skipped digest).
- **Cost telemetry** — `usage.cache_read_input_tokens` / `cache_creation_input_tokens` / `input_tokens` / `output_tokens` are persisted on each row for cache-hit visibility.

### SDK surface (verified against `anthropic 0.112.0`)

All four services use `client.messages.parse(..., output_format=SomePydanticModel, thinking={"type":"adaptive"})` and read `response.parsed_output` on model `claude-opus-4-7`. Verified against the installed SDK: `messages.parse` accepts all these kwargs; `ThinkingConfigAdaptiveParam` accepts `{"type":"adaptive"}`; `ParsedMessage.parsed_output` exists; `claude-opus-4-7` is in the model literal. Cache-hit fields (`cache_read_input_tokens`, `cache_creation_input_tokens`) are `Optional[int]` on `Usage` — the shared `_llm_usage.extract_usage` helper coerces `None` to `0` so downstream aggregation works.

---

## Milestone Plan

### Phase 1: Clean Backend Core ✅
- FastAPI app structure, Postgres models, auth, job creation, status endpoints.
- Service / repository layers, type hints everywhere, pytest setup.
- **Focus:** style, architecture, tests, API design.

### Phase 2: Background Execution ✅
- Queue, retries, progress tracking.
- Async I/O tasks, one thread-based adapter, one process-based CPU step.
- **Focus:** concurrency choices, idempotency, failure handling.

### Phase 3: Frontend + Debugging Realism ✅
- Dashboard, job details, live updates, admin incident console.
- Request correlation IDs visible in the UI.
- **Focus:** Network-tab debugging, auth bugs, frontend/backend contracts.

### Phase 4: Production Deployment ✅
- Docker, cloud deployment, managed Postgres / Redis / storage.
- Secrets / config management, structured logging, metrics, alerting, CI/CD.
- **Focus:** shipping, runtime debugging, environment parity.

### Phase 5: Hardening ✅
- Rate limiting, load testing, caching, test matrix.
- Static checks, performance profiling, chaos / failure scenarios.
- **Focus:** senior-level polish.

### Phase 6: Observability & Reliability ✅
- **OpenTelemetry** distributed tracing — spans across API → worker → DB → Redis, exported to OTLP (AWS X-Ray or Jaeger).
- **Custom metrics** — `JobCompleted`, `JobFailed`, `JobDeadLettered`, `QueueDepth`, `InFlightJobs`, `ConsumerLag`, `RequestLatency` on the `IncidentPlatform` CloudWatch namespace. `emit_count` / `emit_gauge` do no I/O — they sanitise dimensions and queue the datum; one background task per process flushes an aggregated `StatisticSet` every 60s. Dimension values are bounded by a declared allow-list plus a hard cap; anything else becomes `other`. See [Cost model (CloudWatch custom metrics)](docs/ARCHITECTURE.md#cost-model-cloudwatch-custom-metrics) before adding a metric or a dimension.
- **SLOs + error budgets** ✅ — `job_completion_rate` ≥ 99% and `job_dispatch_latency` ≥ 95% within 30 s, both over rolling 24h. `GET /admin/slos` returns current state, budget remaining %, and burn rate; the worker also evaluates them on a schedule and alerts on a fast burn. Cancelled jobs are excluded from both objectives — a saga rollback or a dependency cascade is a decision not to dispatch, not a failure to.
- **CloudWatch alarms** ✅ — eight baseline alarms (`alb-5xx`, `backend-tasks-low`, `mcp-tasks-low`, `rds-cpu-high`, `redis-memory-low`, `queue-depth-high`, `outbox-relay-stalled`, `outbox-dead-lettered`) plus two SLO fast-burn alarms (14.4× over 1h windows) — ten in `infra/cloudwatch.tf`. All notify via SNS topic `${app_name}-alarms`.
- **Circuit breaker** ✅ — `app/utils/circuit_breaker.py` wraps external API calls; opens on N consecutive failures, half-open probe, auto-recover.
- **Structured runbooks** ✅ — `runbooks/*.yaml` at repo root; 8 files covering the ten alarms and both SLO breaches (`mcp-tasks-low` shares `rb-ecs-tasks-low`, `outbox-dead-lettered` shares `rb-outbox-relay-stalled`). Each has summary, symptoms, diagnosis steps (with copy-pasteable shell commands), mitigation, escalation, related dashboards. Alarm descriptions reference `/admin/runbooks/{id}` so on-call has a one-click path from PagerDuty.
- **Focus:** production observability, on-call readiness, failure isolation.

### Phase 7: Kafka + Advanced Architecture Patterns ✅
- **Kafka integration (end-to-end)** ✅
  - **Producer**: `app/workers/kafka_producer.py` publishes lifecycle events; `publish_raw` propagates schema-validation errors so the outbox can mark rows failed.
  - **Consumer groups**: eight, all running concurrently in the worker process (seven were shipped in Phase 7; `llm-triage` joined in Phase 10). See "Current Implementation Status" above for the full list.
  - **Partitioning strategy**: every event keyed by `user_id` so per-user ordering is preserved within each consumer group.
  - **Offset management**: explicit per-message per-partition commits (`{TopicPartition: offset + 1}`) only after `handle_message` returns successfully; on handler failure the consumer seeks back to the failed offset and the next poll redelivers (poison pills are committed past per-partition) — at-least-once. Combined with idempotency keys (jobs) and a unique constraint (event log) to avoid double effects.
  - **Dead-letter topic**: `job.dlq`. Admin UI inspects (with per-type breakdown) and replays. Replay resets `retry_count` (a bug we fixed in `#27`).
  - **Schema Registry** ✅ — file-based JSON Schema in `backend/app/schemas/kafka/`; format checker on (enforces `uuid` etc.); producer + consumer validate on every message.
  - **Local dev**: Redpanda in `docker-compose.yml`. **Production Kafka is not yet provisioned** — nothing in `infra/` creates a broker, `KAFKA_BOOTSTRAP_SERVERS` is omitted from the task definition unless `var.kafka_bootstrap_servers` is set, and the ECS deploy job is gated off. See [ADR 0018](docs/ADR/0018-production-kafka-posture.md).
  - **Testing** ✅ — Testcontainers-based integration test in `backend/tests/integration/test_kafka_e2e.py` spins up Redpanda on a pre-allocated host port and verifies producer ↔ consumer round-trip with schema validation on both ends. Skipped if Docker isn't available; runs on every PR in the `integration` CI job, which fails if it skips.
- **Outbox pattern** ✅ — `outbox_events` table; written in same transaction as job state changes; `_outbox_relay_loop` polls every second and publishes. Partial index on `published_at IS NULL` for hot-path scan.
- **CQRS** ✅ — `ReadModelProjector` maintains Redis sets per status (global + per-user); `GET /admin/stats` reads only those, no SQL aggregate on `jobs`. Sets keyed by `job_id` are idempotent under at-least-once delivery.
- **Event sourcing** ✅ — `job_events` table appended by `EventLogConsumer`; `GET /admin/jobs/{id}/timeline` replays. Frontend renders a vertical timeline with topic/partition/offset per row.
- **Saga pattern** ✅ — `POST /sagas` creates a saga + chain of dependent jobs (sharing `saga_id`). `SagaCoordinator` marks the saga complete when all steps finish; on dead-letter it cancels downstream and creates `{type}.compensate` job rows for completed prior steps in reverse order, then settles the saga `COMPENSATED`/`FAILED` once those are terminal (ADR 0017). Compensation processors are application responsibility — an unregistered `*.compensate` dead-letters and the saga settles `FAILED`, which is the intended forcing function.
- **Job dependency DAG** ✅ — `job_dependencies` (many-to-many self-join); `JobStatus.WAITING` for jobs with unmet deps; `DependencyResolver` consumer promotes children when all parents complete. Cycles impossible by construction.
- **Backpressure** ✅ — dispatcher consumer exposes `consumer_lag()`; metrics loop emits `ConsumerLag` and caches the value in Redis with TTL; `check_backpressure` reads the cache and raises `BackpressureError` (503) when lag exceeds threshold. API never queries Kafka directly.
- **Focus:** Kafka end-to-end (produce → consume → DLQ → replay), distributed systems correctness, event-driven architecture.

### Phase 8: Platform Engineering & Scale 🟡
- **HTTPS + ACM** — add TLS to the ALB with an ACM cert; redirect HTTP → HTTPS; enforce HSTS.
- **Terraform remote state** — S3 bucket + DynamoDB lock table for shared state; enable team collaboration on infra.
- **Staging environment** — second Terraform workspace (`staging`) with smaller instance sizes; CI deploys to staging on PR merge, production on manual approval.
- **Blue/green deployments** — ECS CodeDeploy integration; shift traffic from blue to green with automatic rollback on health check failure.
- **ECS auto-scaling** — scale backend tasks on queue depth (custom metric) and CPU; scale-in protection during active job processing.
- **PgBouncer** — connection pooling sidecar in ECS task; tune pool size vs DB `max_connections`; measure connection wait time.
- **Read replicas** — RDS read replica for analytics / admin queries; route read-heavy endpoints to replica via separate DB session.
- **Database partitioning** — partition `audit_logs` and `job_events` by month (range partitioning); measure query speedup on time-bounded queries.
- **Feature flags** — lightweight Redis-backed feature flag system; enable new job types per-user or per-role without deploys.
- **Focus:** zero-downtime deployments, horizontal scale, cost optimization.

### Phase 9: Security Hardening 🟡
- **WAF** — AWS WAF in front of ALB; rate limiting at the network layer, SQL injection / XSS rules, geo-blocking.
- **Secret rotation** — automatic rotation of DB password and JWT secret in Secrets Manager; app picks up new secrets without restart.
- **VPC flow logs + CloudTrail** — log all network traffic and API calls; ship to S3 + Athena for forensic queries.
- **Dependency scanning** — `pip-audit` and `npm audit` in CI; fail on high-severity CVEs; auto-PR for patch updates via Dependabot.
- **OWASP hardening** — security headers (CSP, X-Frame-Options, HSTS) in Nginx; validate all user input at system boundaries; SQL injection impossible via parameterized queries (verify with sqlmap).
- **mTLS between services** — mutual TLS for backend → RDS and backend → Redis using ACM Private CA; eliminates credential-based auth for internal traffic.
- **Least-privilege IAM** — audit and tighten ECS task role to exact S3 paths and exact Secrets Manager ARNs; no wildcard permissions.
- **Focus:** defence in depth, compliance readiness, zero-trust networking.

### Phase 10: AI / LLM Integration ✅
- **LLM-driven DLQ triage** (PR #34) — Anthropic SDK; per dead-lettered job, Claude classifies the root cause, summarises the failure, suggests a fix, and rates retryability + confidence. Persisted to `job_triages`. Surfaced on the DLQ tab and `JobDetailPage`.
- **LLM-guided retry policy** (PR #39) — after the first deterministic retry, Claude sees the error + context and decides "retry with backoff" (with recommended seconds) vs "dead-letter now". Falls back to deterministic on any error. Audit log records `dead_lettered_by: llm_retry_policy` + reasoning.
- **Natural-language admin queries** (PR #40) — `POST /admin/query` translates plain English into a constrained Pydantic `JobFilterSpec`; the model can only fill enum/literal fields so the query is injection-safe by construction. Off by default → 503 on failure.
- **Periodic incident summaries** (PR #41) — the `_digest_loop` runs every `LLM_DIGEST_INTERVAL_HOURS`, aggregates per-tenant failure stats (digit-normalized fingerprints, top 5 recurring errors), asks Claude for a one-paragraph narrative + key concerns + recommended actions, persists to `incident_summaries`.
- **Shared conventions**: `client.messages.parse()` with a Pydantic `output_format` (no raw JSON); frozen system prompt with `cache_control: ephemeral`; `claude-opus-4-7` with adaptive thinking; usage block persisted for cost telemetry; every feature off by default.
- **Focus:** structured outputs, prompt caching for cost, graceful degradation when the LLM is offline, observable cost telemetry. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).

### Phase 11: Real-time Stream Analytics 🟡
- **Kafka Streams or Flink** topology consuming the lifecycle topics; materialized views for live customer-facing dashboards (per-tenant throughput, latency percentiles).
- **ClickHouse** for historical OLAP (millions of events queryable in under a second).
- **Pre-aggregated time-series materialized views** — 1-minute, 1-hour, 1-day rollups for the dashboard; backfilled from the immutable Kafka log.
- **Customer-facing analytics API** — query parameters validated; tenant isolation enforced at the storage layer.
- **Sub-second p99** target on the live dashboard endpoints.
- **Focus:** streaming semantics (windowing, watermarks), low-latency reads at scale, OLTP / OLAP separation.

### Phase 12: Multi-tenancy ✅

Shipped across PRs `#35`–`#38` (see the per-PR breakdown under "Current Implementation Status"). One bullet below did **not** ship and has no owner: per-tenant billing telemetry — there is no `usage.*` event and no chargeable-action aggregate anywhere in `backend/app/`. Everything else on this list is live.

- **Tenant model** — `tenants` table; every existing tenant-scoped table gets a `tenant_id` FK; all queries scoped by tenant.
- **Postgres row-level security** — RLS policies on every tenant-scoped table; backend connects with a tenant-scoped role; impossible to leak data across tenants even with a query bug.
- **Per-tenant Kafka partitioning** — change the partition key from `user_id` to `tenant_id` (or hash of `(tenant_id, user_id)`); per-tenant ordering guaranteed.
- **Per-tenant rate limits and quotas** — Redis-backed counters keyed by `tenant_id`; quotas configurable per plan.
- **Per-tenant billing telemetry** — emit a `usage.*` event per chargeable action; aggregate by tenant for monthly invoices.
- **Tenant admin UI** — root admins can list tenants, switch contexts, view per-tenant SLO scorecards.
- **Focus:** isolation guarantees (data, compute, blast radius), per-tenant observability, fair-share scheduling.

### Phase 13: Disaster Recovery & Chaos 🟡
- **Multi-region active-passive** — primary in `us-east-1`, warm standby in `us-west-2`; failover via Route 53 health checks.
- **Cross-region Kafka MirrorMaker 2** — `job.*` topics mirrored continuously; consumer offsets translated.
- **RDS cross-region read replica** + automated promotion runbook.
- **S3 cross-region replication** for artifacts.
- **RPO / RTO SLOs** — RPO ≤ 60 seconds, RTO ≤ 15 minutes; verified quarterly via a game-day exercise.
- **Chaos tests in CI** — `litmus` or `gremlin` injects controlled failures: kill the dispatcher mid-job, drop the broker, partition the DB, simulate DNS failure. Each test asserts the system recovers within an expected window.
- **Backup restore drill** — automated weekly job that restores from snapshot to a scratch RDS instance, runs a smoke test, and reports.
- **Focus:** real distributed systems chops — verifying assumptions about failure modes rather than just talking about them.

### Phase 14: Real Job Processors 🟡

Deferred until the incident-commander agent is wired up and driving eval scenarios against the platform. Today's processors (`app/workers/async_tasks.py`, `thread_adapters.py`, `cpu_processors.py`) simulate work with sleeps + progress updates + deliberate failure paths — intentional for the agent story (deterministic behaviour, easy failure injection, no external side effects during eval runs). Once the agent's Phase 6 remediation loop is stable, these upgrade to do real work so the platform demo has substance beyond the infrastructure story.

- **`csv_upload`** — accept a real file upload via `POST /jobs` multipart, stream to MinIO/S3, parse rows with progress updates every 1000 rows, write parse results back to storage, emit row counts to CloudWatch.
- **`bulk_api_sync`** — hit a real third-party HTTP API (Anthropic, GitHub, or a stub echo service), page through results with rate-limit backoff, persist to the DB. Exercises the circuit-breaker from Phase 6.
- **`report_gen`** — generate a real PDF from job data (matplotlib or WeasyPrint), upload to storage, emit a signed URL back on completion. Demonstrates the multiprocessing pool actually doing CPU-heavy work.
- **`doc_analysis`** — extract text from an uploaded PDF (PyMuPDF or pdfplumber), run a real Anthropic call for summarisation when `LLM_TRIAGE_ENABLED=true`, persist the summary.
- **Real failure modes** — each processor gains realistic failure surfaces: partial file corruption in `csv_upload`, 429s in `bulk_api_sync`, OOM guards in `doc_analysis`. These become new eval-scenario fodder for the agent.
- **Focus:** turning the platform demo from "look at the infrastructure" into "look at the infrastructure moving real work through it" — mostly UX polish for stakeholders who don't read the audit log. Zero impact on the agent's investigation surface (observability is already there).

---

## Repo Structure

```
├── CLAUDE.md                       # this file
├── README.md
├── docker-compose.yml              # postgres + redis + redpanda + minio + backend + frontend
├── Dockerfile                      # backend image (Python 3.12 + uvicorn)
├── alembic.ini
├── pyproject.toml                  # deps, mypy, ruff, pytest config
│
├── .github/
│   └── workflows/
│       └── ci.yml                  # lint, type, test, integration, frontend, infra, workflows, deploy
│
├── context/                        # session history; INDEX.md is the map, archives/ is gitignored
│   ├── INDEX.md                    # one line per session — read first
│   ├── README.md                   # the convention: packing, redaction, immutability
│   ├── pack.sh                     # scrub → verify (independent patterns) → zip
│   ├── pack-selftest.sh            # proves pack.sh still scrubs, and still catches
│   └── archives/                   # gitignored + immutable; absent from a fresh clone
│
├── runbooks/                       # machine-readable runbooks (8, covering the 10 alarms + both SLOs)
│   ├── rb-alb-5xx.yaml
│   ├── rb-ecs-tasks-low.yaml       # also linked by the mcp-tasks-low alarm
│   ├── rb-outbox-relay-stalled.yaml # also linked by the outbox-dead-lettered alarm
│   ├── rb-rds-cpu-high.yaml
│   ├── rb-redis-memory-low.yaml
│   ├── rb-queue-depth-high.yaml
│   ├── rb-slo-job-completion.yaml
│   └── rb-slo-dispatch-latency.yaml
│
├── backend/
│   ├── alembic/                    # DB migrations — 32 revisions, head b6c1d90f4a27
│   │   └── versions/               # `alembic history` is the authority on the chain;
│   │                               # the first is a01d04e830dc_initial_schema.py and the
│   │                               # latest is b6c1d90f4a27_agent_run_record.py
│   ├── app/
│   │   ├── main.py                 # FastAPI app factory + lifespan (start_producer, worker_loop)
│   │   ├── config.py               # Settings / env config (pydantic-settings)
│   │   ├── dependencies.py         # Shared FastAPI dependencies (get_db, get_current_user, …)
│   │   │
│   │   ├── api/                    # HTTP routers
│   │   │   ├── auth.py
│   │   │   ├── jobs.py
│   │   │   ├── sagas.py            # POST /sagas, GET /sagas, GET /sagas/{id}
│   │   │   ├── admin.py            # /stats /slos /runbooks /dlq/* /jobs/{id}/{timeline,triage,replay}
│   │   │   ├── audit.py
│   │   │   └── streaming.py        # SSE progress stream
│   │   │
│   │   ├── models/                 # SQLAlchemy models
│   │   │   ├── base.py             # Base + PortableJSON + TimestampMixin
│   │   │   ├── enums.py            # UserRole, JobType, JobStatus, SagaStatus
│   │   │   ├── tenant.py           # Phase 12 — tenants table + DEFAULT_TENANT_ID
│   │   │   ├── user.py             # + is_platform_admin (Phase 12 PR D)
│   │   │   ├── job.py
│   │   │   ├── job_dependency.py   # many-to-many self-join on jobs
│   │   │   ├── audit.py
│   │   │   ├── outbox.py           # outbox_events
│   │   │   ├── event_log.py        # job_events (event sourcing)
│   │   │   ├── saga.py
│   │   │   ├── triage.py           # job_triages (Phase 10)
│   │   │   └── digest.py           # incident_summaries (Phase 10 PR #41)
│   │   │
│   │   ├── schemas/                # Pydantic request/response DTOs
│   │   │   ├── kafka/              # JSON Schema for each Kafka topic
│   │   │   │   ├── job_submitted.schema.json
│   │   │   │   ├── job_progress.schema.json
│   │   │   │   ├── job_completed.schema.json
│   │   │   │   └── job_failed.schema.json    # also used by job.dlq
│   │   │   ├── job.py
│   │   │   ├── user.py
│   │   │   ├── auth.py
│   │   │   ├── audit.py
│   │   │   └── common.py           # PaginationParams, PaginatedResponse
│   │   │
│   │   ├── repositories/
│   │   │   ├── base.py             # generic BaseRepository[ModelT]
│   │   │   ├── tenant.py
│   │   │   ├── user.py
│   │   │   ├── job.py
│   │   │   ├── job_dependency.py
│   │   │   ├── audit.py
│   │   │   ├── outbox.py
│   │   │   ├── event_log.py
│   │   │   ├── saga.py
│   │   │   ├── triage.py
│   │   │   └── digest.py
│   │   │
│   │   ├── services/
│   │   │   ├── auth.py             # + self-service tenant creation (Phase 12 PR D)
│   │   │   ├── job.py              # JobService — create_job, replay_job, list_jobs
│   │   │   ├── saga.py             # SagaService — create_saga (chain of jobs)
│   │   │   ├── slo.py              # SLO computation from jobs table
│   │   │   ├── runbooks.py         # YAML loader
│   │   │   ├── triage.py           # Phase 10 — DLQ triage LLM service
│   │   │   ├── retry_policy.py     # Phase 10 — LLM-guided retry policy
│   │   │   ├── nl_query.py         # Phase 10 — NL admin queries → JobFilterSpec
│   │   │   └── incident_digest.py  # Phase 10 — periodic per-tenant digests
│   │   │
│   │   ├── workers/
│   │   │   ├── dispatcher.py       # JobDispatcherConsumer + worker_loop (starts all 8 consumers + all 11 loops)
│   │   │   ├── async_tasks.py      # asyncio — bulk_api_sync
│   │   │   ├── thread_adapters.py  # threading — csv_upload
│   │   │   ├── cpu_processors.py   # multiprocessing — doc_analysis, report_gen
│   │   │   │
│   │   │   ├── kafka_producer.py   # publish_* + publish_raw (validation surfaces errors)
│   │   │   ├── kafka_consumer.py   # BaseKafkaConsumer — schema validation, offset commit
│   │   │   ├── schema_registry.py  # JSON Schema loader + validate()
│   │   │   │
│   │   │   ├── audit_consumer.py        # group: audit-writer
│   │   │   ├── sse_consumer.py          # group: sse-broadcaster
│   │   │   ├── event_log_consumer.py    # group: event-log
│   │   │   ├── read_model.py            # group: read-model (per-tenant keys since PR #38)
│   │   │   ├── dependency_resolver.py   # group: dependency-resolver
│   │   │   ├── saga_coordinator.py      # group: saga-coordinator
│   │   │   ├── triage_consumer.py       # group: llm-triage (Phase 10)
│   │   │   │
│   │   │   ├── queue.py            # Redis priority queue (delayed retries; pop_ready_delayed)
│   │   │   └── progress.py         # Redis pub/sub progress events (SSE bridge target)
│   │   │
│   │   ├── core/                   # exceptions, logging, middleware, redis, security, tracing, metrics
│   │   └── utils/                  # rate_limit, quota, cache, decorators, mixins, backpressure, circuit_breaker
│   │
│   └── tests/
│       ├── unit/                   # no I/O, mocked deps
│       ├── api/                    # full FastAPI app, dependency overrides
│       ├── integration/            # 14 files — Testcontainers (Docker-gated: Postgres ×12, Redpanda ×1, Redis ×1)
│       ├── load/                   # Locust
│       └── conftest.py             # SQLite-in-memory + dependency overrides + default_tenant fixture
│
├── frontend/
│   ├── Dockerfile                  # Node build → Nginx
│   ├── nginx.conf.template         # SPA serving + /api/ proxy to ${API_UPSTREAM}
│   │                               # (rendered at container start; the ALB
│   │                               # handles /api/ in prod, so it is inert there)
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx                 # Router + routes
│       ├── main.tsx
│       ├── types.ts                # Mirror of backend Pydantic schemas
│       ├── pages/
│       │   ├── LoginPage.tsx
│       │   ├── RegisterPage.tsx
│       │   ├── DashboardPage.tsx
│       │   ├── JobDetailPage.tsx   # SSE progress + Kafka event timeline (admin)
│       │   ├── AdminPage.tsx       # tabs: overview / jobs / dlq / runbooks / users / tenants / digests / audit
│       │   ├── AdminTenantDetailPage.tsx  # per-tenant drill-down (platform admin)
│       │   ├── SagasPage.tsx
│       │   ├── SagaNewPage.tsx
│       │   ├── SagaDetailPage.tsx
│       │   └── DemoPage.tsx        # the live-demo screen (support+); docs/DEMO.md
│       ├── components/             # Layout, StatusBadge, ProgressBar, Toast, TraceId, JobForm, …
│       ├── hooks/                  # useAuth, useJobStream (SSE), useAsyncData, usePolling
│       ├── api/                    # client.ts, auth.ts, jobs.ts, sagas.ts, admin.ts
│       └── utils/                  # tokens, format (status colors, job-type labels), demoPhase
│
├── infra/                          # Terraform — full AWS stack
│   ├── main.tf                     # provider, backend config
│   ├── variables.tf
│   ├── outputs.tf
│   ├── ecr.tf
│   ├── networking.tf               # VPC, subnets, IGW, security groups
│   ├── iam.tf                      # ECS execution + task roles
│   ├── secrets.tf
│   ├── s3.tf
│   ├── rds.tf
│   ├── elasticache.tf
│   ├── alb.tf
│   ├── ecs.tf                      # Cluster, task definitions, Fargate services
│   └── cloudwatch.tf               # SNS topic + 10 alarms (8 baseline + 2 SLO fast-burn)
│
└── scripts/                        # seed data, migrations, ops helpers
    ├── entrypoint.sh               # alembic upgrade head → db_bootstrap password sync → uvicorn
    ├── eval_safety.py              # shared target gate — every script here that writes or destroys calls it
    └── seed_load_test_users.py
```

---

## Working with This Project — A Few Practical Notes

- **`eval-reset` and `PYTHONPATH` — history, kept for the shape of the bug (2026-08).** Against the then-pinned `v0.4.9` image, `make eval-reset` needed `-e PYTHONPATH=/app:/app/backend`: the image baked in `scripts/`, but `python /app/scripts/reset_eval_state.py` puts `/app/scripts` on `sys.path` rather than `/app`, so `from scripts import seed_eval_fixtures` raised `ModuleNotFoundError`. The `sys.path` fix shipped in a later release, so the workaround is no longer load-bearing on a current image — but the commander still passes the override, and there is no reason to remove it. Worth remembering because the failure was invisible until the reset ran inside the container.
- **The scripts under `scripts/` are gated on their target, not on `ENVIRONMENT`.** `reset_eval_state.py`, `seed_eval_fixtures.py`, `seed_load_test_users.py` and `seed_incident_commander.py` all call `scripts/eval_safety.py`, which refuses when `ENVIRONMENT=production` **and** when the `DATABASE_URL`/`REDIS_URL` in play is not the one `settings` names. The label check alone was the bug (WO-R2-18): it inspected the local process while the DSN chose which database got emptied, so an operator in a normal `development` shell passed it unconditionally. Target identity is `(scheme, host, port, database)` — driver suffix, credentials and query string are deliberately ignored, so the two-URL scheme below (owner vs `incident_app` role) does not read as a different target. Pass `--i-know-what-im-doing` for a deliberate cross-stack run; it does **not** override the production check. Each script prints its redacted target before writing (`reset_eval_state.py` prints it on **stderr**, so the JSON summary on stdout stays parseable by `make eval-reset`).
- **Release ordering for the 2026-08 fix campaign: fixes → new version → re-pin → eval** ([ADR 0013](docs/ADR/0013-release-before-rerun.md), maintainer decision 2026-08-08, superseding the earlier "don't cut a tag before the clean-baseline rerun" note). All campaign fixes merge first; the owner cuts `v0.5.0`; the commander re-pins by digest and reblesses its contract snapshot in one planned re-sync PR (expected diff: `+seed_dlq_messages` plus the enumerated description deltas); only then does the eval run, and that run becomes the new baseline. `master` serving 27 tools vs `v0.4.9`'s 26 is the expected, ledgered rebless delta — not a reason to hold the tag. What the ordering gives up (pre-tag live validation, remedied by a `v0.5.1` cycle if the post-release run finds a live-only bug) is recorded in the ADR. Post-`v0.5.0` drift: `master` served **29** tools with `CHAOS_ENABLED=true` (9 of them chaos) up to v0.6.2, and **30** (10 chaos) after WO-R2-166 below — `get_cache_key_info` (#146) and `create_stuck_dag` (the chaos hook that manufactures a genuinely stuck DAG chain for `remediate_runaway_saga_success`) both landed after the tag and ride the next release as ledgered `+get_cache_key_info` / `+create_stuck_dag` rebless deltas. Count it with `list_tools()` rather than trusting this number; it has drifted before. WO-R2-54 adds two more field-level deltas of the same kind: `invalidate_cache_key` and `get_cache_key_info` both say in their `description` and their `key` field description that tenant-scoped keys are reachable only within the caller's own tenant. No schema *shape* change and no tool-count change — but those description strings are pinned, so they land at the same re-pin. The behaviour behind them is a new refusal, not a new field: a cross-tenant `cache:job:` key now returns the existing `cache_key_forbidden` code where it previously succeeded. Separately, WO-R2-32 widens each `tools/list` *entry* rather than the tool count: every tool now advertises `required_scope` and `is_idempotent` alongside `inputSchema`/`outputSchema`. Additive — existing pinned keys are unchanged — but the commander sees it at the same re-pin, so it is a field-level rebless delta on top of the tool-level ones above. WO-R2-55 adds one more of that kind, on a tool that is itself still an unmerged-into-the-baseline delta: `create_stuck_dag`'s `description` and its `chain_name` field description now say ids derive from `{tenant_id}:{chain_name}:{role}` rather than `{chain_name}:{role}`. Shape unchanged, tool count unchanged — but a scenario that pre-computes the root id must now feed the tenant id into the uuid5 key, so this is a rebless delta *and* a caller-visible contract change. Two behaviour changes ride with it: the same `chain_name` in two tenants now builds two independent chains instead of colliding, and a repeat call whose `waiting_steps` is smaller than the stored chain is refused with `stuck_chain_name_in_use` instead of reporting the chain intact. WO-R2-158 adds the first batch of deltas since v0.6.1 that move **schemas** rather than only description strings, so the ledger needs the field list: `list_dlq_messages`' `DlqEntry` gains `fenced_at` + `fenced_by` (output only — no new filter, no input change); `mark_dlq_permanent`'s output gains `fenced_at`; `create_bad_data_job` gains `fixture_name` + `remediation_hint` on input and `fixture_name` + `created` on output, with `remediation_hint` widening from `str` to `str | None`. Description deltas on all three, plus one behaviour change with teeth: `mark_dlq_permanent` on a row already `human_required` used to take an `already_marked` early return and write **nothing** — not the row, not even an audit row — and now always stamps the fence and always audits, so a previously silent no-op is a visible write. The other behaviour change is disposal: `create_bad_data_job` rows now carry `payload.seeded_fixture` and are DELETEd by the eval reset instead of cancelled. The shape list is pinned by `backend/tests/unit/test_fence_tool_descriptions.py::test_the_output_shape_deltas_are_exactly_these`. WO-R2-166 adds the next batch, and it is the first since v0.5.0 to move the **tool count**: `create_mislabeled_dlq_job` is a new chaos tool, so `master` with `CHAOS_ENABLED=true` serves **30** tools (10 of them chaos) and the ledgered delta is `+create_mislabeled_dlq_job`. Alongside it, `poison_message` gains `fixture_name` + `remediation_hint` on input and `fixture_name` + `remediation_hint` + `created` on output, with a rewritten description; `create_bad_data_job`'s description loses the sentence that called `poison_message` a `replay_safe` producer. Three behaviour changes ride with them, all agent-visible: (1) `poison_message`'s dead-letter row is no longer `replay_safe` — it is NULL-hinted by default, `human_required` on request, and `replay_safe` cannot be asked for, because the hook injects a schema violation and a schema violation never becomes replayable (this reverses what the tool advertised for four releases, so a re-pin that does not mention it is a surprise); (2) two new refusal codes reach the commander's ChaosClient — `poison_fixture_name_in_use` and `mislabeled_fixture_name_in_use`, both 409 — and an unknown code is bucketed as a transport fault there, so they belong in the ledger; (3) disposal again — `poison_message` rows now carry `payload.seeded_fixture` and are DELETEd by the eval reset instead of cancelled, which means no chaos hook writes an undeclared DLQ row any more and `_sweep_nonfixture_dlq` is left catching only organic dead-letters and pre-marker legacy rows. The shape list and the refusal codes are pinned by `backend/tests/unit/test_poison_and_mislabel_tool_descriptions.py::test_the_shape_deltas_are_exactly_these` and `::test_the_new_refusal_codes_are_exactly_these`. **WO-R3-187 adds a delta of a kind the ledger has not carried before: none of it is in `tools/list`.** The chaos-audit withholding and the two-token split change no tool name, description or schema — the registry's `tools/list` is byte-identical to v0.6.4 on both sides of the branch (30 tools with `CHAOS_ENABLED=true`, same sha256), so there is nothing to rebless. What changes is what a **response** contains for a principal without `chaos:invoke`: `list_audit_events` and `get_trace` no longer return `chaos.` rows, and no longer count them in `total` / `total_audit_events`. The commander still re-pins by digest for the behaviour, and its half of the order (agent token minus `chaos:invoke`, `PLATFORM_CHAOS_TOKEN` for the runner) is what makes the withholding reachable at all. **WO-R3-200 moves the tool count again:** `pause_control_loop` is a new chaos tool, so `master` with `CHAOS_ENABLED=true` serves **31** tools (11 of them chaos) and the ledgered delta is `+pause_control_loop`. Nothing else in `tools/list` moves — no existing tool's name, description or schema changes — but two things about the new one belong in the ledger. It carries a *fifth* `BlastRadius` member, `single_loop`, in its `[chaos: single_loop]` description prefix, which is the first change to that closed enum since Step 0; and its `loop_name` is a closed 11-member enum whose members are background loops, **not** Kafka consumer groups — a caller written against the plan's draft enum (`dependency_resolver`, `saga_coordinator`, `read_model`) gets an invalid-params refusal and should be calling `kill_consumer` instead ([ADR 0027](docs/ADR/0027-control-loop-pause-closed-enum.md)). **WO-R3-201 moves it once more:** `get_outbox_status` is a new `telemetry:read` tool, so `master` with `CHAOS_ENABLED=true` serves **32** tools (still 11 chaos) and the ledgered delta is `+get_outbox_status`. Nothing else in `tools/list` moves. It takes no arguments and returns one reading of the transactional outbox — `unpublished_count`, the oldest and newest waiting ages, `last_publish_at`, and `relay_last_tick_at` / `relay_heartbeat_age_s` — and three things about it belong in the ledger. It is the Family B partner to `get_consumer_lag` and carries the same scope deliberately, so a token that can read one can read the other. `last_publish_at` excludes rows the relay abandoned, because `mark_failed` stamps `published_at` too and a bare `max()` would report a stalled relay as having just delivered. And it reads a Redis key the relay now writes on every pass, `outbox:relay:last_tick` ([ADR 0028](docs/ADR/0028-outbox-relay-heartbeat-and-delivery-reading.md)) — a new platform key, outside the `chaos:*` namespace and therefore untouched by the eval reset; when it is absent the tool answers `relay_heartbeat_known: false` with a reason rather than an age. **WO-R3-274 + WO-R3-275 move it again, and this batch carries a behaviour change with no field to point at.** `pause_dag_chaos` is a new chaos tool, so `master` with `CHAOS_ENABLED=true` serves **33** tools (**12** of them chaos) and the tool-level delta is `+pause_dag_chaos`. Alongside it, `create_stuck_dag`'s input gains `root_status` (`Literal["dead_letter","completed"]`, default `dead_letter` = today's chain byte for byte), `child_age_seconds` (`int`, 0..86400, default 0) and `failed_step` (`int | None`, 1..10, default None), its output gains `step_job_ids` and `dead_letter_job_id`, and its description is rewritten around the three shapes. Count the tools with `list_tools()` rather than trusting the number here, for the reason this bullet already gives twice. Four things about the batch belong in the ledger. **(1) The behaviour change with teeth:** a chain this hook manufactured can now exist with **no dead-letter row anywhere in it** (`root_status="completed"`), so anything that assumed `create_stuck_dag` implies a DLQ row — a grader, a world audit, a precondition, a canned fixture — has to read `dead_letter_job_id` or the shape it asked for. Relatedly, `waiting_job_ids` narrowed to mean what it says: only the descendants actually in `waiting`, with `step_job_ids` carrying what it used to hold. Under the default the two are equal, so no existing caller moves. **(2) The completed-root chain does not hold by itself, and the description says so:** the root being `completed` leaves step-1 with no unmet parent, so the `dependency-resolver` consumer group and `_resume_unblocked_waiting_loop` each promote it within seconds — the stranded world is this hook **plus** `kill_consumer('dependency-resolver')` **plus** `pause_control_loop('resume_unblocked_waiting')`, and only the pause's TTL heals it (WO-R3-213, ADR 0027's 2026-09-17 amendment). The `failed_step` shape is the exception that holds on its own, because no `waiting` row in it has a `completed` parent. **(3) `pause_dag_chaos` is the first chaos hook whose Redis key sits outside `chaos:*` on purpose** — it writes the platform's own `dag:paused:<root_id>` with the value, TTL default (600 s) and bounds (1..3600) `pause_dag` uses, because a pause a scenario sets has to read back through `get_dag_state` as `paused: true` with the same `paused_by` / `paused_expires_in_seconds` an operator pause would produce. Teardown is `_clear_dag_pauses` (`dag_pauses_cleared`), which already existed for the agent's own residue, plus the TTL. It carries no new `BlastRadius` member: `environment_wide`, the label its sibling `create_stuck_dag` carries on the same chain. **(4) No new refusal code reaches the commander's ChaosClient** — the stranded chain reuses `stuck_chain_name_in_use` (including for a repeat call that asks for a *different shape* under an existing `chain_name`, because the ids do not depend on the shape), the two new input validators refuse as JSON-RPC invalid params, and the lab pause reuses `not_found` for a root that is missing or in a sibling tenant. The shape list is pinned by `backend/tests/unit/test_stranded_chain_and_lab_pause.py::test_the_shape_deltas_are_exactly_these` and the count by `::test_the_chaos_surface_grows_by_exactly_one_tool`; the reasoning is [ADR 0029](docs/ADR/0029-stranded-chain-and-lab-pause-are-manufactured.md). **WO-R3-219 + WO-R3-220 move the count by two, and one of the two behaviour changes is in a production processor.** `saturate_db_pool` and `degrade_downstream` are new chaos tools, so `master` with `CHAOS_ENABLED=true` serves **35** tools (**14** of them chaos) and the tool-level deltas are `+saturate_db_pool` / `+degrade_downstream`. Nothing existing in `tools/list` moves: no other tool's name, description or schema changes. Count it with `list_tools()` rather than trusting the number here, for the reason this bullet already gives twice. Four things about the batch belong in the ledger. **(1) Both hooks are mechanism-only until WO-R3-217 lands.** Neither has a read surface today: a held pool is invisible until `get_postgres_health` reports the **API/worker** process's pool rather than the MCP process's own, and an open breaker is invisible until breaker state is shared across processes (divergence H7). A re-pin that ships them without 217's read tools ships two faults nobody can see, which is a fact about the sequence, not about the hooks. **(2) The behaviour change with teeth:** while `chaos:downstream:bulk_api_sync` is set, a `bulk_api_sync` job whose **every** endpoint call failed now *raises* instead of completing with an error count in its result payload — so the job retries and then dead-letters, and `search_traces(job_type="bulk_api_sync", status="failed")` has rows to find. Without it the breaker opened and every tool the agent has still read healthy, because no operational tool reads a job's result payload. It shipped gated on the flag, with the organic path (10% per endpoint) still completing with errors counted, and making the failure unconditional filed as a follow-up rather than smuggled in there. **That follow-up landed on 2026-09-19 (WO-R3-322, owner decision O-31 D4) and is a behaviour change with nothing in `tools/list` to point at:** the raise is now unconditional — the flag chooses whether the endpoints fail or answer late, never whether a job that synced nothing was a success — and the error message names the count (`bulk api sync failed: all N endpoint calls failed (0 of N endpoints returned a result)`). A *partial* failure is unchanged. No tool name, description, schema, scope or `is_idempotent` flag moves (`degrade_downstream`'s description already said this happens under `fail`, which is still true), so there is nothing to rebless; what a re-pin should know is that a `bulk_api_sync` job can now dead-letter on the organic path too, and that such a death spends `job_completion_rate` budget — on a quiet window one is enough for a 14.4× fast burn and therefore a `critical` alert (ADR 0031's 2026-09-19 amendment). **(3) Neither hook adds a `BlastRadius` member and neither leaves residue outside `chaos:*`:** `saturate_db_pool` is `shared_dependency` (the label `saturate_redis` carries — one process's pool is shared by its API handlers and all eleven of its loops) and `degrade_downstream` is `single_service`; the keys are `chaos:db_pool:hold` and `chaos:downstream:bulk_api_sync`, both swept by the reset's existing `chaos:*` scan with no new pattern, and both hooks hold one key each, so a repeat call replaces the state rather than stacking it. Teardown is the TTL, the reset, or a worker restart. Dead-lettered jobs left behind by (2) are organic DLQ rows and the reset's `_sweep_nonfixture_dlq` already catches those. **(4) No new refusal code reaches the commander's ChaosClient** — both hooks refuse only as JSON-RPC invalid params. The shape list for both is pinned by `backend/tests/unit/test_saturate_db_pool.py::test_the_shape_deltas_are_exactly_these` and the count by `::test_the_chaos_surface_grows_by_exactly_two_tools`; the reasoning is [ADR 0031](docs/ADR/0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md). **WO-R3-217 moves the tool count by two, and it is the first batch whose delta includes fields that are deliberately always null.** `get_slo_status` and `get_circuit_breakers` are both new `telemetry:read` tools, so `master` with `CHAOS_ENABLED=true` serves **37** tools (still **14** chaos, because both of these are read tools) and the read tier goes from 14 to 16; the tool-level deltas are `+get_slo_status` and `+get_circuit_breakers`. Count them with `list_tools()` rather than trusting this number. Alongside them `get_postgres_health`'s output gains **twelve** fields and no input changes: `pool_size`, `pool_checked_out`, `pool_overflow`, `pool_max_overflow`, `pool_wait_timeouts_1m`, `pool_stats_unknown_reason`, `longest_active_query_ms`, `active_queries_over_slow_threshold`, `slow_query_threshold_ms`, `p95_query_ms_1m`, `slow_query_count_1m` and `query_stats_unknown_reason` — more names than the plan's five, because two of the plan's fields ship unmeasurable, each unknown carries its own reason string, and the two readings that can actually be taken are added beside them. Four things about the batch belong in the ledger. **(1) Two fields are null in every response:** `p95_query_ms_1m` and `slow_query_count_1m`, with `query_stats_unknown_reason` carrying one of three fixed sentences — `pg_stat_statements` is not installed anywhere, and it could not answer a one-minute percentile if it were, so the honest readings beside them (`longest_active_query_ms`, `active_queries_over_slow_threshold`, both from `pg_stat_activity` and both live) are what a Family A scenario must grade on ([ADR 0030](docs/ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md)). A commander-side output model with `extra="ignore"` silently drops fields, so check the config on all three models at the re-pin. **(2) The pool fields describe the pool of the process that answered the call**, which on the agent's surface is the MCP process — the API and worker pools are not visible, and every field description says so. **This does not close WO-R3-219's D1:** `saturate_db_pool` holds the API/worker pool, and no field here reports that pool, so the held pool is still invisible to the agent; closing it needs a per-process gauge published the way breaker state is, and WP-8.5's `db_pool` scenario must grade on something else until then. **(3) One new Redis key namespace,** `breaker:state:<name>`, outside `chaos:*` and therefore untouched by the world reset, catalogued in `docs/REDIS.md`; an absent record makes a breaker *absent from the listing*, never reported closed, and an unreachable store returns an empty listing with `unknown_reason` set. **(4) No new refusal code and no new scope** — both tools take no arguments at all (`extra="forbid"` on an empty input model), so there is nothing to refuse, and both carry `telemetry:read` so a token that can read consumer lag can read these. The shape list is pinned by `backend/tests/unit/test_phase8_read_tool_shape_deltas.py::test_the_shape_deltas_are_exactly_these` and the read tier by `::test_the_read_tier_grows_by_exactly_two`. **WO-R3-225 moves no count at all, and it is the first batch whose whole point is a behaviour change in a tool it does not touch.** `kill_consumer` gains one input, `sticky` (`bool`, default `False` = today's kill byte for byte), and three outputs, `sticky` (`bool`), `sticky_key` (`str | None`, the second key a sticky kill wrote or null) and `expires_at` (`datetime`, ISO-8601 UTC, when this kill ends); its description is rewritten around the option. No new tool, so `tools/list` gains no name and the tool-level delta is **empty** — the surface stays at whatever the release before it served, with the same chaos count. Four things about it belong in the ledger. **(1) The behaviour change with teeth is in `restart_consumer_group`, which is unmodified.** Under a sticky kill that action deletes the flag, answers `kill_key_cleared: true` and `accepted: true` — truthfully, that is what it did — and the consumer group is **still down**, because the kill-state read re-arms the flag before the supervisor restarts anything. Every caller that inferred recovery from that reply is now wrong: read the group's own state. The tool's description already said `accepted` does not assert a restart and that recovery must be confirmed through group membership; that sentence is now load-bearing. And this is exactly where the reused-idempotency-key lesson bites (a repeat call under one key returns a perfect success that did nothing), so reset with `PURGE_IDEMPOTENCY=1` between attempts and grade on the resource. **(2) The window is absolute, not rolling.** `chaos:kill_sticky:<group>` holds the deadline `ttl_seconds` gives; the re-arm sets the flag with `PXAT` at that instant, so restarting any number of times cannot extend it, and the marker's own TTL ends it on Redis's clock even if nothing reads it again. A scenario can therefore precondition on `expires_at`. The one caveat recorded rather than solved: the hook computes the deadline in the MCP process and the worker process compares it, so clock skew between them shifts it — immaterial on one host, and the marker's Redis-side TTL bounds it regardless. **(3) No new `BlastRadius` member, no residue outside `chaos:*`, no new key pattern for the reset:** `single_consumer` as before, `chaos:kill_sticky:*` swept by the existing `chaos:*` scan, and one marker per group so a second sticky call replaces the window instead of stacking it. Teardown is the deadline, the reset, or both keys being deleted; an unreadable marker fails open and releases the group. **(4) No new refusal code reaches the commander's ChaosClient** — a bad `sticky` refuses as JSON-RPC invalid params. The shape list is pinned by `backend/tests/unit/test_kill_consumer_sticky.py::test_the_shape_deltas_are_exactly_these`, the absent tool-level delta by `::test_the_chaos_surface_does_not_grow`, and `restart_consumer_group`'s unchanged output by `::test_the_restart_actions_shape_is_unchanged`; the reasoning is [ADR 0032](docs/ADR/0032-a-sticky-kill-re-arms-and-its-window-is-absolute.md). Flipping the commander's `remediate_verify_fails` scenario to live is a separate decision with its own precondition and its own first-paid-run review, and is deliberately not bundled with this. **WO-R3-218 moves the count by one, and it is the first batch whose delta exists because a field in the batch before it is permanently null.** `slow_db_queries` is a new chaos tool, so `master` with `CHAOS_ENABLED=true` serves **38** tools (**15** of them chaos) and the tool-level delta is `+slow_db_queries`; the read tier does not move and nothing existing in `tools/list` changes — no other tool's name, description or schema. Count it with `list_tools()` rather than trusting the number here, for the reason this bullet already gives three times. Four things about it belong in the ledger. **(1) The evidence clause of the plan's WP-8.2 is restated, not delivered.** WP-8.2 specifies the fault's signature as `p95_query_ms_1m` high with `pool_wait_timeouts_1m` ~0, and `p95_query_ms_1m` is null in every response and always will be ([ADR 0030](docs/ADR/0030-breaker-state-is-published-and-a-reading-is-never-invented.md)). The signature this hook actually produces, and the one a Family A scenario must grade on, is **`longest_active_query_ms` rising and `active_queries_over_slow_threshold` above 0 while `pool_wait_timeouts_1m` stays 0 and `pool_checked_out` is normal** — the pair, not either half. **(2) The mechanism is real long-running statements in the worker process, which is why the pool reading stays clean.** An application-level delay (the cheaper option the order offered) would move no reading the agent has: it is not a running query, so `pg_stat_activity` shows nothing. So the hook runs real reads of one declared relation held open by a server-side sleep, two at a time offset by half a chunk — because one sleeper makes `active_queries_over_slow_threshold` read 0 for the first 500 ms of every chunk, and a fixture that is wrong a quarter of the time is worse than no fixture. The `pool_*` fields on the agent's surface describe the MCP process's pool and this fault lives in the worker's, so A1's "queries slow, pool fine" contrast with `saturate_db_pool` falls out of where the fault runs rather than being arranged. **(3) What it does NOT do, so no scenario grades on it:** the platform's own jobs still run at their normal speed — job durations, `get_consumer_lag`, `get_outbox_status` and `get_slo_status` are untouched. It is a slow database, not a slow platform. And `chaos:db_query:slow` is the first chaos key whose effect outlives the reset's sweep at all, by exactly the one query still in flight: at most `query_ms`, 2 s by default and 10 s at the ceiling, which is what the chunked design buys over a single long sleep that would have held a connection for the whole TTL. **(4) No new refusal code, no new `BlastRadius` member, no new key pattern for the reset** — `shared_dependency` as `saturate_db_pool` carries, `chaos:db_query:slow` swept by the existing `chaos:*` scan, one key so a repeat call replaces the fault, and an undeclared `target` refuses as JSON-RPC invalid params against a closed three-member enum (`job_reads`, `audit_reads`, `outbox_reads`). The shape list is pinned by `backend/tests/unit/test_slow_query_hook.py::test_the_shape_deltas_are_exactly_these` and the count by `::test_the_chaos_surface_grows_by_exactly_one_tool`; the reasoning is [ADR 0034](docs/ADR/0034-a-slow-query-is-manufactured-where-the-server-can-see-it.md). **WO-R3-312 moves the count by two, and it is the first batch to add a scope since Step 0.** `report_agent_run` and `report_agent_briefing` are new tools, so `master` with `CHAOS_ENABLED=true` serves **40** tools (still **15** chaos) and the tool-level deltas are `+report_agent_run` / `+report_agent_briefing`. Count them with `list_tools()` rather than trusting the number here, for the reason this bullet already gives four times. **The read tier does not move** — it stays at 16 — and nothing existing in `tools/list` changes: no other tool's name, description, schema, scope or `is_idempotent` flag. Seven things about the batch belong in the ledger. **(1) A new scope, `agent_runs:write`, which is the first change to ADR 0007's five-member enum since Step 0.** Both tools declare it, nothing else does, and a token minted before this release does not carry it — `scripts/seed_incident_commander.py` adds it to the agent account's defaults and the commander's own bootstrap script has to mirror that, so a re-pin that ships the tools without the scope ships two tools the agent cannot call. It is grantable through the admin API, unlike `chaos:invoke`. **(2) A new description prefix, `[commander: telemetry]`, and the commander's planner must filter on it** exactly as it filters `[chaos:`. These calls are made by the loop's checkpoint hook, not chosen by a model; leaving them in the planner's tool list would offer the model two calls that change nothing and spend budget. The platform's half is that the prefix is stable, that the registry flag and the prefix always agree, and that no read-scoped tool carries it. **(3) One input field's wire name is deliberately not its column name:** `report_agent_run.run_label` lands in `agent_runs.scenario`. ADR 0012's registry screen bans the lab's own vocabulary from any non-chaos tool's `tools/list` surface, the screen's own rule is to reword rather than weaken, and a new exemption for this family was declined because the agent's principal holds the scope and can therefore read these descriptions. A commander-side model that maps `scenario` straight through will be refused by `extra="forbid"`. **(4) The `state` enum is the commander's own `IncidentState` values, character for character** — `triage` / `investigating` / `planning` / `awaiting_approval` / `remediating` / `verifying` / `resolved` / `escalated` / `failed`, closed on the wire with `Literal`. There is no mapping layer on either side by design, so a member added in one repository and not the other is a refusal at the wire rather than a silently dropped state; `triaging` and an omitted `awaiting_approval` were both wrong in the order's draft and were corrected before this landed. Only `resolved` / `escalated` / `failed` close a run. **(5) Three new refusal codes reach the ChaosClient's sibling path** — `agent_run_already_finished`, `agent_run_briefing_already_recorded` and `agent_run_not_found`, all 409 — and an unknown code is bucketed there as a transport fault, so they belong in the ledger. All three are sequencing mistakes on the caller's side, and all three leave the incident and the run untouched; the reporter is fail-open, so it must log them and continue. **(5) A fourth audit action, `agent.run_reported`,** which these two tools write *instead of* `agent.tool_invoked` — so a commander-side assertion that every MCP call appears in the `agent.tool_invoked` stream is now false for these two. Same `extra_data` shape. It is withheld from any principal holding `agent_runs:write` (`hidden_audit_action_prefixes`), which is a **behaviour change with no field to point at**: after this release the agent's own `list_audit_events` and `get_trace` return fewer rows and a smaller `total` than the raw table holds, and asking for the stream by name is an empty page rather than an error. **(6) `get_consumer_lag` is byte-identical but its code moved:** the reading now lives in `app/core/consumer_lag.py` so the operator REST twin computes the same number from the same function (`app.api` may not import `app.mcp`). No name, description, schema or behaviour change — worth a line only because a re-pin diffing file paths rather than the wire will see it. **(7) No new Redis key, no new `BlastRadius` member, and one new table:** `agent_runs`, tenant-scoped under the strict policy with FORCE RLS and a partial active index, not swept by the eval reset (recorded, not solved). The shape list, the counts, the refusal codes and the `run_label` → `scenario` mapping are pinned by `backend/tests/unit/test_agent_run_contract.py`; the reasoning is [ADR 0035](docs/ADR/0035-the-agent-reports-its-run-and-cannot-read-it-back.md). **WO-R3-289 moves no count and adds no tool, and it is the first batch whose delta exists to correct a field-level statement two earlier batches made on purpose.** `master` with `CHAOS_ENABLED=true` serves the same **40** tools (still **15** chaos, read tier still **16**); no tool is added or removed, no scope is added, and no input schema anywhere changes. `get_postgres_health`'s output gains **two** fields — `pools` and `pool_gauges_unknown_reason` — plus one new `$defs` entry, `PoolGaugeReading`, whose members are `process`, `size`, `checked_out`, `overflow`, `max_overflow`, `wait_timeouts_1m`, `written_at` and `reported_age_s`. Alongside it `saturate_db_pool`'s **description** changes by one clause and nothing else. Five things about the batch belong in the ledger. **(1) It closes WO-R3-219's D1, which WO-R3-217 restated rather than solved.** The held pool was real and unreadable: `saturate_db_pool` holds the API/worker process's connections and every `pool_*` field describes the process that answered the call, which on the agent's surface is the MCP process. Each process now publishes its own pool to `pool:state:<process>` on a ten-second cadence and `pools` lists every process that has, so WP-8.5's `db_pool` scenario can grade on the fault's own signature — the `api_worker` entry's `checked_out` at its `size` plus `max_overflow` with `wait_timeouts_1m` climbing while `longest_active_query_ms` stays normal — instead of on downstream effects. **(2) The five existing `pool_*` fields do not move, in name, meaning or value.** They are still the answering process's own live pool and their descriptions still say so; the group is added beside them. One entry in the group therefore describes the same pool as the flat fields, sampled up to one cadence earlier, and the field description says so rather than the two being deduplicated — a caller comparing them is doing something reasonable. `process` is a **closed two-member set**, `api_worker` (REST API plus the eight consumers and eleven loops, one engine, one pool) and `mcp`, so a commander-side model may key on it; a name outside the pair cannot be written. **(3) An empty `pools` is always accompanied by `pool_gauges_unknown_reason`** — three fixed sentences, closed set: nothing published, the store unreachable, records unreadable. This is ADR 0030's rule applied where it bites hardest, because an empty list that reads as "no process has a pool problem" is the confident wrong answer the order exists to remove. A commander-side output model with `extra="ignore"` silently drops both new fields, so check that config at the re-pin. **(4) The description delta with teeth is on the hook, not the reading.** `saturate_db_pool` used to end "so a pool reading taken there does not show this fault", which is now false of the group and still true of the flat fields; it now says the `pool_*` fields of a reading taken in the MCP process describe that other pool and the `pools` group in the same reading is where the fault shows up. Its two pinned phrases ("the API and worker process's pool", "MCP server is a separate process with its own pool") are unchanged. `get_postgres_health`'s own description is rewritten in three sections for the same reason. **(5) One new Redis key namespace,** `pool:state:*`, outside `chaos:*` and therefore **untouched by the world reset** — correctly, because nothing in it is state a scenario set; it is a live description of a process, republished within a cadence of whatever the reset did. Its TTL is **60 s**, a deliberate inversion of `breaker:state:*`'s 24 h: a pool reading is a sample where a breaker's state is latched, so a process that stops publishing has to drop out of the listing rather than freeze at its last healthy number, and `saturate_redis` evicting the key empties the group *with a reason* rather than reporting stale pools. No new refusal code, no new `BlastRadius` member, no new scope, and `get_postgres_health` now reads Redis where it did not before — outside the probe's SAVEPOINT, fail-known, so a Redis outage costs the group and not the reading. The shape list is pinned by `backend/tests/unit/test_pool_gauge_shape_delta.py::test_the_shape_deltas_are_exactly_these` and the absent tool-level delta by `::test_the_surface_does_not_grow`; the reasoning is [ADR 0033](docs/ADR/0033-each-process-publishes-its-own-pool-gauge.md). **WO-R3-328 moves no count either, and it is the first batch since WO-R3-312 to widen an input schema — five new optional fields on one tool.** `master` with `CHAOS_ENABLED=true` serves the same **40** tools (still **15** chaos, read tier still **16**); no tool is added or removed, no scope is added, no refusal code is added, no `BlastRadius` member is added and `report_agent_briefing` does not move at all. `report_agent_run`'s **input** gains `hypotheses` (a ranked list, best first), `plan`, `verification`, `step` and `budget` — every one optional, so a caller written against v0.6.15 keeps working byte for byte — plus five `$defs` entries: `RankedHypothesisReport` (`name`, `category`, `confidence`, `reasoning_excerpt`), `PlanReport` (`action_tool`, `action_arguments`, `target_hypothesis`, `rationale_excerpt`), `VerificationReport` (`verdict`, `reasoning_excerpt`, `attempt`, `of`), `StepEventReport` (`seq`, `kind`, `tool`, `arguments`, `result_excerpt`, `outcome`, `latency_ms`, `at`) and `BudgetReport` (`tool_calls_used`, `tool_calls_max`, `tokens_used`, `usd_used`, `wall_seconds`). Its **output** gains two, `steps_count` and `steps_dropped`. `HypothesisReport` and `StepReport` — WO-R3-312's two `$defs` — are unchanged in members **and** in meaning, and `phase_history` is untouched. Seven things about the batch belong in the ledger. **(1) The semantics of the five new fields differ from the two beside them, on purpose.** `hypotheses`, `plan`, `verification` and `budget` are replaced when a report carries them and **never cleared** by a later report that omits them, where `current_hypothesis` and `last_step` are still cleared by omission. The reporter now reports after every tool call rather than every transition, and most of those reports say nothing about hypotheses — under replace-or-clear each one would blank the panel the order exists to fill. Changing the older pair to match was declined: a shipped field whose omission quietly started meaning something else is the worse surprise. **(2) `step` takes exactly ONE step per call and `seq` is its identity.** A second step in one report is not expressible; a `seq` already stored changes nothing, so a fail-open retry cannot double-count; the ledger keeps the newest **200** (`STEPS_CAP`) and `verifications` the newest **50** (`VERIFICATIONS_CAP`), oldest first, with `steps_dropped` counting what went — a capped list a reader knows is capped is useful, one it does not is a run that looks shorter than it was. **(3) Three excerpt limits that REFUSE rather than truncate** — 280 for `reasoning_excerpt` and `rationale_excerpt`, 400 for `result_excerpt`. A caller that sends more gets JSON-RPC invalid params, not a silent cut, which is also what keeps this table from becoming a copy of the responder's own trace. **(4) `get_consumer_lag` is shape-identical and its description is not.** `recent_samples` keeps its name, its two members (`lag`, `measured_at`) and its newest-first order; what changes is how many there are — up to **15**, ~15 minutes at one per pass, where it was 5 — and the description and the field text now say 15 minutes. Behind it, `kafka:consumer_lag:{group}:samples` keeps its shape and gains a TTL of its own: **1080s** rather than the value key's 90s, deliberately breaking the old fresh-or-absent pairing, because `check_backpressure` needs the value fresh while history is most wanted at the moment the pass that writes it stopped. No new key, and the key/cap/TTL now live in `app/core/consumer_lag.py` with the worker importing them. **(5) Four REST additions, none of them on the agent's surface:** `GET /admin/agent-runs/{id}/steps?after_seq=` (a tail read, not an offset page — a poller must not be handed duplicates by a list that grows from the end), the seven new fields on `AgentRunResponse`, `sample_window_seconds` / `sample_interval_seconds` on `ConsumerLagResponse`, and `GET /api/v1/audit/logs` taking `action_prefix` as a comma list plus a new `exclude_prefix` (both bounded at 200 characters and ten prefixes; exclusion wins where they overlap; `hidden_audit_action_prefixes` is untouched, because that is a rule about principals on the MCP path and this is the human one). **(6) Seven new columns and one migration** (`b6c1d90f4a27`, head): `hypotheses`, `plan`, `verification`, `verifications`, `steps`, `steps_dropped`, `budget`. No new index — every reader selects the row by primary key or through the existing partial active index, and nothing filters on a JSONB member. **(7) [ADR 0012](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md) rule 1 still holds unchanged**, which is why the excerpts are allowed at all: these are writes with no matching read, there is still no read tool for `agent_runs`, and `agent.run_reported` is still withheld from whoever holds `agent_runs:write` — an excerpt of output the responder itself produced, stored where only an operator can read it, discloses nothing back to the responder. The shape list is pinned by `backend/tests/unit/test_run_record_shape_delta.py::test_the_shape_deltas_are_exactly_these` and the absent tool-level delta by `::test_the_surface_does_not_grow`; the reasoning is [ADR 0037](docs/ADR/0037-a-run-record-carries-the-run.md).
- **Run the backend locally:** `docker compose up postgres redis redpanda minio -d`, then `./.venv/bin/uvicorn app.main:app --reload --app-dir backend`. Set `KAFKA_BOOTSTRAP_SERVERS=localhost:9092` and run the worker as a separate process (the same `app.main` lifespan starts both, so for local dev you typically just run the API and the worker fires in the same process).
- **Run the frontend:** `cd frontend && npm run dev` — proxies `/api` to `http://localhost:8000`.
- **Run tests:** `make test` (unit + API, no Docker needed). The integration tier is a separate target — `make test-integration` — because it needs a reachable Docker daemon and is gated behind `RUN_RLS_TEST` / `RUN_EVAL_RESET_TEST` / `RUN_MIGRATION_LOCK_TEST`, which that target exports for you. Running bare `pytest` collects only `backend/tests/unit` and `backend/tests/api` (`testpaths` in `pyproject.toml`); pointing it at `tests/integration/` directly collects those files but they **skip** unless the gates are set, so a "0 failed" there is not a pass. CI runs `mypy -p app`, `ruff check backend/`, `pytest` and the full integration tier on every PR.
- **The two-URL scheme (`DATABASE_URL` vs `ALEMBIC_DATABASE_URL`):** since WO-P2-03 the runtime `DATABASE_URL` is the **non-owner `incident_app` role** (in compose: `incident_app:localdev`); alembic prefers `ALEMBIC_DATABASE_URL` — the owner URL — and falls back to `DATABASE_URL` when it's unset (local migrate one-shot, tests). The role's password is synced at boot by `python -m app.core.db_bootstrap` from `INCIDENT_APP_DB_PASSWORD` (in compose it rides the `migrate` one-shot, because the app service's custom `command:` bypasses `scripts/entrypoint.sh`). Anything needing owner powers — ad-hoc DDL, backfills — must use the owner URL (`database-url-owner` secret in prod, the `postgres:postgres` URL locally). The boot posture probe logs ERROR on an RLS-bypassing connection and hard-fails only in production ([ADR 0015](docs/ADR/0015-force-rls-and-nonowner-app-role.md), rollout section). Since WO-R2-26 it checks ENABLE as well as FORCE, and the `tenant_isolation` policy's presence, on **every** tenant-scoped table — it previously read FORCE alone on `jobs`, which for the non-owner production role meant it reported ok whatever the server actually had. The table list is derived from the ORM (`tenant_scoped_tables()`), shared with both RLS test tiers, so a new tenant-scoped table cannot ship unprobed.
- **Add a migration:** `cd backend && ../.venv/bin/alembic revision --autogenerate -m "describe change"` — but always **read the generated file** before committing; autogenerate misses things like enum updates and partial indexes.
- **Add a Kafka consumer group:** subclass `BaseKafkaConsumer`, implement `handle_message`, instantiate in `worker_loop` in `app/workers/dispatcher.py`. The base class does schema validation, offset management, and per-message error handling.
- **Add a CloudWatch alarm:** add it to `infra/cloudwatch.tf`, then add the matching `runbooks/rb-*.yaml` file and reference its `/admin/runbooks/{id}` URL in the alarm description.
- **Add an SLO:** declare in `SLOS` in `app/services/slo.py`, write a `runbooks/rb-slo-*.yaml`, and (optionally) add a fast-burn alarm in `infra/cloudwatch.tf`.
- **Session history (`context/`):** read [`context/INDEX.md`](context/INDEX.md) first — one line per session, plus what already turned out to be a dead end. It is the counterpart to the workspace-root `STATE.md`: that says where things are, this says how they got there. `context/archives/` holds packed transcripts and is **gitignored, so it is absent from a clone** — transcripts carry live credentials and the raw set is ~120MB. Do not read an archive into context; pull one file (`unzip -p context/archives/<name>.zip SUMMARY.md`). At session end, `./context/pack.sh <slug>` redacts, verifies, and prints the `INDEX.md` line to paste — add it, since an unindexed archive never gets opened. Archives are read-only and user-immutable, so `rm`, `mv`, truncation and `git clean -xfd` all refuse. Full convention in `context/README.md`.
- **Memory:** the user's auto-memory directory at `~/.claude/projects/.../memory/MEMORY.md` carries durable preferences across sessions — including branching convention (always feature branch, open PR, let user review), no Claude co-authoring on commits, and `.venv/bin/python` for everything.

---

## Glossary

Terms used throughout this codebase. When in doubt, use these exact words.

- **Backpressure** — the API's rejection of new job submissions when the dispatcher's Kafka consumer group is more than `Settings.backpressure_lag_threshold` messages behind. Raises `BackpressureError` (503). Lag is cached in Redis with TTL 90s by the metrics loop; the API never round-trips to Kafka for this check.
- **Burn rate** — the multiplier of SLO error budget being consumed. 1× = budget burns at the rate that exhausts it exactly at the end of the window; 14.4× = budget exhausted in 1 hour out of a 24h window. Fast-burn alarms fire at 14.4×.
- **Compensation** — saga rollback action. When a saga step dead-letters, the coordinator creates one real `jobs` row per already-completed prior step (`saga_id` set, `type = {type}.compensate`) in the same transaction as the outbox row that announces it, in reverse order. Application is responsible for registering processors for `*.compensate` types — an unregistered compensation job dead-letters, which settles the saga as `failed` (not `compensated`). That is the intended forcing function: no `*.compensate` processor is registered in this repo today, so every saga that dead-letters a step ends in `failed`. See ADR 0017 for settlement semantics.
- **CQRS read model** — Redis-backed denormalized job-status sets keyed by `(tenant_id, status)` and `(user_id, status)`. The `read-model` Kafka consumer projects writes from the lifecycle topics; `GET /admin/stats` reads via `SCARD` with no SQL aggregate.
- **Dead-letter** — a job's terminal failure state after exhausting retries (or after the LLM-guided retry policy decides not to retry). Distinct from `failed`; jobs in `failed` will retry, jobs in `dead_letter` won't. Surfaced in `job.dlq` topic and on the admin DLQ tab.
- **Dispatch latency** — wall-clock time from `pending` to `running`. The `job_dispatch_latency` SLO targets 95% within 30s.
- **Error budget** — the inverse of the SLO. 99% SLO → 1% error budget. The Overview tab shows budget remaining %; fast-burn alarms fire when burn rate threatens to consume the budget early.
- **Event-sourced** — describes the `audit_logs` and `job_events` tables. Every Kafka lifecycle event is appended as a row, in arrival order, immutably. The mutable `jobs.status` is a *projection* of these events.
- **Fail open** — when a non-critical dependency is unavailable, allow the request through rather than blocking it. Rate limits fail open on Redis outage; LLM features fail open on Anthropic outage. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).
- **Fast-burn alarm** — CloudWatch alarm watching SLO burn rate over a short window. Fires before the budget is fully consumed so the team can react.
- **Fingerprint (digest)** — the digit-normalized truncation of an error message used by the incident-summary feature to bucket recurring errors. "attempt 1" and "attempt 27" fingerprint to "attempt #" — same bucket.
- **Idempotency key** — caller-supplied string on `POST /jobs`. Composite UNIQUE on `(tenant_id, idempotency_key)`. Re-submitting with the same key returns the existing job rather than creating a duplicate.
- **In-flight** — describes a job that's been popped from Kafka and is currently executing in the dispatcher's task set. Tracked in `consumer.in_flight`; emitted as the `InFlightJobs` CloudWatch gauge.
- **JobFilterSpec** — the constrained Pydantic shape Claude returns from the NL query feature. Enum/literal fields only; the model can never smuggle SQL.
- **LLM badge** — small purple `LLM` tag on the admin DLQ row indicating the LLM-guided retry policy forced the dead-letter before retries were exhausted. Driven by the persisted `jobs.dead_lettered_by == 'llm_retry_policy'` (exposed on the REST `JobResponse`, not on any MCP tool output), **not** by `retry_count < max_attempts` — that arithmetic badged every saga compensation job, which dead-letters at `retry_count=0` by design, even with LLM features off.
- **Outbox** — the transactional handoff between DB state changes and Kafka publication. Same transaction writes the state change and the `outbox_events` row; a background relay publishes within ~1s. See [ADR 0001](docs/ADR/0001-outbox-vs-cdc.md).
- **Partition key** — `{tenant_id}:{user_id}` composite string. Preserves per-tenant + per-user ordering. See [ADR 0004](docs/ADR/0004-tenant-id-in-kafka-partition-key.md).
- **Platform admin** — `users.is_platform_admin = true`. Cross-tenant operator who can list/create tenants and pass `?tenant_id=` to scope list/stats endpoints to any tenant. Distinct from a `role=admin` (tenant admin).
- **RLS policy** — Postgres row-level security; the second line of defense against cross-tenant data leaks. Defense in depth on top of the application-layer `tenant_id` filter. See [ADR 0003](docs/ADR/0003-rls-as-defense-in-depth.md).
- **Saga step** — a job inside a saga (`saga_id IS NOT NULL`). Steps are linearly ordered via `job_dependencies` (step N depends on step N-1). Saga lifecycle is driven by the `saga-coordinator` consumer.
- **Tenant admin** — `role=admin` without the platform flag. Can manage everything within their own tenant; cannot see sibling tenants.
- **Trace ID** — the OTel trace identifier, propagated from browser → API → worker → DB. Logged on every entry, stored on `jobs.trace_id`, paste-filterable on the admin Jobs tab.
- **Triage analysis** — the LLM-produced classification of a dead-lettered job. Persisted to `job_triages`; one row per job (UNIQUE constraint makes Kafka redelivery a no-op).

---

## Auth & tenant matrix (quick reference)

Three role tiers, with `is_platform_admin` as an additive cross-tenant flag. Full matrix and per-tab permissions in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#auth--tenant-matrix). Quick read:

| Capability | `user` | `support` | `admin` (tenant) | `+is_platform_admin` |
|---|---|---|---|---|
| Create + see own jobs | ✓ | ✓ | ✓ | ✓ |
| See other users' jobs (own tenant) | — | ✓ | ✓ | ✓ |
| Replay / resolve | — | ✓ | ✓ | ✓ |
| Admin Tenants tab | — | — | — | ✓ |
| Cross-tenant `?tenant_id=` | — | — | — | ✓ |
| Manage tenant limits | — | — | — | ✓ |

Enforcement is layered: application-layer filter in `JobService.list_jobs` + Postgres RLS via `set_config('app.tenant_id', …)` in `get_current_user`. RLS catches the bug class "forgot a WHERE clause". All 11 tenant tables carry the `tenant_isolation` policy with `FORCE ROW LEVEL SECURITY`, so it binds the table owner the app connects as; `users` is the single exclusion (auth reads it pre-context — ADR 0003 bootstrap), `deploy_markers` additionally admits `tenant_id IS NULL` rows, and `audit_logs` is UPDATE/DELETE-immutable via RESTRICTIVE policies ([ADR 0015](docs/ADR/0015-force-rls-and-nonowner-app-role.md)).

---

## Failure mode catalog (quick reference)

What degrades when a component dies. Full detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#failure-mode-catalog).

| Component down | What still works | What degrades |
|---|---|---|
| Postgres | Nothing | All API requests 500 |
| Redis | API, DB writes, workers | Rate limits / backpressure / cache / SSE updates fail open |
| Kafka | API accepts new jobs (outbox queues them) | New job execution stalls; SSE updates stop |
| Anthropic API | Everything | DLQ triages absent; retry policy → deterministic; NL queries return 503; digests stall |
| Worker process | API accepts new jobs (outbox queues them) | No job execution; restart resumes from committed offsets |
| API process | Worker, other replicas | New HTTP requests fail until replica restarts |

The truth lives in Postgres + Kafka. Redis and Anthropic are performance + UX dependencies, never correctness ones.

---

## LLM cost model (quick reference)

Approximate per-call costs at Opus 4.7 pricing. Full breakdown + cache-hit telemetry shape in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#cost-model-llm-features).

| Feature | Per call | Typical cadence | Per month (representative) |
|---|---|---|---|
| DLQ triage | ~$0.012 | every dead-letter | ~$3 |
| Retry policy | ~$0.005 | every retry past first | ~$15 |
| NL admin query | ~$0.006 | per admin search | ~$5 |
| Incident digest | ~$0.018 | per tenant per day | ~$2 |

Total Phase-10 LLM spend: <$30/mo in a representative setup. Cache hit rates and token counts are stored on every record (`usage` JSONB column on `job_triages` and `incident_summaries`).

---

## Conventions (expanded)

### Errors

Custom exception hierarchy rooted at `AppError` (`backend/app/core/exceptions.py`). Each subclass declares `status_code` and `error_code`. The middleware catches `AppError` and produces a uniform JSON envelope:

```json
{
  "error_code": "quota_exceeded",
  "message": "Monthly job quota reached for tenant acme (100000 / 100000).",
  "details": {},
  "request_id": "..."
}
```

When adding a new error: subclass `AppError`, set `status_code` + `error_code`, raise from the service layer. Don't catch and re-wrap arbitrary exceptions — the middleware turns uncaught exceptions into 500s with `error_code: internal_error`.

### Migrations

- One migration per logical change.
- Always include `downgrade()`. Even if it's `op.execute("...")` with state we can't fully undo, the down path must exist.
- Inline a rationale at the top of every migration. Six months from now, the *why* is the most valuable thing in the file.
- Never edit a generated revision after it's merged. New change = new revision.
- After `alembic revision --autogenerate`, **read the diff** — autogenerate misses enum updates, partial indexes, and data-only changes.

### Tests

Three layers, each with a clear purpose. Picking the right one matters:

- **Unit (`backend/tests/unit/`)** — services, processors, validators, repositories, consumers. **No I/O**. SQLite in-memory if a DB is needed (via the `db_session` fixture); mocks for Redis/Kafka/Anthropic.
- **API contract (`backend/tests/api/`)** — full FastAPI app via httpx ASGITransport, dependency overrides swap in SQLite + mock Redis. Tests request/response shape + auth + error envelope.
- **Integration (`backend/tests/integration/`)** — real Postgres, Redpanda or Redis via Testcontainers. Fourteen test files. Tests the things only a real DB / broker / Redis can prove (RLS enforcement and tenant isolation, audit-log immutability, outbox single-writer exclusivity, the migration advisory lock, Kafka redelivery, schema validation end-to-end, that a paused outbox relay grows the outbox while dispatcher lag stays flat, that a killed dependency resolver plus a paused resume sweep strand a `WAITING` child with nothing dead-lettered and nothing paused, that a held connection pool times acquisitions out while the queries it does serve stay fast, and that a sticky kill's re-armed flag expires at its original deadline however many restarts happened inside the window). Docker-gated, and three of the fourteen files carry an **opt-in** env gate as well: `RUN_RLS_TEST`, `RUN_EVAL_RESET_TEST`, `RUN_MIGRATION_LOCK_TEST`. Those files are skipped when the variable is **unset** — set it to `1` to run them, which is what `make test-integration` and the `integration` CI job both do.

When in doubt: write a unit test. Move up only when you need the real thing.

### Audit log entries

The convention is `<resource>.<verb>` snake-case: `job.created`, `job.replayed`, `saga.completed`, `tenant.created`, `user.registered`, `incident.resolved`. Resource-only events (`job.dead_letter`) drop the verb when the action *is* the state change.

`extra_data` carries event-specific freeform JSON. e.g. `job.dead_letter` includes `{error, retry_count}` plus `{dead_lettered_by, reasoning}` when the LLM forced it.

### Structured logs

Every entry carries `request_id` / `trace_id` / `tenant_id` / `user_id` / `job_id` as context vars. Logger names are module-scoped (`app.workers.dispatcher`). Levels: `DEBUG` for chatter, `INFO` for state transitions, `WARNING` for fall-through behavior (LLM fell back to deterministic; rate limit failed open), `ERROR` for things that need investigation.

### What goes in the audit log vs. structured logs vs. metrics

- **Audit log**: who did what, when. Always tied to a user/tenant/resource. Queryable from the admin Audit tab. The historical record.
- **Structured logs**: ephemeral operational signal. What the system is doing. CloudWatch Logs. Hot for ~30 days.
- **Metrics**: numerical aggregates over time. Time-series. CloudWatch Metrics. Drives alarms + dashboards.

A job creation gets all three: audit log row (`job.created`), structured log entry (`INFO: job created`), metric (`JobCreated` counter).

---

## Common pitfalls

- **Adding a new endpoint that lists rows without filtering by `tenant_id`.** RLS will catch it for tenant admins, but platform admins implicitly bypass via `set_config('app.tenant_id', other)`. Always filter explicitly at the application layer; let RLS be the safety net.
- **Forgetting to seed the default tenant in a test fixture.** The `default_tenant` fixture in `conftest.py` is the source of truth; new fixtures that create users must depend on it.
- **Adding a Kafka producer without going through the outbox.** Direct publish (`publish_*`) skips the atomicity guarantee. Only `job.progress` uses the direct path; everything else routes through `outbox_events`.
- **Renaming a Kafka field.** Backward-incompatible. Add a new field, deprecate the old, drop after every consumer reads the new one. See the rules in [`docs/KAFKA.md`](docs/KAFKA.md#schema-evolution-rules).
- **Mutating an event log row.** `job_events` is immutable. The `UNIQUE (kafka_topic, kafka_partition, kafka_offset)` constraint is what makes redelivery idempotent.
- **Calling Anthropic from a request handler synchronously.** All LLM features are async. Wrap with `asyncio.wait_for` if you need a timeout; never block the worker indefinitely.
- **Reading from `jobs:status:*` (pre-Phase-12 keys).** They don't exist anymore. Use the per-tenant keys `jobs:tenant:{tid}:status:{status}`. See [Phase 12 PR D](https://github.com/kudratsingh/incident-platform/pull/38).
- **Catching `AppError` and re-raising as a different type.** The middleware needs the type to know the status code. Re-raise the same instance or let it propagate.
- **Hand-editing a generated Alembic revision after merging.** Future migrations chain off the revision ID; changing it breaks the chain. Make a new revision.
- **Skipping the schema check on a new Kafka topic.** Producers without validation send malformed events; consumers without validation accept them. Every topic in `Settings.kafka_topic_*` must have a matching `.schema.json`. Enforced, not requested: `schema_registry` derives the mapping by walking those fields, so a topic with no schema fails at import rather than going unvalidated, and `validate()` raises `UnknownTopicError` on an unmapped topic instead of returning silently.
- **Calling `JobType(job.type)` outside a try/except.** `JobType` is a `StrEnum` with no `_missing_` hook. Saga compensation types (`csv_upload.compensate`) are NOT valid enum members and coercion raises `ValueError`. A historical bug had `_run_job` doing exactly this — see `test_run_job_dead_letters_compensation_when_no_processor`. If you need to coerce a job type string safely, wrap the call and route unknowns to the DEAD_LETTER path.
- **Catching a DB error and carrying on in the same transaction.** On Postgres the transaction is aborted from that point; every later statement — including the audit row — fails. Wrap the risky query in `app/core/db_degrade.degrade_on_db_error` so the failure rolls back to a savepoint. SQLite does not reproduce this, so a green unit run proves nothing without `tests/conftest.py::AbortingSession`.
- **Putting a caller-supplied value into an `audit_logs` column without bounding it.** The row is written under a savepoint by a helper that never raises, so an over-wide value does not fail the request — it deletes the record of it. `X-Request-ID` was exactly this (WO-R2-51).
- **Fire-and-forget `asyncio.create_task` without exception handling.** The dispatcher spawns `_run_job` this way. Its `_run_and_release` wrapper has a `try/except` safety net that logs + force-dead-letters on escape; anything else you spawn similarly needs its own guard, or exceptions vanish silently.
