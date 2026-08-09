# Architecture

The runtime topology, request lifecycles, concurrency model, and failure modes of the incident platform. Read this when you're onboarding, designing a new feature that crosses subsystems, or debugging something you don't fully understand.

For per-component reference, see [`docs/DATA_MODEL.md`](DATA_MODEL.md), [`docs/KAFKA.md`](KAFKA.md), [`docs/REDIS.md`](REDIS.md). For specific design decisions, see [`docs/ADR/`](ADR/).

---

## Runtime topology

The platform runs as **three logical processes**:

1. **API process** — FastAPI behind an ALB. Serves `/api/v1/*`. One ECS task with autoscaling target on CPU.
2. **Worker process** — runs `worker_loop` from `app/workers/dispatcher.py`. Hosts eight Kafka consumer groups + nine background loops. One ECS task today (autoscaling on queue depth is a Phase 8 item).
3. **Frontend** — Nginx serving the React SPA. Same ALB, different listener rule.

External dependencies, all managed:

- **Postgres** (RDS) — system of record
- **Redis** (ElastiCache) — cache, locks, Pub/Sub, CQRS read-model sets
- **Kafka** (Redpanda locally; no production broker is provisioned — [ADR 0018](ADR/0018-production-kafka-posture.md)) — durable event log
- **S3 / MinIO** — uploaded files + artifacts
- **CloudWatch** — metrics, logs, alarms
- **AWS Secrets Manager** — DB password, JWT secret, Anthropic API key
- **Anthropic API** (Phase 10 features only) — Claude

The three processes share nothing in-process but coordinate via Postgres, Redis, and Kafka.

```
                            ┌───────────────┐
                            │  React SPA    │
                            │  (Nginx)      │
                            └──────┬────────┘
                                   │ XHR/SSE
                            ┌──────▼─────────────┐
                            │  ALB (HTTPS)       │
                            └──┬──────────────┬──┘
                               │              │
                  ┌────────────▼─────┐   ┌───▼───────────────┐
                  │  FastAPI gateway │   │  Static files    │
                  │  (API process)   │   │                  │
                  └─┬────┬────┬────┬─┘   └──────────────────┘
                    │    │    │    │
            ┌───────┘    │    │    └─────────┐
            │            │    │              │
            ▼            ▼    ▼              ▼
       ┌────────┐   ┌─────────┐   ┌───────┐  ┌──────────┐
       │Postgres│   │ Redis   │   │ Kafka │  │   S3     │
       │(RDS)   │   │(ElastiC)│   │       │  │          │
       └────────┘   └─────────┘   └───────┘  └──────────┘
            ▲           ▲           ▲   ▲
            │           │           │   │
            └───────────┴───────────┴───┘
                        │
                ┌───────▼──────────┐
                │  Worker process  │
                │  (one ECS task)  │
                │                  │
                │  8 Kafka groups: │
                │   dispatcher     │
                │   audit-writer   │
                │   sse-broadcast  │
                │   event-log      │
                │   read-model     │
                │   dep-resolver   │
                │   saga-coord     │
                │   llm-triage     │
                │                  │
                │  9 loops:        │
                │   outbox-relay   │
                │   promote-delayed│
                │   promote-replay │
                │   resume-waiting │
                │   stale-pending  │
                │   stale-running  │
                │   metrics-loop   │
                │   digest-loop    │
                │   idem-reaper    │
                └──────────────────┘
                        │
                        ▼
                ┌──────────────┐
                │  Anthropic   │
                │  API (LLM)   │
                └──────────────┘
```

---

## Request lifecycles

The five most-touched paths annotated. Each one shows the full hop sequence from browser to durable state.

### 1. `POST /jobs` — submit a job

```
Browser                                    API process                                   DB
  │  POST /jobs (JWT in Authorization)        │                                            │
  │ ─────────────────────────────────────────►│ rate_limiter(client+ip)  ──► Redis INCR    │
  │                                            │                                            │
  │                                            │ check_backpressure                          │
  │                                            │   ──► Redis GET kafka:consumer_lag        │
  │                                            │                                            │
  │                                            │ get_current_user                            │
  │                                            │   ──► DB SELECT users WHERE id = ?          │
  │                                            │   ──► DB set_config('app.tenant_id', …)    │  (RLS)
  │                                            │                                            │
  │                                            │ check_tenant_limits                         │
  │                                            │   ──► Redis INCR rate:tenant:…             │
  │                                            │   ──► DB COUNT jobs WHERE tenant_id=?      │
  │                                            │                                            │
  │                                            │ JobService.create_job                      │
  │                                            │   BEGIN TRANSACTION                         │
  │                                            │     INSERT INTO jobs (…)                    │
  │                                            │     INSERT INTO outbox_events (…)           │
  │                                            │     INSERT INTO audit_logs (job.created)    │
  │                                            │   COMMIT                                    │
  │                                            │                                            │
  │  201 Created (JobResponse)                 │                                            │
  │ ◄─────────────────────────────────────────│                                            │
                                              ▲
                                              │ (within ~1s)
                                              │
                                              │ Outbox relay (worker process)
                                              │   SELECT * FROM outbox_events
                                              │     WHERE published_at IS NULL
                                              │   ──► Kafka publish job.submitted
                                              │   UPDATE outbox_events SET published_at=NOW
```

Pre-conditions checked, in order:
1. **Per-client rate limit** (IP-keyed)
2. **Backpressure** (Kafka consumer-group lag)
3. **Auth** (JWT decode + DB user lookup)
4. **Per-tenant rate limit** (Redis sliding window)
5. **Monthly quota** (SQL count of this month's jobs for the tenant)

Each one fails fast with a structured error envelope. The first three protect the API from external load; the last two protect us from abusive tenants.

The DB transaction does three things atomically: writes the job row, writes the outbox row, writes the audit row. All-or-nothing. The outbox row is the durable handoff (see [ADR 0001](ADR/0001-outbox-vs-cdc.md)).

### 2. `GET /jobs/{id}/stream` — SSE progress

```
Browser                          API process              Worker process            Kafka          Redis
  │  POST /jobs/{id}/stream-token  │                          │                       │              │
  │  (Authorization: Bearer JWT)   │                          │                       │              │
  │ ──────────────────────────────►│                          │                       │              │
  │                                 │ authorize job from DB    │                       │              │
  │                                 │ (tenant + ownership)     │                       │              │
  │  {token: <60s stream token>}    │                          │                       │              │
  │ ◄──────────────────────────────│                          │                       │              │
  │  GET /jobs/{id}/stream          │                          │                       │              │
  │  ?token=<stream token>          │                          │                       │              │
  │ ──────────────────────────────►│                          │                       │              │
  │                                 │ validate stream token    │                       │              │
  │                                 │ (type, expiry, job bind) │                       │              │
  │                                 │ SUBSCRIBE job:progress:{id} on Redis             │              │
  │                                 │ ────────────────────────────────────────────────────────────► │
  │                                 │                          │  Worker is running    │              │
  │                                 │                          │  processor(payload)   │              │
  │                                 │                          │ ──► publish_job_progress to Kafka   │
  │                                 │                          │                       │ ──► SSE bridge consumer
  │                                 │                          │                       │       reads & PUBLISH ──►│
  │                                 │ on PUBLISH:              │                       │              │
  │                                 │   format SSE frame       │                       │              │
  │  data: {status: running, 47%}   │                          │                       │              │
  │ ◄──────────────────────────────│                          │                       │              │
  │  data: {status: completed,100%}│                          │                       │              │
  │ ◄──────────────────────────────│                          │                       │              │
  │                                 │ close                    │                       │              │
```

The browser first POSTs for a **stream token**: native `EventSource` cannot set an `Authorization` header, so the stream GET authenticates with a short-lived (60s), single-purpose token in its query string instead of the primary JWT ([ADR 0014](ADR/0014-sse-stream-token-transport.md)). The mint endpoint is where authorization happens — it loads the job tenant-scoped (404 cross-tenant, 403 non-owner) and binds the job id into the token, so a token minted for job X cannot open job Y's stream. The GET validates that token, subscribes to a Redis Pub/Sub channel, and forwards every received message as an SSE frame. On reconnect the client mints a fresh token (the previous one has expired by then). The worker publishes progress directly to Kafka (the only direct-publish path, see [KAFKA.md](KAFKA.md)); the `sse-broadcaster` consumer republishes to Redis Pub/Sub.

If Redis dies mid-stream, the API logs the error and the connection stays open with no further events. The browser eventually times out and reconnects.

### 3. `POST /sagas` — create a multi-step workflow

```
Browser                           API process                              DB
  │  POST /sagas                     │                                       │
  │  {name, steps: [...]}            │                                       │
  │ ───────────────────────────────► │                                       │
  │                                  │  validate steps                       │
  │                                  │                                       │
  │                                  │  SagaService.create_saga              │
  │                                  │    BEGIN TRANSACTION                  │
  │                                  │      INSERT INTO sagas                │
  │                                  │      for step in steps:               │
  │                                  │        INSERT INTO jobs (saga_id=…)   │
  │                                  │        if not first:                  │
  │                                  │          INSERT INTO job_dependencies │
  │                                  │      INSERT INTO outbox_events        │  (first step → job.submitted)
  │                                  │      INSERT INTO audit_logs           │
  │                                  │    COMMIT                             │
  │                                  │                                       │
  │  201 (SagaResponse)              │                                       │
  │ ◄─────────────────────────────── │                                       │
```

All steps after the first are inserted as `JobStatus.WAITING`. They get promoted to `PENDING` (and a `job.submitted` event emitted) by the `dependency-resolver` consumer when their parent completes.

On any step's dead-letter, the `saga-coordinator` consumer marks the saga `COMPENSATING`, cancels still-`WAITING` downstream steps, and enqueues `{type}.compensate` jobs for completed prior steps in reverse order. Compensation processors are application responsibility — an unregistered `*.compensate` job will dead-letter, which is the intended forcing function.

### 4. `POST /admin/query` — natural-language admin search

```
Browser                           API process              Anthropic API                          DB
  │  POST /admin/query                │                          │                                  │
  │  {question: "..."}                │                          │                                  │
  │ ─────────────────────────────────►│                          │                                  │
  │                                    │ check enabled flag       │                                  │
  │                                    │ rate limit / auth        │                                  │
  │                                    │                          │                                  │
  │                                    │ nl_query.parse_question  │                                  │
  │                                    │   ─────────────────────► │                                  │
  │                                    │                          │ messages.parse                   │
  │                                    │                          │   with output_format=JobFilterSpec│
  │                                    │   ◄───────────────────── │                                  │
  │                                    │   JobFilterSpec validated by Pydantic                       │
  │                                    │                                                              │
  │                                    │ JobService.list_jobs(spec) ──────────────────────────────► │
  │                                    │                                                              │
  │  {spec, items, total}              │                                                              │
  │ ◄─────────────────────────────────│                                                              │
```

The model can only fill fields on the `JobFilterSpec` Pydantic model. Every field is an enum, a small typed value, or a bounded int. The spec maps directly to `JobService.list_jobs` kwargs — there's no SQL generation or freeform interpretation. This is what makes the feature injection-safe by construction.

If the LLM call fails (timeout, disabled, schema mismatch, API error), the endpoint returns 503 with `error_code: nl_query_unavailable`. See [ADR 0005](ADR/0005-llm-features-fail-open.md).

### 5. `POST /admin/digests/generate` — on-demand incident summary

```
Browser           API process             DB                                    Anthropic API
  │ POST /admin/digests/generate│                                                    │
  │ (optional {hours: N})       │                                                    │
  │ ───────────────────────────►│                                                    │
  │                              │ check enabled flag                                │
  │                              │ DigestRepository.window_stats                     │
  │                              │   SELECT status, COUNT(*) FROM jobs               │
  │                              │     WHERE tenant_id = ? AND created_at BETWEEN ?  │
  │                              │   GROUP BY status                                 │
  │                              │   (same for failed_by_type, error_message)        │
  │                              │                                                    │
  │                              │ _top_errors fingerprint (digit-normalize, top 5)   │
  │                              │                                                    │
  │                              │ incident_digest.generate_digest                   │
  │                              │   ────────────────────────────────────────────────► │
  │                              │                                             messages.parse with
  │                              │                                             output_format=IncidentDigest
  │                              │   ◄──────────────────────────────────────────────── │
  │                              │                                                    │
  │                              │ INSERT INTO incident_summaries                    │
  │                              │   (summary, highlights, model_used, usage)         │
  │                              │                                                    │
  │ 201 (digest)                 │                                                    │
  │ ◄───────────────────────────│                                                    │
```

The whole point of the aggregate-then-summarize pattern is to make the LLM call see *patterns* rather than raw rows. With 10K failures the LLM still receives just 5 fingerprints + 5 status counts. Cost stays predictable.

Empty windows skip the LLM call entirely. The endpoint returns `{summary: null, window_start, window_end}` so the UI can show "nothing to summarize" without a special error path.

---

## Concurrency model

The platform uses **all three Python concurrency models deliberately**, picked per workload:

### asyncio — I/O-bound work

The API process is fully asyncio. So is the entire worker process. Reasoning:

- HTTP I/O dominates the API path (DB / Redis / Kafka / Anthropic).
- The worker's eight Kafka consumers and nine loops are all I/O-bound.
- Switching between them on socket reads is what FastAPI + aiokafka were designed for.

Within the worker, every consumer runs as a separate asyncio task (`asyncio.create_task(c.run())`). The `worker_loop` orchestrates startup, graceful shutdown (cancels all tasks, waits for in-flight messages), and per-task error isolation (one consumer's failure doesn't kill others).

### threading — blocking work that can't be async

`app/workers/thread_adapters.py` hosts the `csv_upload` processor. Why threading instead of asyncio:

- CSV parsing libraries (pandas, csv) are synchronous blocking calls.
- Wrapping them with `asyncio.to_thread` is what we'd do for one call; the processor does many in sequence.
- Running the whole processor in a thread gives us back the async loop for everything else.

The worker calls thread-based processors via `await loop.run_in_executor(...)`. The default executor has 8 workers.

### multiprocessing — CPU-bound work

`app/workers/cpu_processors.py` hosts `doc_analysis` and `report_gen`. These are genuinely CPU-bound (PDF extraction, large in-memory transforms, big aggregations) and would block the asyncio loop for seconds.

The worker maintains a `ProcessPoolExecutor` and submits CPU-heavy jobs via `await loop.run_in_executor(pool, ...)`. Pickling overhead matters (a 5MB payload to a subprocess is expensive); we send only what's needed.

Two things about that pool are deliberate:

- **It is created lazily, on an explicit `spawn` context** (`_get_pool()`), never at import time. A pool built at import time would, on Linux, `fork` the worker process as it looked *then*; by the time a CPU job actually runs, the `csv-worker` thread pool and the Kafka/Redis clients are live, and forking a thread-laden process risks children that deadlock on a lock held by a thread the child does not have. `spawn` costs ~0.5–1s of child startup on the first CPU job and nothing after. macOS already defaults to `spawn`, which is why local development never surfaced this.
- **`BrokenProcessPool` resets the pool** (`_reset_pool()`) and re-raises into the dispatcher's normal retry path. A pool whose child was killed (OOM killer, host pressure) is permanently broken; without the reset, one dead child failed *every* subsequent CPU job until the ECS task restarted.

### Payload bounds — why processor knobs are capped

Job payloads are user-controlled, and their knobs translate directly into work in the worker process — which is the same process that serves the API and hosts every consumer loop. `endpoint_count=10**7` eagerly creates ten million asyncio tasks and OOM-kills all of it at once; a large `page_count`/`row_count` pegs the four-worker process pool long enough to push the Kafka dispatcher past `max_poll_interval_ms` and get the consumer group kicked.

So each knob is bounded twice:

| Job type | Knob | Bound |
| --- | --- | --- |
| `bulk_api_sync` | `endpoint_count` | 0–100 |
| `csv_upload` | `row_count` / `chunk_size` | 0–1,000,000 / 1–100,000 |
| `doc_analysis` | `page_count` | 0–1000 |
| `report_gen` | `row_count` / `group_count` | 0–1,000,000 / 1–1000 |

1. **At every creation surface** — `schemas/job.py` defines per-type payload models (`extra="allow"`, so `__traceparent` and arbitrary caller keys pass through) and `validate_processor_payload()` applies them from *both* `JobCreate` (POST /jobs) and `SagaStepRequest` (POST /sagas). The saga path builds jobs through `SagaService` → `JobService.create_job` and never constructs a `JobCreate`, so validating only the latter would leave POST /sagas as an open bypass.
2. **Inside the processors** — as clamps. Replays (`JobService.replay_job`) republish the *stored* payload without revalidating, so rows written before the bounds existed still reach a processor. The clamps are load-bearing, not belt-and-braces.

The `chunk_size` and `group_count` floors are `1`, not `0`: both are divisors, and zero was a `ZeroDivisionError` and a silently-empty report respectively.

These caps are conservative and deliberately defensive — the simulated processors have no real resource envelope to derive limits from. Real processors should replace them with limits derived from their actual memory and CPU cost.

### When to add a new processor

The decision is concrete:
- Is the work **I/O-dominated** (most time spent waiting on a network call)? Use asyncio. Add to `async_tasks.py`.
- Is the work **blocking but I/O-bound** (synchronous library, no CPU heat)? Use threading. Add to `thread_adapters.py`.
- Is the work **CPU-bound** (compute > I/O)? Use multiprocessing. Add to `cpu_processors.py`.

Register the new processor in `_PROCESSORS` in `dispatcher.py`.

---

## The worker loop — what runs where

`worker_loop` in `app/workers/dispatcher.py` is the entry point. On startup it:

1. Starts eight Kafka consumers (each with its own consumer group, started best-effort — one's failure doesn't kill the others).
2. Validates the dispatcher consumer specifically — if that one fails to start, the worker exits since no jobs can run.
3. Spawns the nine background loops as asyncio tasks.

The seventeen tasks run concurrently. They share:

- The same `session_factory` (so they share connection pool semantics; each task acquires/releases per transaction).
- The same Redis client.
- The same producer (Kafka — module-level singleton).

### Per-task responsibilities

| Task | Source | Sink | Failure isolation |
|---|---|---|---|
| `JobDispatcherConsumer` | `job.submitted` Kafka | spawns `_run_job` per message | Per-job — one bad job doesn't poison the group |
| `AuditConsumer` | All lifecycle topics | `audit_logs` rows | Per-row — caught at message level |
| `SseConsumer` | All lifecycle topics | Redis Pub/Sub | Per-message — drops on broker error |
| `EventLogConsumer` | All lifecycle topics | `job_events` rows | Per-row — `IntegrityError` (dedup) is silently swallowed |
| `ReadModelProjector` | All lifecycle topics | Redis sets | Per-message — drops events without `tenant_id` |
| `DependencyResolver` | `job.completed` | `jobs` updates + outbox rows | Per-promotion — failure to promote one child doesn't affect siblings |
| `SagaCoordinator` | `job.completed`, `job.dlq` | `sagas` updates + outbox rows | Per-saga — one saga's failure doesn't affect others |
| `LlmTriageConsumer` | `job.dlq` | `job_triages` rows | Per-job — LLM failure is logged and skipped |
| `_outbox_relay_loop` | `outbox_events` table | Kafka via `publish_raw` | Per-row — schema failures mark row failed, others retry next tick |
| `_promote_delayed_loop` | Redis `delayed_queue` zset | outbox row | Per-item — a failed job is re-pushed onto the zset; the rest of the batch still promotes |
| `_requeue_stale_pending_loop` | `jobs` rows `PENDING` for >300s with no `delayed_queue` timer | outbox row | Per-tick — exception logged, loop continues |
| `_stale_running_sweep_loop` | `jobs` rows `RUNNING` for longer than `stale_running_threshold_seconds` and not in `dispatcher.in_flight_job_ids` | `dead_letter` status + `job.dead_letter` audit row + `job.dlq` outbox row ([ADR 0019](ADR/0019-stale-running-recovery-sweep.md)) | Per-job — each recovery is its own transaction; one failure leaves that row `RUNNING` for the next pass |
| `_metrics_loop` | Dispatcher consumer + Redis | CloudWatch gauges | Per-tick — exception logged |
| `_digest_loop` | DB + Anthropic API | `incident_summaries` rows | Per-tenant — one tenant's API failure doesn't stop the batch |

The "failure isolation" column is the most operationally important one — it documents what happens when something in the loop fails. Every loop catches at the top level and continues; nothing in the worker process is allowed to be fragile in a way that takes down the whole process.

---

## Auth & tenant matrix

Three role tiers, with `is_platform_admin` as an additive cross-tenant flag:

| Role / surface | `user` | `support` | `admin` | `admin + is_platform_admin` |
|---|---|---|---|---|
| Create own jobs | ✓ | ✓ | ✓ | ✓ |
| See own jobs | ✓ | ✓ | ✓ | ✓ |
| See own tenant's jobs (other users') | — | ✓ | ✓ | ✓ |
| See sibling tenants' jobs | — | — | — | ✓ (via `?tenant_id=`) |
| Admin tabs (overview, jobs, dlq, runbooks, audit) | — | ✓ | ✓ | ✓ |
| List users in own tenant | — | — | ✓ | ✓ |
| List ALL tenants | — | — | — | ✓ |
| Create tenant | — | — | — | ✓ |
| Patch tenant limits | — | — | — | ✓ |
| Replay a job | — | ✓ | ✓ | ✓ |
| Mark incident resolved | — | ✓ | ✓ | ✓ |
| Generate incident digest | — | — | ✓ | ✓ |
| Run NL admin query | — | ✓ | ✓ | ✓ |
| Get triage analysis for a job | — | ✓ | ✓ | ✓ |
| Get cross-tenant digest by ID | — | — | — (403) | ✓ |

The "own tenant" rows are enforced by:
- **Application layer:** `JobService.list_jobs` filters by `requesting_user_id` when the role is `user`.
- **DB layer (RLS):** `app.tenant_id` is set by `get_current_user`, and the RLS policy filters every query against it.

The platform-admin override (`?tenant_id=X`) re-issues `set_config('app.tenant_id', X, true)` so the RLS policy permits the cross-tenant query.

The last two rows of the matrix — create tenant, patch tenant limits — each write an audit row (`tenant.created`, `tenant.limits_updated`) belonging to the tenant being acted on, which is normally *not* the admin's own. Since `audit_logs` carries a WITH CHECK on `tenant_id` under FORCE RLS, `backend/app/api/admin.py` retargets `app.tenant_id` at the subject tenant for the INSERT with the same `set_config` statement, then hands it back. No policy is relaxed: the row is written under the tenant it records.

**Database roles:** the API, the worker loops, and the MCP process all share one engine (`backend/app/dependencies.py`) and connect as the non-owner **`incident_app`** role — DML only, no DDL, and no UPDATE/DELETE on `audit_logs` (tampering raises `insufficient_privilege`). Migrations run as the owner (the RDS master) on the separate `ALEMBIC_DATABASE_URL`; a boot-time probe (`app/core/rls_check.assert_rls_posture`) hard-fails a production process whose connection would silently bypass RLS.

See [ADR 0003](ADR/0003-rls-as-defense-in-depth.md) for the RLS design and [ADR 0015](ADR/0015-force-rls-and-nonowner-app-role.md) for FORCE RLS and the role split.

---

## Failure mode catalog

What happens when a component dies, in priority order.

### Postgres

**Symptom:** every API request returns 500. Worker stalls (can't commit jobs).
**Detection:** ALB 5xx alarm fires. ECS task health check fails (the API task pings DB on startup).
**Recovery:** RDS auto-failover (Multi-AZ); ~60s. New primary; connections drop and reconnect.
**Data loss:** none — RDS automated backups + Multi-AZ.
**Runbook:** `runbooks/rb-rds-cpu-high.yaml` covers degradation; a full outage page → on-call → AWS console.

### Redis

**Symptom:** API still works but degraded. Rate limits fail open (everyone gets through). Backpressure check fails open (no rejections). SSE streams freeze (no updates). Admin overview shows stale numbers. Job cache misses on every request.
**Detection:** `redis-memory-low` alarm at 80%; ALB latency uptick if cache miss rate balloons.
**Recovery:** ElastiCache failover (Multi-AZ); ~60s.
**Data loss:** depends on persistence. With AOF every-second (production), <1s of writes. Delayed queue entries from that window are gone — those jobs are stuck in `failed` until manually replayed.
**Runbook:** `runbooks/rb-redis-memory-low.yaml`.

### Kafka

**No production broker exists.** `infra/` provisions none, and the ECS deploy job is gated off behind the `ENABLE_ECS_DEPLOY` repository variable — see [ADR 0018](ADR/0018-production-kafka-posture.md). What follows describes broker loss against the broker the platform actually runs on (Redpanda in `docker-compose.yml`), and against whatever production broker is eventually chosen; the client-side behaviour is a property of this codebase, not of any particular cluster. Broker-side recovery characteristics (replication factor, multi-AZ failover, failover time) belong to the broker and cannot be stated here until one is provisioned.

**Symptom:** outbox table grows; the relay logs publish failures. Direct-publish (progress) silently drops. Consumer groups stop receiving (they were already stopped because the broker is unreachable).
**Detection:** consumer lag alarm; queue depth alarm; task can't reach broker (logged).
**Recovery (client side):** producers retry on reconnect; consumers resume from the last committed offset. This also covers the boot-time case: if an API/worker task starts while the broker is down, `start_producer()` fails, the producer singleton stays unset, and the publish paths lazily restart it (one attempt per 5s) — the outbox relay's next tick after the broker returns brings the producer up and drains the backlog. No redeploy needed.
**Data loss:** no. Outbox keeps unpublished rows.
**Operational shape:** the more time spent before recovery, the more outbox rows pile up; the relay catches up over the next minute or two after the broker is back.
**Degenerate case worth naming:** a deployed stack with `KAFKA_BOOTSTRAP_SERVERS` unset is indistinguishable, from the API's point of view, from a permanently-down broker — jobs are accepted, outbox rows accumulate, and nothing ever executes. That is today's state on an ungated AWS deploy, and it is the reason the deploy is gated rather than merely documented.

### Anthropic API

**Symptom:** LLM features degrade. DLQ triages stop appearing. NL queries return 503. Retry policy falls back to deterministic. Digests stall.
**Detection:** structured log warnings; per-feature usage in admin UI drops.
**Recovery:** no action required — the next successful call resumes normal operation.
**Data loss:** none — every LLM feature has a non-LLM fallback (see [ADR 0005](ADR/0005-llm-features-fail-open.md)).

### Worker process

**Symptom:** no new job processing; no events flowing. The API still accepts new jobs (they queue in the outbox).
**Detection:** `backend-tasks-low` alarm (if ECS service replica count drops); queue-depth alarm climbs.
**Recovery:** ECS auto-restarts the task. On restart it rebuilds consumer-group state and resumes from committed offsets.
**Data loss:** in-flight jobs may double-execute if they had side effects before the crash. Mitigated by idempotency keys.
**Stranded jobs:** offsets are committed at dispatch time, so jobs the dead worker was executing stay `RUNNING` with nothing left to redeliver them. Once a worker is back, `_stale_running_sweep_loop` dead-letters them within `stale_running_threshold_seconds` (default 900s) plus one 60s pass — they land in the DLQ with `reason: worker_crash_recovery` rather than being re-published, because a partially-executed job is unsafe to re-run ([ADR 0019](ADR/0019-stale-running-recovery-sweep.md)).
**Runbook:** `runbooks/rb-ecs-tasks-low.yaml`.

### API process

**Symptom:** ALB returns 502. New requests fail.
**Detection:** ALB target unhealthy.
**Recovery:** ECS restart. Other API replicas continue serving (we run ≥2 in prod).
**Data loss:** in-flight HTTP requests are lost — clients retry.

---

## SLOs and error budgets

Two SLOs defined in `app/services/slo.py`, both computed live from the `jobs` table:

| SLO | Target | Window | Burn-rate alarm |
|---|---|---|---|
| `job_completion_rate` | 99% of jobs complete (vs. dead-letter) | rolling 24h | 14.4× fast-burn over 1h → alarm |
| `job_dispatch_latency` | 95% dispatched within 30s (pending → running) | rolling 24h | 14.4× fast-burn over 1h → alarm |

`GET /admin/slos` returns current state + budget remaining + burn rate. Each has a corresponding runbook (`runbooks/rb-slo-*.yaml`) referenced from the CloudWatch alarm description.

The SLO state is also shown on the admin Overview tab — operators see the current burn rate without leaving the UI.

---

## Tracing

OpenTelemetry auto-instrumentation enabled on FastAPI, SQLAlchemy, and Redis. Traces export to OTLP (X-Ray in prod, Jaeger locally).

**Cross-process trace propagation:** when the API creates a job, it injects the current OTel context as `__traceparent` into the job payload. The worker's `_run_job` extracts it and continues the trace as a child span. End-to-end visibility: browser → API → worker → DB → external API.

The trace ID is logged with every structured log entry (via `trace_id_var` contextvar) and stored on the job row. The admin UI lets you filter by trace ID.

---

## Cost model (LLM features)

Approximate per-call costs at current Opus 4.7 pricing ($5/M input, $25/M output) and our prompt sizes:

| Feature | Typical input | Cached input | Output | Per-call cost | Per-month at expected volume |
|---|---|---|---|---|---|
| DLQ triage | ~2 KB user msg | ~1.5 KB system (cached) | ~400 tokens | ~$0.012 | ~$3 (assuming 100 DLQs/day) |
| Retry policy | ~500 B | ~1.2 KB (cached) | ~150 tokens | ~$0.005 | ~$15 (assuming 100 consults/day) |
| NL admin query | ~300 B | ~1.5 KB (cached) | ~200 tokens | ~$0.006 | ~$5 (assuming 25 queries/day) |
| Incident digest | ~3 KB aggregates | ~1 KB (cached) | ~600 tokens | ~$0.018 | ~$2 (1 per tenant per day, 3 tenants) |

Total Phase-10 LLM spend in a representative dev setup: <$30/month. Cost telemetry (cache hit rates, token counts) is persisted on every record for visibility.

---

## Pointers

- `backend/app/main.py` — API process entry point
- `backend/app/workers/dispatcher.py` — `worker_loop` (the worker process entry point)
- `backend/app/dependencies.py` — shared FastAPI dependencies
- `backend/app/api/*.py` — HTTP routers
- `backend/app/services/*.py` — business logic
- `backend/app/repositories/*.py` — DB access
- `backend/app/models/*.py` — SQLAlchemy models
- `backend/app/workers/*.py` — workers, consumers, processors
