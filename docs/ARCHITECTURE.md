# Architecture

The runtime topology, request lifecycles, concurrency model, and failure modes of the incident platform. Read this when you're onboarding, designing a new feature that crosses subsystems, or debugging something you don't fully understand.

For per-component reference, see [`docs/DATA_MODEL.md`](DATA_MODEL.md), [`docs/KAFKA.md`](KAFKA.md), [`docs/REDIS.md`](REDIS.md). For specific design decisions, see [`docs/ADR/`](ADR/).

---

## Runtime topology

The platform runs as **three logical processes**:

1. **API process** — FastAPI behind an ALB. Serves `/api/v1/*`. One ECS task with autoscaling target on CPU.
2. **Worker** — `worker_loop` from `app/workers/dispatcher.py`. Hosts eight Kafka consumer groups + nine background loops. Logically separate, but **not a separate deployable yet**: it runs as a supervised task inside every API process (see "More than one process runs this" below; the dedicated worker deployable and queue-depth autoscaling are Phase 8 items). That is why worker liveness is reported on the API's own `/api/v1/health` — a dead worker is a degraded *API task*, and the probe that governs restarts has to be able to say so ([ADR 0009](ADR/0009-consumer-lifecycle-and-supervision.md), 2026-08-30 amendment).
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
                │  10 loops:       │
                │   outbox-relay   │
                │   promote-delayed│
                │   promote-replay │
                │   resume-waiting │
                │   stale-pending  │
                │   stale-running  │
                │   lease-renewal  │
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
4. **Per-tenant rate limit** (Redis fixed window — 2x the cap is reachable across a boundary; see [`docs/REDIS.md`](REDIS.md#rate-limits-rate))
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

The browser first POSTs for a **stream token**: native `EventSource` cannot set an `Authorization` header, so the stream GET authenticates with a short-lived (60s), single-purpose token in its query string instead of the primary JWT ([ADR 0014](ADR/0014-sse-stream-token-transport.md)). The mint endpoint is where authorization happens — it loads the job tenant-scoped (404 cross-tenant, 403 non-owner) and binds the job id into the token, so a token minted for job X cannot open job Y's stream. The GET validates that token, joins the process-wide fan-out broker for that job's Redis Pub/Sub channel, and forwards every received message as an SSE frame. The broker holds **one** Pub/Sub connection for the whole process on a Redis pool dedicated to streaming, so viewers no longer consume a connection each out of the pool the request path and worker loops share — see [REDIS.md](REDIS.md#connection-budget--why-streaming-has-its-own-pool-and-its-own-cap). Concurrent streams are capped per process (`SSE_MAX_CONCURRENT_STREAMS`); past the cap the GET answers 503 with `Retry-After` rather than competing for a finite resource, and idle/maximum-duration timeouts reclaim slots from parked tabs. On reconnect the client mints a fresh token (the previous one has expired by then). The worker publishes progress directly to Kafka (the only direct-publish path, see [KAFKA.md](KAFKA.md)); the `sse-broadcaster` consumer republishes to Redis Pub/Sub.

If Redis dies mid-stream, the API logs the error and closes the affected streams — the browser's `EventSource` reconnects on its own. It is never a 500: the job's durable state is in Postgres, and a reconnecting client that finds the job already finished is short-circuited off the `jobs` row.

### 3. `POST /sagas` — create a multi-step workflow

```
Browser                           API process                              DB
  │  POST /sagas                     │                                       │
  │  {name, steps: [...]}            │                                       │
  │ ───────────────────────────────► │                                       │
  │                                  │  rate limit (shared jobs:create)      │
  │                                  │  validate steps (<= MAX_SAGA_STEPS)   │
  │                                  │  check_job_admission(N=len(steps))    │
  │                                  │    backpressure + tenant quota        │
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

Admission control is identical to `POST /jobs` and runs through the same guard (`app/utils/admission.py`), because this endpoint creates `jobs` rows exactly as that one does — it just creates several. The saga is weighed as its **step count**: `_check_monthly_quota` counts every `Job` row, so saga steps already consumed the cap that blocks `POST /jobs` while this endpoint was never blocked by it, which made the per-tenant billing cap unenforceable rather than merely leaky. Checking the batch up front also means a saga is refused whole instead of committing part of its chain and meeting the cap mid-loop. `steps` is bounded by `MAX_SAGA_STEPS` so one request cannot create an unbounded number of rows.

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
| `csv_upload` | `row_count` / `chunk_size` | 0–1,000,000 / 1–100,000, **and** `ceil(row_count / chunk_size)` ≤ 10,000 chunks |
| `doc_analysis` | `page_count` | 0–1000 |
| `report_gen` | `row_count` / `group_count` | 0–1,000,000 / 1–1000 |

1. **At every creation surface** — `schemas/job.py` defines per-type payload models (`extra="allow"`, so `__traceparent` and arbitrary caller keys pass through) and `validate_processor_payload()` applies them from *both* `JobCreate` (POST /jobs) and `SagaStepRequest` (POST /sagas). The saga path builds jobs through `SagaService` → `JobService.create_job` and never constructs a `JobCreate`, so validating only the latter would leave POST /sagas as an open bypass. The same reasoning applies to admission control, which is why the rate limit, backpressure check and tenant quota live behind one shared guard both endpoints call rather than three calls each endpoint has to remember (`app/utils/admission.py`).
2. **Inside the processors** — as clamps. Replays (`JobService.replay_job`) republish the *stored* payload without revalidating, so rows written before the bounds existed still reach a processor. The clamps are load-bearing, not belt-and-braces.

The `chunk_size` and `group_count` floors are `1`, not `0`: both are divisors, and zero was a `ZeroDivisionError` and a silently-empty report respectively.

Bounding knobs one at a time is not enough when the cost is a *relationship* between two of them. `process_csv_upload` iterates `range(0, row_count, chunk_size)`, so its cost is the chunk count — and `{row_count: 1_000_000, chunk_size: 1}` satisfied both field bounds while buying ~22 hours of execution for one HTTP request. Hence the third `csv_upload` bound above, on the quotient (**not** the product, which is smallest for exactly that shape and largest for the cheapest legitimate one). See [ADR 0021](ADR/0021-bounded-execution-and-non-blocking-dispatch.md).

Payload bounds can only ever cover processors whose cost model the schema knows. The backstop for the rest is `job_execution_timeout_seconds` (default 600s): a processor that overruns it is cancelled and its job dead-letters with `dead_lettered_by: execution_timeout`, releasing the concurrency slot. Note the deadline frees the *slot*, not necessarily the worker — a processor cancelled inside `run_in_executor` leaves its thread or process running to completion, since Python cannot preempt either.

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

### More than one process runs this

`worker_loop` is started from the API's own lifespan (`app/main.py`), so every API replica hosts a
full set of these tasks — two of everything for the overlap window of every rolling deploy, and
permanently more once Phase 8 scales the API out. Two loops are singular by nature and are guarded
accordingly:

- **`_outbox_relay_loop` is leader-gated.** Each tick runs only if the process wins
  `pg_try_advisory_lock` on a constant key, held across the tick's three transactions on one pinned
  connection; the loser skips and re-probes a second later. Ungated, every deploy republished the
  whole unpublished backlog — duplicate lifecycle events into audit, `job_events`, and the SSE
  bridge. See [ADR 0020](ADR/0020-outbox-relay-single-writer.md).
- **The sweeps (`_resume_unblocked_waiting_loop`, `_requeue_stale_pending_loop`) are deliberately
  ungated.** Both promote through a compare-and-set (`promote_waiting_to_pending`,
  `claim_for_running`), so a second sweeper loses the CAS and emits nothing extra. Concurrent
  sweeps are wasted scans, not duplicate work — and the CAS also holds against Kafka redelivery,
  which no leader gate would see.

### Per-task responsibilities

| Task | Source | Sink | Failure isolation |
|---|---|---|---|
| `JobDispatcherConsumer` | `job.submitted` Kafka | spawns `_run_job` per message; the concurrency slot is taken *inside* the spawned task so a saturated worker keeps polling ([ADR 0021](ADR/0021-bounded-execution-and-non-blocking-dispatch.md)) | Per-job — one bad job doesn't poison the group |
| `AuditConsumer` | All lifecycle topics | `audit_logs` rows | Per-row — caught at message level |
| `SseConsumer` | All lifecycle topics | Redis Pub/Sub | Per-message — drops on broker error |
| `EventLogConsumer` | All lifecycle topics | `job_events` rows | Per-row — `IntegrityError` (dedup) is silently swallowed |
| `ReadModelProjector` | All lifecycle topics | Redis sets | Per-message — drops events without `tenant_id` |
| `DependencyResolver` | `job.completed` | `jobs` updates + outbox rows | Per-promotion — failure to promote one child doesn't affect siblings |
| `SagaCoordinator` | `job.completed`, `job.dlq` | `sagas` updates + outbox rows | Per-saga — one saga's failure doesn't affect others |
| `LlmTriageConsumer` | `job.dlq` | `job_triages` rows | Per-job — an LLM failure is logged, no row is written, and the offset is committed ([ADR 0005](ADR/0005-llm-features-fail-open.md)). Only 429/5xx re-raise for redelivery; anything deterministic would otherwise loop on a billed call |
| `_outbox_relay_loop` | `outbox_events` table | Kafka via `publish_raw` | Per-row — schema failures mark row failed, others retry next tick. Leader-gated: only one process relays at a time ([ADR 0020](ADR/0020-outbox-relay-single-writer.md)) |
| `_promote_delayed_loop` | Redis `delayed_queue` zset | outbox row | Per-item — a failed job is re-pushed onto the zset; the rest of the batch still promotes |
| `_requeue_stale_pending_loop` | `jobs` rows `PENDING` for >300s with no `delayed_queue` timer and no `requeued_at` inside the same window ([ADR 0023](ADR/0023-dispatcher-sweep-ownership.md)) | outbox row + `requeued_at` stamp, one transaction | Per-tick — exception logged, loop continues |
| `_stale_running_sweep_loop` | `jobs` rows `RUNNING` for longer than `stale_running_threshold_seconds` whose lease (`heartbeat_at`) has lapsed, also skipping `dispatcher.in_flight_job_ids` until they are a further `_IN_FLIGHT_EXCLUSION_GRACE_SECONDS` stale ([ADR 0021](ADR/0021-bounded-execution-and-non-blocking-dispatch.md), [ADR 0023](ADR/0023-dispatcher-sweep-ownership.md)) | `dead_letter` status + `job.dead_letter` audit row + `job.dlq` outbox row ([ADR 0019](ADR/0019-stale-running-recovery-sweep.md)) | Per-job — each recovery is its own transaction and compare-and-sets on the observed `started_at`/`heartbeat_at`; one failure or refusal leaves that row `RUNNING` for the next pass |
| `_renew_running_leases_loop` | `dispatcher.in_flight_job_ids` | `heartbeat_at` on this worker's `RUNNING` rows, every 20s, until the job is `stale_running_threshold_seconds + _IN_FLIGHT_EXCLUSION_GRACE_SECONDS` old ([ADR 0023](ADR/0023-dispatcher-sweep-ownership.md)) | Per-tick — exception logged; the TTL spans six intervals, and a sustained failure degrades to age-plus-local-set, not to anything worse |
| `_metrics_loop` | Dispatcher consumer + Redis | CloudWatch gauges | Per-tick — exception logged |
| `_digest_loop` | DB + Anthropic API | `incident_summaries` rows | Per-tenant — one tenant's API failure doesn't stop the batch |

The "failure isolation" column is the most operationally important one — it documents what happens when something in the loop fails. Every loop catches at the top level and continues; nothing in the worker process is allowed to be fragile in a way that takes down the whole process.

Two paths still escape those top-level guards — a loop's deferred imports, which sit *before* its `while True`, and a `CancelledError` reaching `_supervise_consumer`, which re-raises by design. Both end `worker_loop` itself, so the task is supervised in turn by `app/workers/supervisor.py`: the death is logged, the worker is restarted (immediately, then 1s → 30s), and its liveness is reported on `GET /api/v1/health`. `_promote_delayed_loop` also calls `worker_tick()` on every pass, which extends that liveness from "the task exists" to "its loops are turning". This matters more than the per-loop isolation, because `ConsumerLag` — the metric both backlog alarms read — is emitted by `_metrics_loop`, a loop inside the worker, and is absent rather than high when the worker is dead. Without the health signal, worker death silences the metrics that would detect it.

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
**Detection:** `GET /api/v1/health` reports `"worker": "error"` and 503, with a `worker_detail` giving the state, restart count and last error. Do **not** wait on the backlog alarm for this one: `ConsumerLag` is emitted by a loop inside the worker and is not emitted at all when lag is unknown, so a dead worker makes the datapoints *absent* and the alarm reads missing data as `notBreaching` — `infra/cloudwatch.tf` hands this case to worker supervision explicitly. `backend-tasks-low` only fires if the whole ECS task drops, which a dead worker task inside a live API process does not do.
**Recovery:** the supervisor restarts the worker in-process first (immediately, then 1s → 30s); if it cannot stay up, the 503 fails the ECS container check and the ALB target, and the task is recycled after 3 × 30s. On restart it rebuilds consumer-group state and resumes from committed offsets.
**Data loss:** in-flight jobs may double-execute if they had side effects before the crash. Mitigated by idempotency keys.
**Stranded jobs:** offsets are committed at dispatch time, so jobs the dead worker was executing stay `RUNNING` with nothing left to redeliver them. The dead worker also stops renewing their lease, so `heartbeat_at` lapses within `_RUNNING_LEASE_TTL_SECONDS` (120s) and any surviving replica can tell those rows apart from the ones it is executing itself. `_stale_running_sweep_loop` dead-letters them within `stale_running_threshold_seconds` (default 900s) plus one 60s pass — they land in the DLQ with `reason: worker_crash_recovery` rather than being re-published, because a partially-executed job is unsafe to re-run ([ADR 0019](ADR/0019-stale-running-recovery-sweep.md), [ADR 0023](ADR/0023-dispatcher-sweep-ownership.md)).
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

## Cost model (CloudWatch custom metrics)

CloudWatch bills a custom metric per distinct **dimension combination**, and
throttles `PutMetricData` on **call rate** — two independent limits that need
two independent guards. Both live in `backend/app/core/metrics.py`.

### Cardinality — what a dimension may contain

`RequestLatency` used to be dimensioned on `request.url.path`, the raw URL. Every
job, tenant and user id in a URL minted its own billable metric, and since
`BaseHTTPMiddleware` wraps the router, 404s on arbitrary URLs were measured too —
so anyone who could reach the load balancer could mint metrics at will. The
dimension is now the route's templated path (`/jobs/{job_id}`), with the constant
`unmatched` for anything that matched no route.

Two guards keep it that way, so a future caller cannot reintroduce the problem:

| Guard | Applies to | Effect |
|---|---|---|
| Allow-list (`register_dimension_values`) | dimensions a caller declared | anything outside the declared set becomes `other` |
| Hard cap (`MAX_DIMENSION_VALUES`, 100) | dimensions nobody declared | the first 100 distinct values pass; later new values become `other` |

The API and MCP processes register their templated route table at startup, which
is exactly the set of `Path` values that can legitimately occur. Current bound:
**41 `Path` values** (40 routes + `unmatched`), plus `other`. `StatusCode` has no
declared allow-list and so falls to the hard cap — bounded at 100, realistically
about a dozen. Worst case is therefore ~4,200 combinations and typically far
fewer, against a raw-path version that was bounded only by the number of rows in
the database.

Note the `Path` value is the route's *declared* path, which for anything mounted
via `include_router(prefix=...)` is router-relative — `/jobs/{job_id}`, not
`/api/v1/jobs/{job_id}`. Recovering the full path needs FastAPI's private
`_IncludedRouter.include_context.prefix`, which production code deliberately does
not read. The short form is safe only while it is unique across the app;
`test_route_labels_are_unique_per_route` fails the build if two routers ever
declare the same relative path.

### Call rate — how datums leave the process

`emit_count` / `emit_gauge` do **no I/O**. They sanitise dimensions and
`put_nowait` onto a bounded queue (`QUEUE_MAXSIZE`, 10,000); a background task
started from the app lifespan drains it every `FLUSH_INTERVAL_SECONDS` (60s),
folds the window into one `StatisticSet` per distinct (metric, unit, dimensions),
and makes **one** `PutMetricData` call — chunked only if the aggregated datums
exceed `MAX_DATUMS_PER_CALL`. A thousand requests against one route cost one
datum, and CloudWatch still reconstructs Average / Sum / Min / Max / SampleCount.

Overflow drops and counts rather than applying backpressure: blocking a request
in order to record how fast that request was would be self-defeating. Shutdown
flushes the final window so a clean stop does not discard up to 60s of data.

This replaced a per-request `asyncio.create_task` around a blocking boto3 call on
the default executor, whose retained task-handle set grew with in-flight emits —
a slow CloudWatch was a memory leak on the hot path, on top of the worst possible
call-rate ratio against a limit that counts calls.

**Why `RequestLatency` is kept at all:** nothing consumes it today — no alarm in
`infra/` references it. It stays because per-route latency is the one thing the
ALB's aggregate `TargetResponseTime` cannot give you, and it is now bounded and
cheap enough to be worth having before an alarm needs it.

---

## Pointers

- `backend/app/main.py` — API process entry point
- `backend/app/workers/dispatcher.py` — `worker_loop` (the worker entry point)
- `backend/app/workers/supervisor.py` — supervises that task; the liveness `/api/v1/health` reports
- `backend/app/dependencies.py` — shared FastAPI dependencies
- `backend/app/api/*.py` — HTTP routers
- `backend/app/services/*.py` — business logic
- `backend/app/repositories/*.py` — DB access
- `backend/app/models/*.py` — SQLAlchemy models
- `backend/app/workers/*.py` — workers, consumers, processors
