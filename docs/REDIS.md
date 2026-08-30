# Redis — key catalog

Redis sits on three hot paths: rate limiting, the SSE progress bridge, and the CQRS read-model. It also caches the consumer lag for backpressure and stores delayed-retry timers.

This doc catalogs every key pattern in use: what writes it, what reads it, its TTL, and what happens if Redis goes away.

For the broader architectural context, see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Key catalog

| Key pattern | Type | Writer | Reader | TTL | Eviction-safe? |
|---|---|---|---|---|---|
| `rate:{client}:{window_start}` | string (counter) | `rate_limit.py` `_check` | `rate_limit.py` `_check` | `window * 2` (e.g. 120s) | Yes — fails open |
| `rate:tenant:{tenant_id}:{window_start}` | string (counter) | `quota.py` `_check_tenant_rate` | `quota.py` `_check_tenant_rate` | 120s | Yes — fails open |
| `job:progress:{job_id}` | Pub/Sub channel (no persisted key) | `progress.py` `publish` | `streaming.py` SSE endpoint via `subscribe` | N/A (Pub/Sub is fire-and-forget) | No — listeners must be connected |
| `job:progress:last:{job_id}` | string (last `ProgressEvent` as JSON) | `progress.py` `publish` (fed by `sse_consumer`) | `progress.py` `subscribe` / `read_last_event`, for the SSE endpoint | 1h (`LAST_EVENT_TTL_SECONDS`) | Yes — degrades to the pre-snapshot stream; the `jobs` row is the fallback |
| `jobs:tenant:{tenant_id}:status:{status}` | set of job_ids | `read_model.py` `_move` | `admin.py` `system_stats` via `read_global_stats` | None | No — read model goes stale |
| `jobs:user:{user_id}:status:{status}` | set of job_ids | `read_model.py` `_move` | `admin.py` `user_stats` via `read_user_stats` | None | No — read model goes stale |
| `cache:job:{tenant_id}:{job_id}` | JSON string | `cache.py` `JobCache.set` | `cache.py` `JobCache.get` | 10s | Yes — cache miss = DB read |
| `kafka:consumer_lag:worker-dispatcher` | string (int) | `dispatcher.py` `_metrics_loop` | `backpressure.py` `check_backpressure` | 90s | Yes — falls back to "no backpressure" |
| `priority_queue` (sorted set) | sorted set of job_ids by priority score | `queue.py` `push` | `queue.py` `pop` | None | Critical — see below |
| `delayed_queue` (sorted set) | sorted set of job_ids by ready-at timestamp | `queue.py` `push_delayed` | `queue.py` `pop_ready_delayed` | None | Critical — see below |

### Agent-era keys (chaos, Tier-1 action residue, eval fixtures)

These namespaces are written by the incident-commander MCP tools and the eval tooling, not by the platform's own request/worker paths. The `chaos:*` tools are registered only when `CHAOS_ENABLED=true` (ADR 0008); the rows marked **eval-only** are seeded by `scripts/seed_eval_fixtures.py` and never appear outside an eval stack. `scripts/reset_eval_state.py` clears residue between scenarios: a `chaos:*` scan-and-delete, the `jobs:dlq_replay_delayed` ZSET, every `dag:paused:*` flag, and a re-seed of the fixture keys.

Any key under `cache:` / `jobs:cache:` / `kafka:consumer_lag:` / `read_model:` — the same prefixes `invalidate_cache_key` may delete — is additionally observable through the `get_cache_key_info` MCP read tool (`telemetry:read`), which reports existence, TTL, type and size but never the value. That is the read half of the stale-cache remediation loop: check the key before invalidating, confirm the delete after.

| Key pattern | Type | Writer | Reader | TTL | Eviction-safe? | Cleared by eval reset? |
|---|---|---|---|---|---|---|
| `chaos:kill:{group}` | string flag | `kill_consumer` chaos tool (`kill_key_for`) | `kafka_consumer.py` `_check_chaos_kill` — top of every poll; consumer shuts down while set | `ttl_seconds` (default 300s) | Yes — chaos ends early | Yes (`chaos:*` scan) |
| `chaos:latency:{group}` | string (ms) | `inject_latency` chaos tool (`latency_key_for`) | `kafka_consumer.py` `_check_chaos_latency` — sleep before each poll | `ttl_seconds` (default 300s) | Yes — chaos ends early | Yes (`chaos:*` scan) |
| `chaos:bad_deploy` | string (label) | `bad_deploy` chaos tool (`BAD_DEPLOY_KEY`) | Nothing yet — the fired `critical` alert is the observable signal | `ttl_seconds` (default 600s) | Yes | Yes (`chaos:*` scan) |
| `chaos:sat:{run_id}:{i}` | string (filler) | `saturate_redis` chaos tool (default 1000 keys × 1 KiB) | Nobody — exists purely for memory pressure | `ttl_seconds` (default 60s) | Yes — eviction is the point | Yes (`chaos:*` scan; usually already expired) |
| `dag:paused:{root_job_id}` | string flag | `pause_dag` action tool (`pause_key_for` in `app/utils/dag_pause.py`) | Every dispatch path via `find_blocking_pause` — resolver promotion, resume sweep, delayed-retry promotion, `replay_job`, the scheduled DLQ-replay loop and `_run_job`'s pre-claim re-check, plus a create-time WAITING hold (ADR 0011 + its 2026-08-09 amendment); reported by `get_dag_state` | `ttl_seconds` (default 600s) | Yes — pause fails open, DAG resumes | Yes (`dag:paused:*` scan) |
| `jobs:dlq_replay_delayed` | sorted set of job_ids by fire-at time | `replay_dlq_by_ids` / `replay_dlq_by_category` (`delay_seconds` path) | `dlq_replay_scheduler.py` via the worker's `_promote_dlq_replay_loop` | None | No — pending delayed replays disappear silently | Yes (ZSET deleted) |
| `cache:jobs:worker-dispatcher:hot_set` (**eval-only**) | JSON array of seeded DLQ job ids | `seed_eval_fixtures.py` `_seed_hot_set`; also the `create_stale_cache` chaos tool | The stale-cache scenario observes it via `get_cache_key_info`, then deletes it via `invalidate_cache_key` | 24h (seed) / `ttl_seconds` default 600s (tool) | Yes — re-seeded | Re-populated by reset |
| `kafka:consumer_lag:{group}` (**eval-only** except `worker-dispatcher`) | string (int) | Metrics loop writes only `worker-dispatcher` (row above); `seed_eval_fixtures.py` writes the 7 synthetic groups (`billing-consumer` … `healthy-consumer`) | `get_consumer_lag` MCP tool (any group); `check_backpressure` reads only `worker-dispatcher` | 24h on seeded groups (90s on the real one) | Yes | Re-seeded by reset |

---

## Hot-path details

### Rate limits (`rate:*`)

Sliding-window counters. Two scopes:

- **Per-client (IP + endpoint)** — defends against a single noisy client. Defined in `app/utils/rate_limit.py`. Used as a FastAPI dependency on every mutating endpoint (login, register, job create, etc.). The client identity is the rightmost `X-Forwarded-For` hop — the one the trusted ALB appends — so rotating the caller-supplied part of the header cannot mint fresh buckets.
- **Per-tenant** — defends against a noisy *tenant* (multiple users / multiple processes from the same customer). Defined in `app/utils/quota.py` as `_check_tenant_rate`. Checked at the top of `POST /jobs` after the per-client check.

Both fail open if Redis is unreachable: `logger.warning` and let the request through. The rationale: a Redis outage is bad enough; turning it into a 100% outage by blocking all traffic makes it worse.

Configuration:

- Per-client limits are hard-coded per endpoint (e.g. `rate_limiter(limit=30, window=60)` on `POST /jobs`).
- Per-tenant limit is `tenants.rate_limit_per_minute`, configurable via `PATCH /admin/tenants/{id}`, defaults to 120 r/min. `0` disables.

### SSE progress bridge (`job:progress:{job_id}` Pub/Sub channel + `job:progress:last:{job_id}` snapshot)

The worker publishes progress to Kafka. The `sse-broadcaster` consumer reads Kafka and republishes to Redis Pub/Sub on channel `job:progress:{job_id}`. The browser-facing SSE endpoint at `GET /jobs/{id}/stream` opens a Pub/Sub subscription on that channel and streams events down to the client.

Pub/Sub itself is fire-and-forget and at-most-once, so `publish()` also SETs the event as a retained snapshot at `job:progress:last:{job_id}` (1h TTL) — **before** the PUBLISH, so a subscriber can never both miss the live event and find no snapshot. `subscribe()` then reads that snapshot as its first event, and it reads it *after* the SUBSCRIBE has taken effect: the overlap can at worst deliver one event twice (harmless — the UI is last-write-wins on a progress bar), whereas reading first would leave a gap in which an event is lost entirely.

That snapshot is what stops a late subscriber from hanging. A client that connects after the job already finished (or reconnects across a Redis blip) receives the terminal snapshot immediately and the stream closes, instead of waiting forever on a channel that will never speak again. Terminal for this purpose is `completed | failed | dead_letter | cancelled` — `cancelled` included because saga rollbacks cancel jobs.

Eviction is safe by design: with no snapshot the behaviour degrades to exactly the old pure-Pub/Sub stream, and the SSE endpoint covers the terminal case from durable state instead — it loads the `jobs` row (tenant-scoped) up front and, if the job is already in a terminal status **and** no snapshot is retained, emits one synthetic `ProgressEvent` built from the row and closes. Redis is never a correctness dependency here; durable state lives in `jobs` + `job_events`. A subscriber that disconnects mid-job still misses the intervening events — only the latest one is retained — which is fine for a progress bar.

### CQRS read-model sets (`jobs:tenant:*`, `jobs:user:*`)

Per-tenant sets keyed by `(tenant_id, status)`; per-user sets keyed by `(user_id, status)`. The `read-model` Kafka consumer (`ReadModelProjector` in `app/workers/read_model.py`) handles every lifecycle event by:

1. `SREM job_id` from every *other* status set
2. `SADD job_id` to the *new* status set

Both operations are idempotent: SADD on an existing member is a no-op, SREM on a missing one is a no-op. This is what makes the read-model correct under at-least-once Kafka redelivery.

`GET /admin/stats` and `GET /admin/users/{user_id}/stats` read these sets via `SCARD` — O(1) per status. There's no SQL aggregate on the `jobs` table on the admin read path.

The keys have **no TTL**: they represent the source of truth for the admin overview. If Redis flushes them (eviction, restart), the read-model goes silent until either:

- A future PR adds a "rebuild from event_log" backfill — currently the rebuild path is manual.
- Or each job naturally transitions through a lifecycle event, repopulating its set membership.

This is the most operationally fragile thing about the current Redis usage. Documented as a roadmap item in [`docs/ROADMAP.md`](ROADMAP.md).

The tenant scoping landed in Phase 12 PR D — before that, the sets were keyed only by status (`jobs:status:running`) and one tenant's overview counted sibling tenants' jobs. See the PR description on #38.

### Job cache (`cache:job:{tenant_id}:{job_id}`)

Read-through cache for `GET /jobs/{id}`. JSON serialization of the `JobResponse` schema. TTL 10s. Cache invalidated explicitly on replay and incident-resolve (`JobCache.delete`) so admins re-hitting after a Replay see the fresh state immediately.

The invalidation site is the **service layer** — `JobService.replay_job` and `JobService.resolve_incident` (`app/services/job.py`) — not the REST handlers. That is what makes every write path coherent: `POST /admin/jobs/{id}/replay`, the MCP replay tools (`replay_dlq_messages` / `replay_dlq_by_ids` / `replay_dlq_by_category`), and the scheduled DLQ-replay loop in `app/workers/dispatcher.py` all funnel through `JobService` (E2-02 — previously only the REST wrappers invalidated, so an agent-driven replay left `GET /jobs/{id}` stale for a full TTL).

The `cache:` namespace also makes this key reachable by the `invalidate_cache_key` MCP action, whose allowlist covers `cache:` — an agent can force-refresh a single job read without any change to that tool's contract. Its read counterpart `get_cache_key_info` covers the same prefixes and reports existence / TTL / type / size only — never the cached `JobResponse` payload, which is tenant data.

The key embeds the tenant (E2-01): a caller from another tenant computes a different key, structurally misses, and falls through to the tenant-scoped DB query. Isolation lives in the key, so the cached `JobResponse` payload never needs to carry `tenant_id`.

### Consumer lag (`kafka:consumer_lag:worker-dispatcher`)

The metrics loop calls `dispatcher.consumer_lag()` every 60 seconds and writes the result here with TTL 90 (so the key is always fresh-or-missing, never stale).

`check_backpressure` in `app/utils/backpressure.py` reads this key from the `POST /jobs` hot path. If the value is above `backpressure_lag_threshold`, the request is rejected with `BackpressureError` (503). The API never round-trips to Kafka.

"Lag unknown" has three causes and they all resolve to "let the request through" — fail-open on purpose, same logic as rate limits:

| Cause | Handled by |
|---|---|
| Key missing or expired (metrics loop hasn't run, or Redis is down and returning nothing) | `raw is None` → return |
| Value unparseable | `except (TypeError, ValueError)` → return |
| **The GET itself raised** (Redis unreachable, pool exhausted, timeout) | `except Exception` → `logger.warning("backpressure_check_failed")`, return |

The third row was unhandled until the fail-open fix. That made `check_backpressure` the only Redis touch on `POST /jobs` that failed *closed*: an unreachable Redis raised out of the endpoint and every job submission became a 500, contradicting both this document and the "What happens when Redis is down" table below. Losing an advisory signal is not grounds for refusing work the durable path (Postgres + outbox) can still accept.

The degradation is logged at WARNING (`backpressure_check_failed`) rather than swallowed silently, so a Redis outage is visible as a burst of these rather than as a mysterious absence of 503s. Pinned by `test_redis_error_fails_open` (unit) and `test_job_create_still_works_when_redis_is_down` (API).

### Priority queue + delayed queue (sorted sets)

These predate Phase 7. They were the original job queue (push-pop via Redis sorted set scored by priority). After Phase 7 the primary path became Kafka, but two surfaces still use them:

- **Delayed retries** — when a job fails and gets queued for retry with exponential backoff, it lands in the `delayed_queue` sorted set scored by `time.time() + delay`. The `_promote_delayed_loop` background task pops ready entries (`ZRANGEBYSCORE` with score ≤ now) and republishes them through the outbox.
- **(Legacy)** — the original `priority_queue` sorted set; still has push/pop methods exposed but no longer driven from the create-job path.

These keys have **no TTL** and are critical: losing the delayed queue means delayed retries disappear silently. Persistence is enabled on the Redis instance in production (RDB snapshots + AOF). On local dev with the default `docker-compose.yml` setup, Redis is in-memory only — restarts lose state.

#### Reader semantics for `jobs:delayed`

The pop (`queue._atomic_pop_ready`, shared with `jobs:dlq_replay_delayed`) is a single Lua `EVAL` that does `ZRANGEBYSCORE` + `ZREM` in one round-trip, so two readers can never see the same member. Three consequences worth knowing before touching this path:

- **The pop is bounded**: `LIMIT 0 1000`, so a call returns at most 1000 members and the `ZREM` is chunked. Without the bound, Lua's `unpack` hits `LUAI_MAXCSTACK` (8000 by default) once a backlog builds and the script raises on *every* tick — wedging both sorted sets permanently, since nothing is ever removed. A backlog over the limit drains across ticks instead: both readers poll every 0.5s, so ~2000 members/s per set. **Callers must not assume they received every due member.**
- **The pop is destructive before the work happens**: members are gone from Redis the instant `EVAL` returns, so the promotion pass owns them. `_promote_delayed_once` therefore isolates each job in its own `try` and, on failure, pushes the id back with a short delay (`_PROMOTE_RETRY_DELAY_SECONDS`). One bad job cannot strand the rest of the batch.
- **A backstop covers the crash windows** the re-push cannot. If the worker dies between the pop and the outbox commit, or between the retry transaction's commit and `push_delayed`, the job sits in `pending` with no timer in this ZSET and no Kafka message. `_requeue_stale_pending_loop` sweeps every 60s for jobs `PENDING` and untouched for 300s, skips any job with a live `ZSCORE` in `jobs:delayed` (it is legitimately waiting out a backoff — the LLM retry policy can set those to minutes), and re-publishes the rest. A re-publish is safe to duplicate: the second delivery loses the atomic `pending -> running` claim and executes nothing.
  The same backstop now also covers a live Redis *error* on that second window, not just a worker death: `_run_job`'s retry-branch `push_delayed` is wrapped like the module's other three call sites, so a connection blip logs and leaves the job `PENDING` for this sweep. It used to be the one unguarded call site, and the raise escaped into the dispatcher's force-dead-letter safety net — terminally dead-lettering a job that still had retries left, which is exactly the correctness dependency on Redis this catalog says the platform does not have.

---

## Eviction policy

Production: `maxmemory-policy noeviction`. We never want Redis to silently drop a key — the failure modes are bad enough already (read-model goes silent, delayed retries disappear). Better to OOM and alert.

Rationale: every key in this catalog either has a TTL (so it expires naturally) or is durable state we don't want lost. There's no LRU-cacheable category big enough to justify enabling eviction.

When `maxmemory` fills up, writes start failing with OOM. The CloudWatch alarm `redis-memory-low` fires at 80% utilization (see `infra/cloudwatch.tf`, runbook `runbooks/rb-redis-memory-low.yaml`).

---

## Persistence

Production: AOF every second + daily RDB snapshot. The trade-off is durability vs. throughput; for our key mix the AOF cost is acceptable.

Local dev: in-memory, no AOF, no snapshot. Restarts lose state. This is fine because:
- The dispatcher would rebuild the priority queue from `jobs WHERE status=pending` on startup (TODO: this isn't actually wired — currently a restart leaves PENDING jobs orphaned).
- The read-model would rebuild as new events flow.
- The delayed queue would lose pending retries (those jobs are stuck in `failed` status with `retry_count < max_retries` until manually replayed).

The orphaned-PENDING-on-restart issue is the second roadmap item in [`docs/ROADMAP.md`](ROADMAP.md).

---

## Operational ops

### Inspect a key

```bash
docker compose exec redis redis-cli
> KEYS 'jobs:tenant:*'                    # don't do this in prod
> SCAN 0 MATCH 'jobs:tenant:*' COUNT 100  # do this in prod
> SMEMBERS jobs:tenant:<tid>:status:completed
> ZRANGEBYSCORE delayed_queue 0 +inf WITHSCORES
> GET kafka:consumer_lag:worker-dispatcher
```

### Rebuild the read-model

Manual today; future PR (Phase 8 platform work) makes it an admin endpoint. The pattern:

```python
# DELETE all jobs:tenant:* and jobs:user:* keys
# Replay from event_log:
#   for each job_event in order:
#     apply the same logic as ReadModelProjector.handle_message
```

### Clear the cache

`FLUSHDB` is too blunt — wipes everything including the delayed queue. Better (job-cache keys live under `cache:job:{tenant_id}:{job_id}`):

```bash
SCAN 0 MATCH 'cache:job:*' COUNT 100 | xargs DEL
```

Cache misses are fine; they re-populate on next read.

---

## What happens when Redis is down

The platform degrades, doesn't fail. Specifically:

| Surface | Behavior |
|---|---|
| `POST /jobs` | Rate limit + tenant quota fail open. Backpressure check fails open. Job submission succeeds via DB + outbox. |
| `GET /jobs/{id}/stream` (SSE) | Connection opens, sends initial DB state, never streams updates. UI shows static progress. |
| `GET /admin/stats` | Returns whatever was in Redis before the outage (cache or empty). Doesn't 500. |
| `GET /admin/jobs/{id}` | Cache miss, falls through to DB. Slower but works. |
| Worker loop | Delayed retry promotion stops (the loop catches the exception and continues). Active job processing continues — the DB and Kafka are the load-bearing components. |

The platform is designed so that Redis is a **performance + UX** dependency, not a **correctness** one. The truth is in Postgres + Kafka.

---

## Pointers

- `backend/app/utils/rate_limit.py` — per-client rate limiting
- `backend/app/utils/quota.py` — per-tenant rate limit + monthly quota
- `backend/app/utils/cache.py` — `JobCache`
- `backend/app/utils/backpressure.py` — `check_backpressure`
- `backend/app/workers/read_model.py` — CQRS read-model projector
- `backend/app/workers/progress.py` — Pub/Sub publish
- `backend/app/workers/queue.py` — priority queue + delayed queue
- `backend/app/api/streaming.py` — SSE endpoint subscribing to Pub/Sub
- `infra/elasticache.tf` — production ElastiCache provisioning
