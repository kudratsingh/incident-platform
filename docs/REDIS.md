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
| `jobs:tenant:{tenant_id}:status:{status}` | set of job_ids | `read_model.py` `_move` | `admin.py` `system_stats` via `read_global_stats` | None | No — read model goes stale |
| `jobs:user:{user_id}:status:{status}` | set of job_ids | `read_model.py` `_move` | `admin.py` `user_stats` via `read_user_stats` | None | No — read model goes stale |
| `cache:job:{job_id}` | JSON string | `cache.py` `JobCache.put` | `cache.py` `JobCache.get` | 300s | Yes — cache miss = DB read |
| `kafka:consumer_lag:dispatcher` | string (int) | `dispatcher.py` `_metrics_loop` | `backpressure.py` `check_backpressure` | 90s | Yes — falls back to "no backpressure" |
| `priority_queue` (sorted set) | sorted set of job_ids by priority score | `queue.py` `push` | `queue.py` `pop` | None | Critical — see below |
| `delayed_queue` (sorted set) | sorted set of job_ids by ready-at timestamp | `queue.py` `push_delayed` | `queue.py` `pop_ready_delayed` | None | Critical — see below |

---

## Hot-path details

### Rate limits (`rate:*`)

Sliding-window counters. Two scopes:

- **Per-client (IP + endpoint)** — defends against a single noisy client. Defined in `app/utils/rate_limit.py`. Used as a FastAPI dependency on every mutating endpoint (login, register, job create, etc.).
- **Per-tenant** — defends against a noisy *tenant* (multiple users / multiple processes from the same customer). Defined in `app/utils/quota.py` as `_check_tenant_rate`. Checked at the top of `POST /jobs` after the per-client check.

Both fail open if Redis is unreachable: `logger.warning` and let the request through. The rationale: a Redis outage is bad enough; turning it into a 100% outage by blocking all traffic makes it worse.

Configuration:

- Per-client limits are hard-coded per endpoint (e.g. `rate_limiter(limit=30, window=60)` on `POST /jobs`).
- Per-tenant limit is `tenants.rate_limit_per_minute`, configurable via `PATCH /admin/tenants/{id}`, defaults to 120 r/min. `0` disables.

### SSE progress bridge (`job:progress:{job_id}` Pub/Sub channel)

The worker publishes progress to Kafka. The `sse-broadcaster` consumer reads Kafka and republishes to Redis Pub/Sub on channel `job:progress:{job_id}`. The browser-facing SSE endpoint at `GET /jobs/{id}/stream` opens a Pub/Sub subscription on that channel and streams events down to the client.

Pub/Sub is fire-and-forget — no key is stored. A subscriber that disconnects mid-job will miss intervening events and won't see them on reconnect (the SSE endpoint sends the *current* job state via DB query on subscribe, then streams Pub/Sub from there). This is acceptable: the UI uses Pub/Sub only for live updates; durable state lives in `jobs` + `job_events`.

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

### Job cache (`cache:job:{job_id}`)

Read-through cache for `GET /jobs/{id}` and `GET /admin/jobs/{id}`. JSON serialization of the `JobResponse` schema. TTL 300s. Cache invalidated explicitly on replay (`JobCache.delete`) so admins re-hitting after a Replay see the fresh state immediately.

### Consumer lag (`kafka:consumer_lag:dispatcher`)

The metrics loop calls `dispatcher.consumer_lag()` every 60 seconds and writes the result here with TTL 90 (so the key is always fresh-or-missing, never stale).

`check_backpressure` in `app/utils/backpressure.py` reads this key from the `POST /jobs` hot path. If the value is above `backpressure_lag_threshold`, the request is rejected with `BackpressureError` (503). The API never round-trips to Kafka.

If the key is missing (Redis down, or the metrics loop hasn't run yet), the backpressure check treats it as "no lag" and lets the request through. Fail-open on purpose — same logic as rate limits.

### Priority queue + delayed queue (sorted sets)

These predate Phase 7. They were the original job queue (push-pop via Redis sorted set scored by priority). After Phase 7 the primary path became Kafka, but two surfaces still use them:

- **Delayed retries** — when a job fails and gets queued for retry with exponential backoff, it lands in the `delayed_queue` sorted set scored by `time.time() + delay`. The `_promote_delayed_loop` background task pops ready entries (`ZRANGEBYSCORE` with score ≤ now) and republishes them through the outbox.
- **(Legacy)** — the original `priority_queue` sorted set; still has push/pop methods exposed but no longer driven from the create-job path.

These keys have **no TTL** and are critical: losing the delayed queue means delayed retries disappear silently. Persistence is enabled on the Redis instance in production (RDB snapshots + AOF). On local dev with the default `docker-compose.yml` setup, Redis is in-memory only — restarts lose state.

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
> GET kafka:consumer_lag:dispatcher
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

`FLUSHDB` is too blunt — wipes everything including the delayed queue. Better:

```bash
SCAN 0 MATCH 'cache:*' COUNT 100 | xargs DEL
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
