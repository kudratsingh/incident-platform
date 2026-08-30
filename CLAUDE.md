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
- [`docs/ADR/`](docs/ADR/) — architecture decision records. Read these to understand *why* the platform looks the way it does:
  - [0001 — Outbox over CDC](docs/ADR/0001-outbox-vs-cdc.md)
  - [0002 — JSON Schema over Protobuf](docs/ADR/0002-json-schema-vs-protobuf.md)
  - [0003 — Postgres RLS as defense-in-depth](docs/ADR/0003-rls-as-defense-in-depth.md)
  - [0004 — Composite tenant_id:user_id Kafka partition key](docs/ADR/0004-tenant-id-in-kafka-partition-key.md)
  - [0005 — LLM features fail open](docs/ADR/0005-llm-features-fail-open.md)
  - [0006 — MCP server as a standalone process from the platform codebase](docs/ADR/0006-mcp-server-standalone-process.md)
  - [0007 — Machine principals with a scope model separate from human roles](docs/ADR/0007-machine-principal-scope-model.md)
  - [0008 — Chaos framework is triple-gated and never in production](docs/ADR/0008-chaos-gating.md)
  - [0010 — Idempotency record lifecycle](docs/ADR/0010-idempotency-record-lifecycle.md)
  - [0011 — DAG pause is enforced by the resolver, not just recorded](docs/ADR/0011-dag-pause-enforcement.md)
  - [0012 — The lab is invisible to the agent](docs/ADR/0012-the-lab-is-invisible-to-the-agent.md) — rule 1 shipped v0.4.9; rule 2 deferred to post-rerun
  - [0018 — Production Kafka is not provisioned](docs/ADR/0018-production-kafka-posture.md) — no broker in `infra/`, ECS deploy gated off, `KAFKA_BOOTSTRAP_SERVERS` omitted unless set
  - [0019 — Stale-RUNNING recovery sweep dead-letters, never re-publishes](docs/ADR/0019-stale-running-recovery-sweep.md) — worker-crash orphans go to the DLQ, not back onto `job.submitted`; revisit once a job can prove it did not partially execute
  - [0022 — Promotable-only resume sweep, and a stranded parent cascades CANCELLED](docs/ADR/0022-promotable-only-resume-sweep-and-dependency-cascade.md) — amends 0011; the sweep's limit now bounds promotable work, and `CANCELLED` gains a second, non-saga writer
  - [0023 — A dispatcher sweep only acts on a row it can prove it owns, and only once per window](docs/ADR/0023-dispatcher-sweep-ownership.md) — amends 0019 and 0021; `requeued_at` de-duplicates the stale-PENDING backstop, `heartbeat_at` plus a compare-and-set stop one replica dead-lettering another's running job
  - [0024 — Public registration may found a tenant or join the default one, and nothing else](docs/ADR/0024-tenant-enrolment-policy.md) — unauthenticated `tenant_slug` no longer enrols into an arbitrary existing tenant (403); existing-tenant enrolment moves behind `POST /auth/tenant/members`, admin-only, tenant taken from the token
  - [0025 — The alert severity vocabulary is `low | info | warning | critical`](docs/ADR/0025-alert-severity-vocabulary.md) — user decision; `low` added so the commander's noise branch is reachable from a real alert, `medium`/`high` declined as duplicates of `warning`/`critical`, `unknown` declined as a receiver's default rather than a producer's assertion
- [`docs/postmortems/`](docs/postmortems/) — one file per incident (backfilled or written at the time). Format: Impact / Timeline / Root cause / Detection gap / Fix / Prevention rule adopted.
  - [0009 — Consumer lifecycle and supervision](docs/ADR/0009-consumer-lifecycle-and-supervision.md)
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — open extension ideas, sized + categorized
- [`runbooks/`](runbooks/) — machine-readable on-call playbooks for every CloudWatch alarm + SLO
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
- **Nine background loops** also running in the same process:
  - **Outbox relay** — polls `outbox_events` every second and publishes to Kafka
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
- [ADR 0007 — Machine principals with a scope model separate from human roles](docs/ADR/0007-machine-principal-scope-model.md) — `service_accounts` table; opaque `sa_<random>` bearer tokens; five fixed scopes, non-hierarchical, additive, orthogonal to the human role enum. Shipped in PR #52.
- [ADR 0008 — Chaos framework is triple-gated and never in production](docs/ADR/0008-chaos-gating.md) — `CHAOS_ENABLED` env flag + `chaos:invoke` scope + per-tool blast-radius check; Terraform validation refuses `CHAOS_ENABLED=true` in the production workspace.

### Naming conventions (normative)

**Tool names** — verb-first `snake_case`, one function per tool. Examples: `get_consumer_lag`, `list_dlq_messages`, `list_audit_events`, `restart_consumer_group`, `replay_dlq_messages`, `pause_dag`, `invalidate_cache_key`, `kill_consumer`, `poison_message`, `saturate_redis`, `inject_latency`, `bad_deploy`, `create_stuck_dag`. `snake_case` matches Pydantic field style and serializes cleanly through the MCP `tools/list` response.

**Tool descriptions** — normative, and treated as code. The agent cannot read this file or the docstrings; the `description` string in the `@tool` decorator *is* the whole interface. A description that does not match the query behind it is therefore a functional defect, not a documentation nit, and it fails in the worst possible way: silently, with a confident-looking answer. Three rules, all learned from WO-R2-53:

- **Say which clock.** "Most recent first" is ambiguous where a row has more than one timestamp. `list_dlq_messages` claimed it while ordering by job *submission* time, so the newest dead-letters could sit past the end of the only page the agent could fetch. It now orders by dead-letter time and says so, and emits `created_at` and `dead_lettered_at` as separate fields.
- **Never promise completeness you cap.** `get_trace` promised "every artifact carrying a given trace_id" and hard-capped at 50 jobs / 200 audit rows with nothing to signal it had stopped short — an agent reading 50 of 4000 and concluding anything about the trace was misled by the tool. Either return everything, or state the cap in the description AND return a `truncated` flag with the true total. A capped result the caller knows is capped is useful; one it does not is worse than an error.
- **Filter before the limit, not after.** `search_traces` applied `limit` in SQL and dropped NULL-trace rows in Python afterwards, so untraced jobs spent the result budget and the tool answered "no traces" for traces that existed. Any predicate that decides whether a row belongs in the answer belongs in the query.

State pagination explicitly too: whether an `offset` exists, and whether `total` can exceed what was returned. "No offset" is a fine answer — an unstated one is not.

**Scopes** — `<domain>:<verb>`, fixed enum. Adding a scope is a decision; renaming or splitting one is a token migration. The five scopes:

| Scope | Grants |
|---|---|
| `telemetry:read` | Observability read surface — consumer lag, queue depth, in-flight counts, traces, health snapshots. |
| `incidents:read` | Incident-response read surface — DLQ contents, incident summaries, saga state, per-job history. |
| `actions:propose` | Create a proposal for a Tier 1 or Tier 2 action; does not execute. |
| `actions:execute` | Execute an approved proposal (Tier 1 idempotent, Tier 2 requires an approval reference). |
| `chaos:invoke` | Invoke chaos framework tools. Additionally gated by `CHAOS_ENABLED`. |

The seed principal `incident-commander` currently holds four scopes: `telemetry:read`, `incidents:read`, `actions:execute`, `chaos:invoke`. It was read-only at Step 0; `actions:execute` arrived with the Wave 3 Tier-1 actions and `chaos:invoke` with the self-seeding chaos scenarios, so live remediation evals can break the platform and fix it without an operator in the loop.

Notably **not** granted: `actions:propose`. Tier-2 actions and the approvals subsystem are still unbuilt, so nothing needs it yet — the agent executes Tier-1 directly under an `Idempotency-Key`. Verify the live grant with `SELECT name, scopes FROM service_accounts` rather than trusting this list; it drifted once already.

**Audit events** — same `<resource>.<verb>` snake-case shape as existing events. Every machine-principal action carries `principal_type='service_account'` on the audit row:

- `service_account.created` / `service_account.token_minted` / `service_account.token_revoked`
- `agent.tool_invoked` — every MCP tool call; `extra_data` carries `tool_name`, `arguments`, `scope_used`, `latency_ms`, `outcome`.
- `agent.action_proposed` / `agent.action_approved` / `agent.action_executed` / `agent.action_rejected`
- `chaos.tool_invoked` / `chaos.tool_denied` — chaos activity is a separate stream from `agent.tool_invoked` so it filters cleanly on the Audit tab.

### Runtime shape

- Two deployables from one image: `api` (the existing FastAPI app) and `mcp` (the ASGI entrypoint at `backend/app/mcp/standalone.py`). Same commit, same schemas, same service layer — but **not the same process boot**, and that distinction has bitten once. Anything the API sets up at import time the MCP entrypoint has to set up too; the MCP process ran no observability bootstrap at all until WO-R2-60, so the agent-facing surface emitted unstructured logs with every INFO dropped and exported zero spans while `OTLP_ENDPOINT` was configured for it. Both entrypoints now call `app/core/observability.py::bootstrap_process_observability` (structured logging + tracing + the Redis instrumentor) and `instrument_app` after mounting routes. A third entrypoint gets it by calling one function rather than by remembering four.
- Each process gets its own DB pool sized to its rate limits — the MCP process runs a small pool.
- Both processes (and the worker loops inside the API process) connect as the **non-owner `incident_app` DB role** — DML only, no DDL, no UPDATE/DELETE on `audit_logs`. Migrations run as the owner (RDS master) via `ALEMBIC_DATABASE_URL`; each lifespan runs `assert_rls_posture` after the migration check and refuses to serve in production if the connection would silently bypass RLS ([ADR 0015](docs/ADR/0015-force-rls-and-nonowner-app-role.md)).
- The agent points at `PLATFORM_MCP_URL` for tools and `PLATFORM_REST_URL` for anything else (there shouldn't be much — everything the agent needs should surface as an MCP tool over time).
- Contract stability between agent and platform is verified by contract snapshot testing against the pinned image, per agent-repo ADR 0007.

---

## Stack

### Backend
- **Python 3.12+ / FastAPI** — async API gateway
- **PostgreSQL** — system of record. Tables: `users`, `tenants`, `jobs`, `audit_logs`, `outbox_events`, `job_events`, `job_dependencies`, `sagas`, `job_triages`, `incident_summaries`. Full reference in [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md).
- **Redis** — cache, locks, rate limits, pub/sub progress events, CQRS read-model sets, cached backpressure lag value.
- **Kafka** (Redpanda locally; production Kafka not yet provisioned — see [ADR 0018](docs/ADR/0018-production-kafka-posture.md)) — durable event log; decouples job submission from execution, powers event sourcing and fan-out. Topics: `job.submitted` / `job.progress` / `job.completed` / `job.failed` / `job.dlq`.
- **JSON Schema** — every Kafka topic has a schema in `backend/app/schemas/kafka/`; producer and consumer validate on every message.
- **Object storage** — S3 in production, MinIO locally — for uploaded files and artifacts.
- **Worker layer** — asyncio tasks for I/O-heavy work, a threading adapter for blocking SDKs, a multiprocessing pool for CPU-heavy transforms.
- **Anthropic SDK** (Phase 10, complete) — Claude API for DLQ triage, LLM-guided retry policy, natural-language admin queries, and periodic incident summaries. All features off-by-default; fail open on any API issue. See [ADR 0005](docs/ADR/0005-llm-features-fail-open.md).

### Frontend
- **React + Vite + TypeScript + Tailwind**
- Pages: `LoginPage`, `RegisterPage`, `DashboardPage` (job list + create form), `JobDetailPage` (live SSE progress + Kafka event timeline), `AdminPage` (overview / jobs / DLQ / runbooks / users / audit tabs), `SagasPage` / `SagaNewPage` / `SagaDetailPage` (multi-step workflow management).
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
- Current test count: **726** passing (447 unit + 254 API + 25 integration).
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
           │   Nine supporting loops:                        │
           │     • outbox relay (DB → Kafka)                 │
           │     • delayed-retry promote (Redis → outbox)    │
           │     • dlq replay promote (scheduled replays)    │
           │     • resume-waiting sweep (pause lifted)       │
           │     • stale-PENDING backstop (lost timers)      │
           │     • stale-RUNNING sweep (crash orphans → DLQ) │
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
- Tabs: **Overview** (CQRS stats cards + SLO scorecards), **Jobs** (filter by status / trace ID / type), **DLQ** (with per-type breakdown pills and per-row Replay/Resolve), **Runbooks** (clickable list with a modal showing the diagnosis steps), **Users**, **Audit** (clickable rows opening a metadata modal).
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
1. **Unit tests** (`backend/tests/unit/`) — services, processors, validators, repositories, consumers. 447 tests. No I/O.
2. **API contract tests** (`backend/tests/api/`) — full FastAPI app with dependency overrides; SQLite in-memory DB; mocked Redis. 254 tests.
3. **Integration tests** (`backend/tests/integration/`) — Testcontainers with real Postgres 16 (RLS enforcement, eval-reset SQL, migration advisory lock, outbox relay exclusivity) and Redpanda (Kafka round-trip). 25 tests across five files. Docker-gated; run locally with `make test-integration`, and in CI by the `integration` job.
4. **Load tests** (`backend/tests/load/`) — Locust scenarios for the job submission path.
5. **Failure-mode tests** — circuit-breaker open/close, schema validation rejecting bad payloads, redelivery dedup via unique constraints.

### Tooling
- `pytest` fixtures and parametrization.
- Factories for test data (inline `_make_*` helpers; could grow into proper factories later).
- Testcontainers for the whole integration tier — Postgres 16 in four files, Redpanda in one (Docker availability is skip-gated).
- Coverage gate at 70% in `pyproject.toml`.
- Unit + API tests run on every PR via the `test` job in `ci.yml`; the integration tier runs on every PR via the `integration` job, which sets `RUN_RLS_TEST` / `RUN_EVAL_RESET_TEST` / `RUN_MIGRATION_LOCK_TEST` and then asserts that all five files contributed tests and none skipped.

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
| **Dead-letter queue** | `job.dlq` topic; `dead_letter` job status; `/admin/dlq/*` endpoints; `LlmTriageConsumer` (Phase 10) analyses each entry | Admin replay resets `retry_count`; unregistered `.compensate` types also route here |
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
- **CloudWatch alarms** ✅ — five baseline alarms (`alb-5xx`, `backend-tasks-low`, `rds-cpu-high`, `redis-memory-low`, `queue-depth-high`) plus two SLO fast-burn alarms (14.4× over 1h windows). All notify via SNS topic `${app_name}-alarms`.
- **Circuit breaker** ✅ — `app/utils/circuit_breaker.py` wraps external API calls; opens on N consecutive failures, half-open probe, auto-recover.
- **Structured runbooks** ✅ — `runbooks/*.yaml` at repo root; one per alarm + one per SLO breach (7 total). Each has summary, symptoms, diagnosis steps (with copy-pasteable shell commands), mitigation, escalation, related dashboards. Alarm descriptions reference `/admin/runbooks/{id}` so on-call has a one-click path from PagerDuty.
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

### Phase 12: Multi-tenancy 🟡
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
├── runbooks/                       # machine-readable runbooks (one per alarm + SLO)
│   ├── rb-alb-5xx.yaml
│   ├── rb-ecs-tasks-low.yaml
│   ├── rb-rds-cpu-high.yaml
│   ├── rb-redis-memory-low.yaml
│   ├── rb-queue-depth-high.yaml
│   ├── rb-slo-job-completion.yaml
│   └── rb-slo-dispatch-latency.yaml
│
├── backend/
│   ├── alembic/                    # DB migrations (11, head: e1d24a8b50c2)
│   │   └── versions/
│   │       ├── a01d04e830dc_initial_schema.py
│   │       ├── b2a8f9c7e103_outbox_events.py
│   │       ├── c3e9f1a4d802_job_events.py
│   │       ├── d4b1a8e60305_dag_and_sagas.py
│   │       ├── e7f4c2a91b08_job_triages.py
│   │       ├── f8a1c4e23507_multi_tenancy.py
│   │       ├── a9c2d1e83104_per_tenant_idempotency.py
│   │       ├── b3d8e7a52116_tenant_quotas.py
│   │       ├── c4f8e9a52340_row_level_security.py
│   │       ├── d9c01a7e4f30_platform_admin.py
│   │       └── e1d24a8b50c2_incident_summaries.py
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
│   │   │   ├── dispatcher.py       # JobDispatcherConsumer + worker_loop (starts all 8 consumers + 11 loops)
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
│       ├── unit/                   # 447 tests
│       ├── api/                    # 254 tests
│       ├── integration/            # 25 tests — Testcontainers (Docker-gated: Postgres ×4 files, Redpanda ×1)
│       ├── load/                   # Locust
│       └── conftest.py             # SQLite-in-memory + dependency overrides + default_tenant fixture
│
├── frontend/
│   ├── Dockerfile                  # Node build → Nginx
│   ├── nginx.conf                  # SPA serving, no proxy (ALB handles /api/)
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
│       │   ├── AdminPage.tsx       # tabs: overview / jobs / dlq / runbooks / users / audit
│       │   ├── SagasPage.tsx
│       │   ├── SagaNewPage.tsx
│       │   └── SagaDetailPage.tsx
│       ├── components/             # Layout, StatusBadge, ProgressBar, Toast, TraceId, JobForm, …
│       ├── hooks/                  # useAuth, useJobStream (SSE)
│       ├── api/                    # client.ts, auth.ts, jobs.ts, sagas.ts, admin.ts
│       └── utils/                  # tokens, format (status colors, job-type labels)
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
│   └── cloudwatch.tf               # SNS topic + 7 alarms (5 baseline + 2 SLO fast-burn)
│
└── scripts/                        # seed data, migrations, ops helpers
    ├── entrypoint.sh               # alembic upgrade head → db_bootstrap password sync → uvicorn
    ├── eval_safety.py              # shared target gate — every script here that writes or destroys calls it
    └── seed_load_test_users.py
```

---

## Working with This Project — A Few Practical Notes

- **Running `eval-reset` against the pinned image (`v0.4.9`):** it needs `-e PYTHONPATH=/app:/app/backend`. The image bakes in `scripts/`, but `python /app/scripts/reset_eval_state.py` puts `/app/scripts` on `sys.path`, not `/app`, so `from scripts import seed_eval_fixtures` raises `ModuleNotFoundError: No module named 'scripts'`. The commander's `make eval-reset` passes the override already — **keep it for the rerun.** A `sys.path` fix is on `master` but is not in any released image, so "scripts are baked in, the workaround is retired" is only half true until the next tag. Verified against the published digest, both ways.
- **The scripts under `scripts/` are gated on their target, not on `ENVIRONMENT`.** `reset_eval_state.py`, `seed_eval_fixtures.py`, `seed_load_test_users.py` and `seed_incident_commander.py` all call `scripts/eval_safety.py`, which refuses when `ENVIRONMENT=production` **and** when the `DATABASE_URL`/`REDIS_URL` in play is not the one `settings` names. The label check alone was the bug (WO-R2-18): it inspected the local process while the DSN chose which database got emptied, so an operator in a normal `development` shell passed it unconditionally. Target identity is `(scheme, host, port, database)` — driver suffix, credentials and query string are deliberately ignored, so the two-URL scheme below (owner vs `incident_app` role) does not read as a different target. Pass `--i-know-what-im-doing` for a deliberate cross-stack run; it does **not** override the production check. Each script prints its redacted target before writing (`reset_eval_state.py` prints it on **stderr**, so the JSON summary on stdout stays parseable by `make eval-reset`).
- **Release ordering for the 2026-08 fix campaign: fixes → new version → re-pin → eval** ([ADR 0013](docs/ADR/0013-release-before-rerun.md), maintainer decision 2026-08-08, superseding the earlier "don't cut a tag before the clean-baseline rerun" note). All campaign fixes merge first; the owner cuts `v0.5.0`; the commander re-pins by digest and reblesses its contract snapshot in one planned re-sync PR (expected diff: `+seed_dlq_messages` plus the enumerated description deltas); only then does the eval run, and that run becomes the new baseline. `master` serving 27 tools vs `v0.4.9`'s 26 is the expected, ledgered rebless delta — not a reason to hold the tag. What the ordering gives up (pre-tag live validation, remedied by a `v0.5.1` cycle if the post-release run finds a live-only bug) is recorded in the ADR. Post-`v0.5.0` drift: `master` now serves **29** tools with `CHAOS_ENABLED=true` (9 of them chaos) — `get_cache_key_info` (#146) and `create_stuck_dag` (the chaos hook that manufactures a genuinely stuck DAG chain for `remediate_runaway_saga_success`) both landed after the tag and ride the next release as ledgered `+get_cache_key_info` / `+create_stuck_dag` rebless deltas. Count it with `list_tools()` rather than trusting this number; it has drifted before. WO-R2-54 adds two more field-level deltas of the same kind: `invalidate_cache_key` and `get_cache_key_info` both say in their `description` and their `key` field description that tenant-scoped keys are reachable only within the caller's own tenant. No schema *shape* change and no tool-count change — but those description strings are pinned, so they land at the same re-pin. The behaviour behind them is a new refusal, not a new field: a cross-tenant `cache:job:` key now returns the existing `cache_key_forbidden` code where it previously succeeded. Separately, WO-R2-32 widens each `tools/list` *entry* rather than the tool count: every tool now advertises `required_scope` and `is_idempotent` alongside `inputSchema`/`outputSchema`. Additive — existing pinned keys are unchanged — but the commander sees it at the same re-pin, so it is a field-level rebless delta on top of the tool-level ones above. WO-R2-55 adds one more of that kind, on a tool that is itself still an unmerged-into-the-baseline delta: `create_stuck_dag`'s `description` and its `chain_name` field description now say ids derive from `{tenant_id}:{chain_name}:{role}` rather than `{chain_name}:{role}`. Shape unchanged, tool count unchanged — but a scenario that pre-computes the root id must now feed the tenant id into the uuid5 key, so this is a rebless delta *and* a caller-visible contract change. Two behaviour changes ride with it: the same `chain_name` in two tenants now builds two independent chains instead of colliding, and a repeat call whose `waiting_steps` is smaller than the stored chain is refused with `stuck_chain_name_in_use` instead of reporting the chain intact.
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
- **LLM badge** — small purple `LLM` tag on the admin DLQ row indicating the LLM-guided retry policy forced the dead-letter before retries were exhausted. Driven by the persisted `jobs.dead_lettered_by == 'llm_retry_policy'` (exposed on the REST `JobResponse`, not on any MCP tool output), **not** by `retry_count < max_retries` — that arithmetic badged every saga compensation job, which dead-letters at `retry_count=0` by design, even with LLM features off.
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

- **Unit (`backend/tests/unit/`)** — services, processors, validators, repositories, consumers. **No I/O**. SQLite in-memory if a DB is needed (via the `db_session` fixture); mocks for Redis/Kafka/Anthropic. 447 tests.
- **API contract (`backend/tests/api/`)** — full FastAPI app via httpx ASGITransport, dependency overrides swap in SQLite + mock Redis. Tests request/response shape + auth + error envelope. 254 tests.
- **Integration (`backend/tests/integration/`)** — real Postgres or Redpanda via Testcontainers. 25 tests across five files. Tests the things only a real DB / broker can prove (RLS enforcement and tenant isolation, audit-log immutability, outbox single-writer exclusivity, the migration advisory lock, Kafka redelivery, schema validation end-to-end). Docker-gated, and three of the five files carry an **opt-in** env gate as well: `RUN_RLS_TEST`, `RUN_EVAL_RESET_TEST`, `RUN_MIGRATION_LOCK_TEST`. Those files are skipped when the variable is **unset** — set it to `1` to run them, which is what `make test-integration` and the `integration` CI job both do.

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
