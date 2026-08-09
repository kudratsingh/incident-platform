# ADR 0020 — The outbox relay is single-writer via a Postgres advisory-lock leader gate; the sweeps stay CAS-guarded

**Status:** Accepted · **Date:** 2026-08-09 · **Owner:** Platform

## Context

**E1-15.** `worker_loop` is started from the API's own lifespan
(`backend/app/main.py:117`) with no enable flag, so the number of outbox relays running at any
instant equals the number of API replicas. That is two for the whole overlap window of every ECS
rolling deploy today, and permanently more than one once Phase 8 turns on horizontal scaling.

`OutboxRepository.fetch_unpublished` is a plain `SELECT ... WHERE published_at IS NULL ORDER BY
created_at LIMIT 100` with no locking, so both relays read the same rows and both publish them.
The duplicate lands in every consumer of the lifecycle topics: `audit_logs` rows, `job_events`
rows, the SSE bridge, the read-model projector. This is a live defect, not a scaling hypothetical
— it fires on every deploy that has a non-empty outbox.

The relay's shape is what makes this hard, and it is the reason the obvious fix does not work.
One tick is **three transactions** with Kafka round-trips between them
(`backend/app/workers/dispatcher.py`, `_outbox_relay_tick`):

```python
async with session_factory() as session:          # tx 1 — FETCH
    async with session.begin():
        events = await repo.fetch_unpublished(limit=OUTBOX_RELAY_BATCH)
                                                  # tx 1 COMMITS HERE

for event in events:                              # no transaction at all
    await kafka_producer.publish_raw(...)         # network I/O, per row

async with session_factory() as session:          # tx 2 — MARK
    async with session.begin():
        await repo.mark_published(published_ids)
        await repo.increment_attempts(failed_ids)
```

(The relay can read `event.topic` / `event.payload` in the middle block only because
`async_sessionmaker(..., expire_on_commit=False)` — `backend/app/dependencies.py:37` — leaves the
loaded attributes populated after tx 1 commits.)

## The audit's one-line sketch is a non-fix

The audit proposed adding `.with_for_update(skip_locked=True)` to `fetch_unpublished`. Taken
literally — as a change to the repository method alone — **it does nothing**. Row locks live and
die with the transaction that took them, and tx 1 commits before the first `publish_raw` call.
Every lock is released precisely when it would first have mattered, and both relays proceed to
publish the identical batch. Worse than useless: the clause reads like a guarantee, so the next
reader stops looking.

`fetch_unpublished` therefore carries a comment saying exactly this, and deliberately does **not**
carry the clause. Adding a lock that is released before the critical section buys the appearance
of safety and nothing else.

## Options considered

### (A) Hold the fetch transaction open across publish + mark — REJECTED

Collapse the three transactions into one, `SELECT ... FOR UPDATE SKIP LOCKED` at the top. This is
the sketch's first option taken as a *package*, and unlike the half-version it is correct: the
locks are held through the publishes, so a second relay's `SKIP LOCKED` fetch returns the rows
nobody holds.

Rejected because it puts up to `OUTBOX_RELAY_BATCH` (100) row locks and an open Postgres
transaction around 100 sequential Kafka round-trips. A slow or degraded broker — the exact
condition under which the outbox backlog is deepest — then holds a write transaction open for as
long as the publishes take, pinning the xmin horizon against vacuum and burning a pool connection
per relay for the duration. It also makes broker latency a Postgres availability problem, which
is the coupling the outbox pattern exists to avoid. The per-row granularity it buys is worth
nothing here: the relay is a single sequential drain, not a work queue with contending consumers.

### (B) `claimed_at` / `claimed_by` lease columns plus a reaper — REJECTED

Two-phase claim: `UPDATE outbox_events SET claimed_by = :me, claimed_at = now() WHERE
published_at IS NULL AND (claimed_at IS NULL OR claimed_at < now() - interval)` returning the
claimed ids, then publish, then mark. No long transaction, and it generalizes to N parallel relays
later.

Rejected for cost against benefit today. It needs a new migration (two columns plus an index) on
a hot table, a stale-lease reaper with a timeout that has to be tuned longer than the worst-case
batch publish time and shorter than tolerable stall-after-crash, and it introduces a genuinely new
failure mode: a relay that dies mid-publish leaves rows claimed and invisible until the reaper
runs, so the lease timeout becomes a latency floor on crash recovery. That is the right machinery
for parallel relays; we do not want parallel relays, we want exactly one.

### (C) Postgres advisory-lock leader gate around the tick — CHOSEN

At the top of each tick take `pg_try_advisory_lock(OUTBOX_RELAY_LOCK_KEY)`. Hold it for the whole
tick — all three transactions and the publishes between them — and release it in a `finally`. A
relay that does not get it skips the tick and sleeps.

Smallest correct diff, no schema change, no long-lived transaction, no per-row cost, and it states
the actual invariant ("one relay at a time") rather than approximating it with row-level
mechanics. Non-blocking (`try` rather than the blocking form) because the loser has nothing to
wait *for*: it polls again in a second, and a queue of blocked ticks would only convert
contention into a backlog of redundant work.

## Decision

### 1. The relay is leader-gated; leadership is per tick

`app/core/leader_lock.py` provides `advisory_leader_lock(session_factory, key)`, an async context
manager yielding whether this process is the writer. `_outbox_relay_loop` wraps every tick in it.
The lock is acquired and released each tick rather than held across ticks, so leadership follows
whoever is alive: a task that dies or is drained during a deploy hands over on the survivor's next
poll, at most `OUTBOX_RELAY_INTERVAL` (1s) later.

Key is `OUTBOX_RELAY_LOCK_KEY = 0x6F7574626F78` (ASCII `outbox`), a module constant. It shares the
single global advisory-lock space with `MIGRATION_LOCK_KEY` (`0x616C656D626963`, ASCII `alembic`,
ADR-less but documented in `app/core/migration_lock.py`); the keys are disjoint and a unit test
asserts it, because a collision would silently make `alembic upgrade head` and the relay exclude
each other.

### 2. Session-level, on one explicitly pinned connection

`pg_try_advisory_lock`, not `pg_try_advisory_xact_lock`: a transaction-scoped lock would be
released at tx 1's commit — in the middle of the window it exists to protect. This is the same
reason `app/core/migration_lock.py` takes a session lock around alembic, whose transaction
boundaries are likewise internal.

The consequence is the trap that dominates the implementation: **session-level advisory locks are
scoped to a connection**, and the async engine hands out pooled ones. A lock taken on whatever
connection `session.execute()` happened to check out is not a lock — the next statement may run on
a different connection, "releasing" a lock the process never held while the real one leaks until
that pooled connection is recycled. Holding the `AsyncSession` object across the tick is *not*
sufficient either: a `Session` returns its connection to the pool at every commit, and this tick
commits twice.

So the gate calls `engine.connect()` and holds that one `AsyncConnection` for the lock's entire
lifetime: acquire, the caller's whole tick, release. Two details inherited from the alembic work:

- SQLAlchemy 2.0 autobegins a transaction on the first `execute()`. The gate commits it
  immediately — the lock is session-scoped and outlives transaction boundaries, and leaving it
  open would park the connection in `idle in transaction`, holding a snapshot back from vacuum,
  for the whole tick.
- If the release statement fails, the connection is `invalidate()`d rather than returned to the
  pool. A pooled connection that still holds the lock would lock every replica out of the relay
  until it happened to be recycled; closing the underlying backend makes Postgres drop the lock.
  (Crash safety comes free the same way: a process that dies loses its connection and its lock.)

### 3. Fail-open when there is no Postgres

`advisory_leader_lock` yields `True` unconditionally on a non-Postgres dialect, and yields `True`
with a warning if the engine cannot be resolved from the session factory. The unit and API tiers
run one process against in-memory SQLite, which has no advisory locks and nothing to be excluded
from. Fail-open, not fail-closed, is deliberate: a relay that stops publishing is a worse outcome
than a duplicate publish, which the second line of defense below already absorbs.

### 4. The background sweeps are NOT gated — WO-P4-03's CAS covers them

`_resume_unblocked_waiting_loop` and `_requeue_stale_pending_loop` stay ungated. Both were made
duplicate-safe by compare-and-set in WO-P4-03, and a CAS is a strictly better guarantee than a
lock because it holds against *any* concurrency, including a Kafka redelivery that no leader gate
would ever see:

- The resume sweep promotes through `JobRepository.promote_waiting_to_pending`, a conditional
  `UPDATE ... WHERE status = WAITING`. A second sweeper loses the CAS and skips the outbox add
  with it, so the duplicate `job.submitted` is never minted.
- The stale-PENDING sweep can emit a duplicate `job.submitted`, by design and independently of
  replica count (it also fires when consumer lag exceeds the staleness window). The duplicate is
  absorbed by `JobRepository.claim_for_running`, which lets exactly one delivery execute.

Concurrent sweeps are therefore wasted scans, not incorrect ones. Gating them would be cheap but
would trade a real property (correct under redelivery) for a narrower one (correct under
multi-replica) and invite the belief that the CAS is now redundant. Both docstrings say this.

## Non-goals

- **Consumer-side dedup is not replaced.** The `job_events` unique constraint and the WO-P4-03
  atomic claims remain the second line of defense and must not be removed on the strength of this
  gate. The outbox is at-least-once by construction (ADR 0001): a relay that publishes and dies
  before `mark_published` republishes on the next tick, gate or no gate. The gate removes the
  *systematic* duplication of the entire backlog on every deploy; it does not make delivery
  exactly-once.
- **No dedicated worker deployable.** Splitting the relay and the sweeps out of the API task into
  their own single-instance ECS service is the real end state and the audit's third option, but it
  is a Terraform/ECS topology change, not a patch — it belongs to Phase 8 alongside autoscaling
  and blue/green. When it lands, the gate becomes redundant for the relay but stays correct and
  costs one round-trip per second; keep it until the worker deployable is genuinely single-instance
  *and* deploys without overlap, which blue/green by definition is not. `docs/ROADMAP.md` records
  the subsumption.
- **No parallel relays.** Nothing here is a step toward N relays sharing the outbox. If throughput
  ever demands that, option (B)'s lease columns — or the Debezium CDC migration already on the
  roadmap, which retires the polling relay entirely — is the path, and this ADR is superseded.

## Consequences

- Outbox publish throughput is now bounded by one process regardless of replica count. At
  `OUTBOX_RELAY_BATCH = 100` per second per leader this is far above current load; if it ever
  binds, raise the batch size before reaching for parallel relays.
- One extra pooled connection is checked out per relay tick per replica, for the duration of the
  tick, plus two round-trips (`lock`, `unlock`). Followers pay one round-trip and release
  immediately.
- A leaked lock — release failing in a way that also defeats `invalidate()` — would stall the
  relay platform-wide until the holding backend goes away. The invalidate-on-failure path and
  connection-death release make this narrow, and it is the accepted residual risk of (C).
- The unit tiers cannot observe any of this. `advisory_leader_lock` is injectable into
  `_outbox_relay_loop` as `leader_gate` so the leader/follower branches are testable on SQLite,
  and mutual exclusion itself is proved in
  `backend/tests/integration/test_outbox_relay_concurrency.py` (Testcontainers Postgres, skipped
  without Docker), which races two ticks over ten rows and asserts the two publish sets are
  disjoint and cover all ten — plus a control that bypasses the gate and reproduces the twenty
  publishes HEAD produced.

## References

- [ADR 0001](0001-outbox-vs-cdc.md) — why there is a polling relay at all, and its at-least-once
  contract
- [ADR 0009](0009-consumer-lifecycle-and-supervision.md) — the worker's task supervision model
- `backend/app/core/migration_lock.py` — the same session-lock discipline applied to alembic
- `docs/ARCHITECTURE.md` §"The worker loop — what runs where"
