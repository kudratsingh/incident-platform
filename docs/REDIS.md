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
| `job:progress:{job_id}` | Pub/Sub channel (no persisted key) | `progress.py` `publish` | `progress_broker.py` — one shared subscription per process, fanned out to every viewer of that job | N/A (Pub/Sub is fire-and-forget) | No — listeners must be connected |
| `job:progress:last:{job_id}` | string (last `ProgressEvent` as JSON) | `progress.py` `publish` (fed by `sse_consumer`) | `progress_broker.py` `subscribe` via `progress.py` `read_last_event`, for the SSE endpoint | 1h (`LAST_EVENT_TTL_SECONDS`) | Yes — degrades to the pre-snapshot stream; the `jobs` row is the fallback |
| `jobs:tenant:{tenant_id}:status:{status}` | sorted set of job_ids, scored by projection time, capped at `READ_MODEL_WINDOW` | `read_model.py` `_move` | `admin.py` `system_stats` via `read_global_stats` | 7d (`READ_MODEL_TTL_SECONDS`), refreshed on write | Recoverable — `rebuild_read_model` restores it from `jobs` |
| `jobs:tenant:{tenant_id}:status:{status}:evicted` | string (counter) | `read_model.py` `_project` trim | `read_model.py` `_member_count` | same as its key | Recoverable — same rebuild |
| `jobs:user:{user_id}:status:{status}` | sorted set of job_ids, scored by projection time, capped at `READ_MODEL_WINDOW` | `read_model.py` `_move` | `admin.py` `user_stats` via `read_user_stats` | 7d (`READ_MODEL_TTL_SECONDS`), refreshed on write | Recoverable — `rebuild_read_model` restores it from `jobs` |
| `jobs:user:{user_id}:status:{status}:evicted` | string (counter) | `read_model.py` `_project` trim | `read_model.py` `_member_count` | same as its key | Recoverable — same rebuild |
| `cache:job:{tenant_id}:{job_id}` | JSON object (`JobResponse`) | `cache.py` `JobCache.set` — **live path, not chaos-writable** | `cache.py` `JobCache.get` | 10s | Yes — cache miss = DB read; a non-object payload also reads as a miss |
| `kafka:consumer_lag:worker-dispatcher` | string (int) | `dispatcher.py` `_metrics_loop` | `backpressure.py` `check_backpressure` | 90s | Yes — falls back to "no backpressure" |
| `priority_queue` (sorted set) | sorted set of job_ids by priority score | `queue.py` `push` | `queue.py` `pop` | None | Critical — see below |
| `delayed_queue` (sorted set) | sorted set of job_ids by ready-at timestamp | `queue.py` `push_delayed` | `queue.py` `pop_ready_delayed` | None | Critical — see below |

### Agent-era keys (chaos, Tier-1 action residue, eval fixtures)

These namespaces are written by the incident-commander MCP tools and the eval tooling, not by the platform's own request/worker paths. The `chaos:*` tools are registered only when `CHAOS_ENABLED=true` (ADR 0008); the rows marked **eval-only** are seeded by `scripts/seed_eval_fixtures.py` and never appear outside an eval stack. `scripts/reset_eval_state.py` clears residue between scenarios: a `chaos:*` scan-and-delete, the `jobs:dlq_replay_delayed` + `jobs:dlq_replay_inflight` ZSETs, every `dag:paused:*` flag, and a re-seed of the fixture keys.

Any key under `cache:` / `jobs:cache:` / `kafka:consumer_lag:` / `read_model:` — the same prefixes `invalidate_cache_key` may delete — is additionally observable through the `get_cache_key_info` MCP read tool (`telemetry:read`), which reports existence, TTL, type and size but never the value. That is the read half of the stale-cache remediation loop: check the key before invalidating, confirm the delete after.

| Key pattern | Type | Writer | Reader | TTL | Eviction-safe? | Cleared by eval reset? |
|---|---|---|---|---|---|---|
| `chaos:kill:{group}` | string flag | `kill_consumer` chaos tool (`kill_key_for`) | `kafka_consumer.py` `_check_chaos_kill` — top of every poll; consumer shuts down while set | `ttl_seconds` (default 300s) | Yes — chaos ends early | Yes (`chaos:*` scan) |
| `chaos:latency:{group}` | string (ms) | `inject_latency` chaos tool (`latency_key_for`) | `kafka_consumer.py` `_check_chaos_latency` — sleep before each poll | `ttl_seconds` (default 300s) | Yes — chaos ends early | Yes (`chaos:*` scan) |
| `chaos:bad_deploy` | string (label) | `bad_deploy` chaos tool (`BAD_DEPLOY_KEY`) | Nothing yet — the fired `critical` alert is the observable signal | `ttl_seconds` (default 600s) | Yes | Yes (`chaos:*` scan) |
| `chaos:sat:{run_id}:{i}` | string (filler) | `saturate_redis` chaos tool (default 1000 keys × 1 KiB) | Nobody — exists purely for memory pressure | `ttl_seconds` (default 60s) | Yes — eviction is the point | Yes (`chaos:*` scan; usually already expired) |
| `dag:paused:{root_job_id}` | string flag | `pause_dag` action tool (`pause_key_for` in `app/utils/dag_pause.py`) | Every dispatch path via `find_blocking_pause` — resolver promotion, resume sweep, delayed-retry promotion, `replay_job`, the scheduled DLQ-replay loop and `_run_job`'s pre-claim re-check, plus a create-time WAITING hold (ADR 0011 + its 2026-08-09 amendment); reported by `get_dag_state` | `ttl_seconds` (default 600s) | Yes — pause fails open, DAG resumes | Yes (`dag:paused:*` scan) |
| `jobs:dlq_replay_delayed` | sorted set of `{tenant}:{principal}:{job}` by fire-at time | `replay_dlq_by_ids` / `replay_dlq_by_category` (`delay_seconds` path), audit row first — see below | `dlq_replay_scheduler.claim_ready` via the worker's `_promote_dlq_replay_loop` | None | No — pending delayed replays disappear silently | Yes (ZSET deleted) |
| `jobs:dlq_replay_inflight` | sorted set of the same members by claim deadline | `dlq_replay_scheduler.claim_ready` (moves them off the set above) | Same loop; released by `ack_replay`, reclaimed by a later `claim_ready` once the deadline lapses | None (logical TTL = `CLAIM_TTL_SECONDS`, 60s) | No — a claimed-but-unfired replay disappears silently | Yes (ZSET deleted) |
| `cache:jobs:worker-dispatcher:hot_set` (**eval-only**) | JSON array of seeded DLQ job ids | `seed_eval_fixtures.py` `_seed_hot_set`; also the `create_stale_cache` chaos tool | The stale-cache scenario observes it via `get_cache_key_info`, then deletes it via `invalidate_cache_key` | 24h (seed) / `ttl_seconds` default 600s (tool) | Yes — re-seeded | Re-populated by reset |
| `kafka:consumer_lag:{group}` (**eval-only** except `worker-dispatcher`) | string (int) | Metrics loop writes only `worker-dispatcher` (row above); `seed_eval_fixtures.py` writes the 7 synthetic groups (`billing-consumer` … `healthy-consumer`) | `get_consumer_lag` MCP tool (any group); `check_backpressure` reads only `worker-dispatcher` | **none** on seeded groups (90s on the real one) | Yes | Re-seeded by reset |

---

## Hot-path details

### Rate limits (`rate:*`)

**Fixed**-window counters, keyed on `int(time.time()) // window`. Named
"sliding" here and in three `app/utils/rate_limit.py` docstrings until
WO-R2-30, which promised a bound the code has never enforced: because the
bucket resets on absolute boundaries rather than moving with the caller, a
client that spends its allowance just before a boundary gets a fresh one
immediately after. **The enforced ceiling is `2 * limit` across a
boundary instant**, and every caller's limit is sized against that doubled
figure. Pinned by
`backend/tests/api/test_rate_limit_surfaces.py::test_fixed_window_admits_2x_across_a_boundary`.

Four scopes:

- **Per-client (IP + endpoint)** — defends against a single noisy client. Defined in `app/utils/rate_limit.py`. Used as a FastAPI dependency on every mutating endpoint (login, register, job create, etc.). `POST /jobs` and `POST /sagas` deliberately share ONE bucket (`JOB_CREATE_RATE_BUCKET` in `app/utils/admission.py`): separate buckets would let a caller refused by one keep creating job rows through the other. The client identity is the rightmost `X-Forwarded-For` hop — the one the trusted ALB appends — so rotating the caller-supplied part of the header cannot mint fresh buckets.
- **Per-principal (MCP)** — defends the MCP process against a tool-call storm from one machine principal. Defined as `check_identity_rate_limit` in `app/utils/rate_limit.py`, keyed on `Principal.id` under the `mcp:principal` bucket, enforced in `app/mcp/standalone.py` between JSON-RPC parsing and dispatch. Keyed on the principal rather than the IP because every agent reaches the process from the same address, so an IP bucket would let one noisy principal throttle the others. Ceiling: `MCP_RATE_LIMIT_PER_PRINCIPAL` (default 120/min), sized to stop a runaway loop from exhausting the MCP process's DB pool (SQLAlchemy defaults: 5 + 10 overflow = 15 connections) without touching a legitimate eval run. Refusals come back as a JSON-RPC error with code `MCP_RATE_LIMITED` (-32003) and HTTP 429.
- **Per-admin, per paid endpoint** — bounds *spend*, not load. `POST /admin/query` (~$0.006 an Anthropic call) and `POST /admin/digests/generate` (~$0.018) each make one paid call per request and had no limiter at all before WO-R2-30. Keyed on the admin user id under the `admin:nl_query` and `admin:digest` buckets — separate, so exhausting one never blocks the other — and checked immediately before the paid call, so a validation error or a disabled feature flag costs no allowance. Ceilings: `ADMIN_NL_QUERY_RATE_LIMIT` (10/min) and `ADMIN_DIGEST_RATE_LIMIT` (5/min).
- **Per-tenant** — defends against a noisy *tenant* (multiple users / multiple processes from the same customer). Defined in `app/utils/quota.py` as `_check_tenant_rate`. Checked after the per-client check by `check_job_admission`, which every job-creating endpoint runs. Counted in *requests*: one saga is one request no matter how many steps it has — its step count is weighed against the monthly job quota instead, whose unit is jobs.

All four fail open if Redis is unreachable: `logger.warning` and let the request through. The rationale: a Redis outage is bad enough; turning it into a 100% outage by blocking all traffic makes it worse.

Configuration:

- Per-client limits are hard-coded per endpoint (e.g. `rate_limiter(limit=30, window=60)` on `POST /jobs`).
- Per-tenant limit is `tenants.rate_limit_per_minute`, configurable via `PATCH /admin/tenants/{id}`, defaults to 120 r/min. `0` disables.

### SSE progress bridge (`job:progress:{job_id}` Pub/Sub channel + `job:progress:last:{job_id}` snapshot)

The worker publishes progress to Kafka. The `sse-broadcaster` consumer reads Kafka and republishes to Redis Pub/Sub on channel `job:progress:{job_id}`. The browser-facing SSE endpoint at `GET /jobs/{id}/stream` reads that channel through the process-wide fan-out broker (`app/workers/progress_broker.py`) and streams events down to the client.

#### Connection budget — why streaming has its own pool and its own cap

A Pub/Sub subscription owns its connection for as long as it is subscribed. The endpoint used to call `redis.pubsub()` per viewer, so the process's open-stream count *was* its held-connection count, and those connections came out of the single 20-slot pool shared with `worker_loop`, the rate limiter, `check_backpressure`, the job cache and the admin stats loops. The practical viewer ceiling was well under 20, and past it the failures landed on everyone *except* the viewer who caused them: the rate limiter failed open silently, `check_backpressure` 500'd `POST /jobs`, admin stats errored. The frontend reconnects every 2s until terminal, so a tab parked on a waiting job pinned a slot indefinitely.

Three things now hold the line:

| Mechanism | Where | Effect |
|---|---|---|
| **Fan-out broker** | `workers/progress_broker.py` | One Pub/Sub connection for the whole process. It SUBSCRIBEs a channel on that job's first viewer and UNSUBSCRIBEs on its last; a single reader task pumps messages into a per-viewer `asyncio.Queue`. N viewers of one job and M jobs all cost **one** connection — the viewers↔connections relationship is gone, not widened. |
| **Dedicated pool** | `core/redis.py` `get_sse_redis_pool()` | Streaming draws from its own pool (`SSE_REDIS_MAX_CONNECTIONS`, default 5), never the shared 20. Whatever streaming does to its own pool, the request path keeps its slots. |
| **Explicit cap + timeouts** | `SSE_MAX_CONCURRENT_STREAMS` (default 200), `SSE_STREAM_IDLE_TIMEOUT_SECONDS` (300), `SSE_STREAM_MAX_DURATION_SECONDS` (3600) | Past the cap the endpoint answers **503 with `Retry-After`** — a refusal addressed to the viewer asking, instead of a cost charged to unrelated callers. Idle and maximum-duration timeouts reclaim the slot from a parked tab; EventSource reconnects on its own, so ending a stream costs a live viewer one reconnect. Set any of the three to `0` to disable it. |

The cap is per *process*, so the deployment-level ceiling is `SSE_MAX_CONCURRENT_STREAMS × replicas`. A client that meets a 503 here should retry (possibly landing on another replica) — unlike a `backpressure` 503, it is not a signal to stop submitting work, which is why it carries its own `stream_capacity` error code.

Pub/Sub itself is fire-and-forget and at-most-once, so `publish()` also SETs the event as a retained snapshot at `job:progress:last:{job_id}` (1h TTL) — **before** the PUBLISH, so a subscriber can never both miss the live event and find no snapshot. `subscribe()` then reads that snapshot as its first event, and it reads it *after* the SUBSCRIBE has taken effect: the overlap can at worst deliver one event twice (harmless — the UI is last-write-wins on a progress bar), whereas reading first would leave a gap in which an event is lost entirely.

That snapshot is what stops a late subscriber from hanging. A client that connects after the job already finished (or reconnects across a Redis blip) receives the terminal snapshot immediately and the stream closes, instead of waiting forever on a channel that will never speak again. Terminal for this purpose is `completed | failed | dead_letter | cancelled` — `cancelled` included because saga rollbacks cancel jobs.

**The snapshot write is ordered (WO-R2-57).** It used to be unconditional, so the last *write* won rather than the latest *event* — and these events come off Kafka, where a rebalance or a failed handler redelivers a `job.progress` the job has already moved past. That redelivery overwrote a terminal snapshot with `running`, and nothing corrected it, because a finished job produces no further events: for the snapshot's remaining hour every late subscriber was told the job was still running and sat on a dead channel. `publish()` now refuses to replace a terminal snapshot with a non-terminal one, and refuses an event whose Kafka offset within its own topic is one the snapshot has already seen (offsets from different topics are incomparable, which is what the terminal rule covers). Terminal→terminal still applies, so a DLQ replay's eventual `job.completed` lands.

The inverse — a terminal snapshot in front of a job a replay put back in flight — is reconciled at the endpoint, which holds the `jobs` row: if the row is non-terminal and was updated *after* the snapshot was written, the row wins and the stream skips the snapshot (`subscribe(..., use_snapshot=False)`). The tie-break is recency rather than a blanket preference, so the ordinary race — the job finishing microseconds after the row was read — still ends the stream promptly instead of waiting out the idle timeout.

Publish rate is bounded too: `progress.rate_limited` wraps the publisher a processor is handed, so the number of Kafka messages and immutable `job_events` rows a job writes follows elapsed work (at most one per whole percent, and no more than one per half second) rather than the caller's chunk count.

Eviction is safe by design: with no snapshot the behaviour degrades to exactly the old pure-Pub/Sub stream, and the SSE endpoint covers the terminal case from durable state instead — it loads the `jobs` row (tenant-scoped) up front and, if the job is already in a terminal status **and** no snapshot is retained, emits one synthetic `ProgressEvent` built from the row and closes. Redis is never a correctness dependency here; durable state lives in `jobs` + `job_events`. A subscriber that disconnects mid-job still misses the intervening events — only the latest one is retained — which is fine for a progress bar.

### CQRS read-model (`jobs:tenant:*`, `jobs:user:*`)

Per-tenant keys keyed by `(tenant_id, status)`; per-user keys keyed by `(user_id, status)`. The `read-model` Kafka consumer (`ReadModelProjector` in `app/workers/read_model.py`) handles every lifecycle event by:

1. `ZREM job_id` from every *other* status key
2. `ZADD job_id` (scored by projection time) to the *new* status key

Both operations are idempotent: re-adding an existing member updates its score and nothing else, and `ZREM` on a missing one is a no-op. This is what makes the read-model correct under at-least-once Kafka redelivery.

`GET /admin/stats` and `GET /admin/users/{user_id}/stats` read these keys via `ZCARD` plus the `:evicted` counter — O(1) per status. There's no SQL aggregate on the `jobs` table on the admin read path.

**Bounded, with a reaper (WO-R2-56).** These were unbounded SETs with no TTL: every terminal job_id stayed a member forever, and production runs `noeviction`, which cannot reclaim a key that has no TTL. Now:

- The key is a ZSET trimmed to the `READ_MODEL_WINDOW` (10,000) most recent ids after every write, oldest first. Ordering is the whole reason it is a ZSET rather than a SET — `SPOP` evicts a random member, which can drop a just-projected terminal id and re-open the reordering hole the projector's terminal guard exists to close.
- What the trim removes is added to a sibling `:evicted` counter, so a status count is `ZCARD + evicted` and stays whole. The counter is monotonic, so a *trimmed* id that later changes status leaves its old status over-counted by one — bounded by the number of ids ever trimmed, and corrected by a rebuild.
- Every key carries a 7-day TTL refreshed on each write. That is the reaper (a tenant or user that stops submitting stops holding memory) and it is also what makes the key evictable at all under a `volatile-*` policy.

**Rebuilding is a real path now**, not a roadmap item: `read_model.rebuild_read_model(session, redis, tenant_id=None)` recomputes every key from the `jobs` table — exact counts from a `GROUP BY`, membership from a windowed query — and `scripts/reset_eval_state.py` runs it at the end of every eval reset. That matters because the projection cannot heal itself: an id only moves when an event names it, and no further event is coming for a finished job, so anything an eviction or a restart or a `saturate_redis` run drops stays dropped. Use it after any Redis incident, and after deploying WO-R2-56 (the projector migrates a pre-existing SET key by dropping it, which is safe but lossy until the rebuild runs).

The tenant scoping landed in Phase 12 PR D — before that, the keys were keyed only by status (`jobs:status:running`) and one tenant's overview counted sibling tenants' jobs. See the PR description on #38.

### Job cache (`cache:job:{tenant_id}:{job_id}`)

Read-through cache for `GET /jobs/{id}`. JSON serialization of the `JobResponse` schema. TTL 10s. Cache invalidated explicitly on replay and incident-resolve so admins re-hitting after a Replay see the fresh state immediately.

The invalidation site is the **service layer** — `JobService.replay_job` and `JobService.resolve_incident` (`app/services/job.py`) — not the REST handlers. That is what makes every write path coherent: `POST /admin/jobs/{id}/replay`, the MCP replay tools (`replay_dlq_messages` / `replay_dlq_by_ids` / `replay_dlq_by_category`), and the scheduled DLQ-replay loop in `app/workers/dispatcher.py` all funnel through `JobService` (E2-02 — previously only the REST wrappers invalidated, so an agent-driven replay left `GET /jobs/{id}` stale for a full TTL).

**Replay invalidates after the commit, with a tombstone** (`JobCache.invalidate`, R2-23). Two things were wrong with a plain `JobCache.delete` issued from inside the replay transaction:

- *Timing.* Until the transaction commits, the cached row is still what every other connection would read. Deleting early advertises a change nobody can see yet — and hands a concurrent reader an empty slot to refill with the row that has not changed. `replay_job` now registers the invalidation on the session (`app/utils/post_commit.py`) and the session owner drains it once the commit lands: `get_db` for the API and MCP processes, the scheduled-replay loop for the worker. On rollback the drain never runs, which is the point — no commit, no announcement.
- *The race that timing alone does not close.* A reader that missed and read the pre-replay row from Postgres is still holding it, and its `JobCache.set` can land after the delete. So `invalidate` parks a short `__invalidated__` tombstone in the slot (TTL 30s) instead of emptying it, and `JobCache.set` writes with `NX`. One `SET` both destroys the stale value and closes the slot to every writer until the tombstone expires — long enough that any reader carrying a pre-commit snapshot has given up. Readers miss and go to Postgres, which is the correct answer for as long as we cannot distinguish a fresh write from a stale one. `JobCache.get` reads the tombstone as a miss.

The `NX` has one visible consequence: a live entry is never refreshed mid-TTL — whoever wrote it wins for the remaining seconds. At a 10s TTL that is noise, and the loser's value was no fresher than the winner's.

`resolve_incident` still uses the plain `JobCache.delete`. Same race in principle, but it writes a terminal status that nothing subsequently changes, so a stale entry costs one TTL and then resolves itself.

The `cache:` namespace also makes this key reachable by the `invalidate_cache_key` MCP action, whose allowlist covers `cache:` — an agent can force-refresh a single job read without any change to that tool's contract. Its read counterpart `get_cache_key_info` covers the same prefixes and reports existence / TTL / type / size only — never the cached `JobResponse` payload, which is tenant data.

Read and delete, but **not write**: the `create_stale_cache` chaos hook inherits the same `cache:` allowlist and so used to admit this key, where it wrote a JSON array of fabricated IDs (R2-20). `JobCache.get` handed that straight to `JobResponse.model_validate` and `GET /jobs/{id}` 500'd for the whole TTL. Two changes close it — the hook now refuses `cache:job:` outright (`stale_cache_key_forbidden`, pinned by `test_chaos_hook_cannot_write_the_live_job_read_cache`), and `JobCache.get` treats any payload that is not a JSON object as a miss, so a corrupt entry costs one Postgres read instead of an outage. The reset also sweeps `cache:job:*`, which is otherwise the one namespace chaos residue can occupy without wearing the `chaos:` name.

The key embeds the tenant (E2-01): a caller from another tenant computes a different key, structurally misses, and falls through to the tenant-scoped DB query. Isolation lives in the key, so the cached `JobResponse` payload never needs to carry `tenant_id`.

### Consumer lag (`kafka:consumer_lag:worker-dispatcher`)

The metrics loop calls `dispatcher.consumer_lag()` every 60 seconds and writes the result here with TTL 90 (so the key is always fresh-or-missing, never stale).

**Only this one group is refreshed.** The other seven advertised groups (`billing-consumer` … `healthy-consumer`) are static values written once by `seed_eval_fixtures.py::_seed_consumer_lag` and refreshed by nothing. They carried a 24h TTL until R2-17, on the reasoning that a fresh run re-seeds anyway — but the *stack* outlives the run. After a day of uptime without a re-seed the keys expired, and the six live scenarios that assert a non-null lag for these groups began failing as agent errors rather than as an unmet precondition, one wasted paid run each. They are now written durably like every other fixture, with the reset's rebaseline pass responsible for their freshness instead of a TTL.

The same asymmetry made `inject_latency`'s "watch the group's lag grow" false for every group but `worker-dispatcher` — a static number cannot grow. `get_consumer_lag` now reports a `source` of `live` or `static` per group so the agent can tell which readings can move. That field is worded operationally rather than as `fixture`/`seed`: ADR 0012 rule 1 bans lab vocabulary from the non-chaos tool surface and `test_no_lab_vocabulary_on_non_chaos_tool_surface` enforces it, so the wire says "a recorded constant" and this document says which script records it.

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

The pop (`queue._atomic_pop_ready`) is a single Lua `EVAL` that does `ZRANGEBYSCORE` + `ZREM` in one round-trip, so two readers can never see the same member. Three consequences worth knowing before touching this path:

- **The pop is bounded**: `LIMIT 0 1000`, so a call returns at most 1000 members and the `ZREM` is chunked. Without the bound, Lua's `unpack` hits `LUAI_MAXCSTACK` (8000 by default) once a backlog builds and the script raises on *every* tick — wedging the sorted set permanently, since nothing is ever removed. A backlog over the limit drains across ticks instead: the reader polls every 0.5s, so ~2000 members/s. **Callers must not assume they received every due member.** The DLQ-replay claim script below carries the same two guards for the same reason.
- **The pop is destructive before the work happens**: members are gone from Redis the instant `EVAL` returns, so the promotion pass owns them. `_promote_delayed_once` therefore isolates each job in its own `try` and, on failure, pushes the id back with a short delay (`_PROMOTE_RETRY_DELAY_SECONDS`). One bad job cannot strand the rest of the batch.
- **A backstop covers the crash windows** the re-push cannot. If the worker dies between the pop and the outbox commit, or between the retry transaction's commit and `push_delayed`, the job sits in `pending` with no timer in this ZSET and no Kafka message. `_requeue_stale_pending_loop` sweeps every 60s for jobs `PENDING` and untouched for 300s, skips any job with a live `ZSCORE` in `jobs:delayed` (it is legitimately waiting out a backoff — the LLM retry policy can set those to minutes), and re-publishes the rest. A re-publish is safe to duplicate: the second delivery loses the atomic `pending -> running` claim and executes nothing.
  The same backstop now also covers a live Redis *error* on that second window, not just a worker death: `_run_job`'s retry-branch `push_delayed` is wrapped like the module's other three call sites, so a connection blip logs and leaves the job `PENDING` for this sweep. It used to be the one unguarded call site, and the raise escaped into the dispatcher's force-dead-letter safety net — terminally dead-lettering a job that still had retries left, which is exactly the correctness dependency on Redis this catalog says the platform does not have.

#### Reader semantics for `jobs:dlq_replay_delayed` — claim, don't pop

Scheduled DLQ replays shared the destructive pop above until R2-21. They can't: `jobs:delayed` survives a lost batch because `_promote_delayed_once` re-pushes on failure, and this path deliberately does **not** re-enqueue a failed replay — the operator sees the `job.replay_scheduled` audit row with no matching `job.replayed` row and re-issues, and auto-retrying would mask a permanent problem like "the job was deleted". So a pop that discarded the batch before any replay was attempted lost operator- and agent-scheduled remediations outright, with no record of the loss.

The reader is now a claim over two keys (`dlq_replay_scheduler._CLAIM_READY_LUA`, still one `EVAL`, still bounded and chunk-`ZREM`ing):

- `claim_ready` moves due members from `jobs:dlq_replay_delayed` into `jobs:dlq_replay_inflight`, scored with a claim deadline of now + `CLAIM_TTL_SECONDS` (60s). It re-claims lapsed in-flight entries *first*, so recovered work is never starved by a large fresh batch.
- Every outcome the promote pass can observe — fired, deferred for a paused DAG, or failed — calls `ack_replay`, which `ZREM`s the claim. Failure policy is unchanged: acked and dropped, not re-enqueued.
- Only a worker that dies mid-item skips the ack. `CancelledError` is a `BaseException`, so it bypasses the per-item `except Exception` by construction. The claim lapses and the next tick recovers it.
- The cost is a replay that can fire twice if the worker dies after `replay_job` commits but before the ack. Bounded and benign: the job is no longer `dead_letter`/`failed` by then, so the second attempt is refused with a `JobError` and logged.
- Malformed members can never be acked by anyone (they don't parse into a tenant/principal/job triple), so `claim_ready` acks them itself rather than reclaiming them forever.

The writer half is ordered to match. `_scheduled_replay.schedule_one_audited` — shared by `replay_dlq_by_ids` and `replay_dlq_by_category` so the two cannot drift apart again — writes the `job.replay_scheduled` audit row **before** arming the ZSET entry, inside a `begin_nested` savepoint, and `ZREM`s the entry if anything afterwards fails. Armed-without-audit is the one state this path does not allow: an agent remediation that fires with no audit evidence breaks the audit-is-ground-truth invariant.

---

## Eviction policy

Production: `maxmemory-policy noeviction`. We never want Redis to silently drop a key — the failure modes are bad enough already (the read-model under-reports, delayed retries disappear). Better to OOM and alert.

Rationale: every key in this catalog either has a TTL (so it expires naturally) or is durable state we don't want lost. There's no LRU-cacheable category big enough to justify enabling eviction.

`noeviction` makes an unbounded key without a TTL a slow OOM rather than a large key, which is why the read-model keys are now both capped and TTL'd (WO-R2-56) — under this policy nothing else was ever going to reclaim them. `saturate_redis` is the tool that makes the pressure happen on purpose; its own keys expire, but keys it pushes out do not come back by themselves, and for the read-model the way back is `rebuild_read_model`.

When `maxmemory` fills up, writes start failing with OOM. The CloudWatch alarm `redis-memory-low` fires at 80% utilization (see `infra/cloudwatch.tf`, runbook `runbooks/rb-redis-memory-low.yaml`).

---

## Persistence

Production: AOF every second + daily RDB snapshot. The trade-off is durability vs. throughput; for our key mix the AOF cost is acceptable.

Local dev: in-memory, no AOF, no snapshot. Restarts lose state. This is fine because:
- The dispatcher would rebuild the priority queue from `jobs WHERE status=pending` on startup (TODO: this isn't actually wired — currently a restart leaves PENDING jobs orphaned).
- The read-model would **not** rebuild by itself — an id only moves when an event names it, and a finished job has no more events coming. Run `read_model.rebuild_read_model` (the eval reset already does).
- The delayed queue would lose pending retries (those jobs are stuck in `failed` status with `retry_count < max_retries` until manually replayed).

The orphaned-PENDING-on-restart issue is the second roadmap item in [`docs/ROADMAP.md`](ROADMAP.md).

---

## Operational ops

### Inspect a key

```bash
docker compose exec redis redis-cli
> KEYS 'jobs:tenant:*'                    # don't do this in prod
> SCAN 0 MATCH 'jobs:tenant:*' COUNT 100  # do this in prod
> ZRANGE jobs:tenant:<tid>:status:completed 0 -1        # newest last
> ZCARD jobs:tenant:<tid>:status:completed             # + the :evicted counter
> GET jobs:tenant:<tid>:status:completed:evicted       # = the true count
> ZRANGEBYSCORE delayed_queue 0 +inf WITHSCORES
> GET kafka:consumer_lag:worker-dispatcher
```

### Rebuild the read-model

From the `jobs` table, which is the source of truth — not from `event_log`, which would replay the same at-least-once ordering problems the projector guards against:

```python
from app.workers.read_model import rebuild_read_model

# Whole projection:
await rebuild_read_model(session, redis)
# One tenant, leaving every other tenant's keys untouched:
await rebuild_read_model(session, redis, tenant_id=tenant_id)
```

Run it after a Redis restart or eviction event, after any `saturate_redis` run that tripped eviction, and once after deploying WO-R2-56 (which changes these keys from SETs to ZSETs; the projector drops a stale-typed key rather than raising, so counts are low until the rebuild lands). It deletes the keys in scope and rewrites them, so counts are exact and membership is windowed exactly as the live projector would keep it.

It is not atomic against a running projector: an event landing mid-rebuild can be overwritten by it, self-correcting on that job's next event. `scripts/reset_eval_state.py` runs it as the last step of every eval reset.

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
| `GET /jobs/{id}/stream` (SSE) | Connection opens, sends initial DB state, never streams updates. UI shows static progress. The broker closes open streams when its shared connection dies rather than leaving them silently open; EventSource reconnects. Never a 500. |
| `GET /admin/stats` | Returns whatever was in Redis before the outage (cache or empty). Doesn't 500. |
| `GET /admin/jobs/{id}` | Cache miss, falls through to DB. Slower but works. |
| Worker loop | Delayed retry promotion stops (the loop catches the exception and continues). Active job processing continues — the DB and Kafka are the load-bearing components. |

The platform is designed so that Redis is a **performance + UX** dependency, not a **correctness** one. The truth is in Postgres + Kafka.

---

## Pointers

- `backend/app/utils/rate_limit.py` — per-client rate limiting
- `backend/app/utils/admission.py` — `check_job_admission`, the shared guard every job-creating endpoint runs
- `backend/app/utils/quota.py` — per-tenant rate limit + monthly quota
- `backend/app/utils/cache.py` — `JobCache`
- `backend/app/utils/backpressure.py` — `check_backpressure`
- `backend/app/workers/read_model.py` — CQRS read-model projector
- `backend/app/workers/progress.py` — Pub/Sub publish + retained snapshot
- `backend/app/workers/progress_broker.py` — one shared Pub/Sub connection fanned out to every open SSE stream; the stream cap and timeouts
- `backend/app/workers/queue.py` — priority queue + delayed queue
- `backend/app/api/streaming.py` — SSE endpoint reading Pub/Sub through the broker
- `backend/app/core/redis.py` — the two pools: shared (20) and SSE-dedicated (`SSE_REDIS_MAX_CONNECTIONS`)
- `infra/elasticache.tf` — production ElastiCache provisioning
