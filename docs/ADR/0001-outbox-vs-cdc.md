# ADR 0001 — Outbox pattern over CDC for state→event publishing

**Status:** Accepted (Phase 7) · **Date:** 2026 Q1 · **Owner:** Platform

## Context

When a job state change is persisted to Postgres (`UPDATE jobs SET status=...`), we need to publish the corresponding lifecycle event (`job.submitted` / `job.progress` / `job.completed` / `job.failed`) to Kafka so downstream consumers — the SSE bridge, the audit writer, the event-log appender, the CQRS read-model projector, the saga coordinator, the DLQ triage consumer — can observe it.

The naive approach is to publish to Kafka right after the SQL commit. That's wrong: between the commit and the publish, the worker can die. The DB has the new state, the broker doesn't, and there's no recovery — the downstream consumers will never see the event.

We need atomicity between "state changed" and "event will eventually be published".

## Decision

Use the **transactional outbox pattern**:

1. The same DB transaction that mutates `jobs` (or any other table emitting events) also inserts a row into `outbox_events`.
2. A background relay loop (`_outbox_relay_loop` in `app/workers/dispatcher.py`) polls `outbox_events WHERE published_at IS NULL`, publishes to Kafka, then marks the row published.
3. Schema validation happens on `publish_raw`; a SchemaValidationError marks the row failed permanently (`published_at=NOW`, `error_message=...`) rather than retrying forever.

The outbox row is the durable handoff. As long as the DB commit succeeds, the event will be published — at-least-once — without coordination between DB and broker.

## Alternatives considered

### Change Data Capture (Debezium → Kafka Connect)

CDC tails the Postgres WAL and emits a Kafka message for every committed row change. Tempting because it eliminates application-level code entirely: every `UPDATE jobs` automatically produces an event.

**Why not:**
- **Schema coupling.** Our Kafka topics carry semantic events (`job.completed` with `dead_lettered: false`), not row diffs. CDC gives us "row X went from `status=running` to `status=completed`" — we'd still need an application layer translating that into our event taxonomy, defeating much of the win.
- **Operational complexity.** Debezium + Kafka Connect adds two new services to operate (the connector cluster and the connector itself) plus a wal2json or pgoutput plugin in Postgres. Outbox is one polling loop in code we already operate.
- **Replication slot lifecycle.** A stalled Debezium consumer prevents WAL truncation, which can fill the disk on Postgres. Outbox's failure mode is a slowly growing table — same eventual problem, but bounded and queryable.
- **No control over event emission.** With outbox, the application chooses *what* to publish and *when* (e.g. skip emitting on a no-op status update). CDC publishes every committed row diff including ones we don't care about; consumers would have to filter.
- **Lock contention on the WAL.** Multiple consumers tailing the same replication slot is fragile; you typically need one consumer per slot.

### Two-phase commit (XA) between Postgres and Kafka

Coordinated commit across both stores. Kafka has limited XA support; aiokafka doesn't expose it cleanly; Postgres' XA needs `max_prepared_transactions > 0`. Even when it works, the coordinator becomes a single point of failure.

**Why not:** operational nightmare for marginal benefit. Outbox achieves at-least-once with simpler primitives.

### Publish-after-commit, hope for the best

What we'd do if we didn't care about correctness.

**Why not:** the entire reason for this ADR.

## Consequences

### Positive

- **Atomic with the source of truth.** DB commit succeeds → event will be published. Worker crash mid-publish → relay retries on next tick.
- **One operational surface.** The relay is just an asyncio task in the worker process. No new infrastructure.
- **Application-level control.** We choose what events to emit and what payload they carry. No leaky DB schema.
- **Debuggable.** `SELECT * FROM outbox_events WHERE published_at IS NULL` answers "are there events stuck somewhere?" in one query.
- **Per-row failure isolation.** A bad payload that fails schema validation doesn't block the rest of the queue.

### Negative

- **Polling latency.** The relay sleeps `OUTBOX_RELAY_INTERVAL` (1s) between polls. Worst-case latency between commit and publish is one tick. Acceptable; SSE consumers see Kafka events within ~1s of state change.
- **Hot-path write amplification.** Every job mutation costs an extra INSERT. Mitigated by a small JSONB payload and a partial index on `published_at IS NULL` so the polling query is fast.
- **At-least-once, not exactly-once.** Relay crashes between publish and `UPDATE outbox_events SET published_at=...` will republish. Consumers must be idempotent — combined with the `UNIQUE (kafka_topic, kafka_partition, kafka_offset)` constraint on `job_events` and idempotency keys on jobs, this is fine.
- **Outbox table grows unboundedly.** We don't currently truncate. Future PR: archive published rows older than 30 days to S3, drop them from Postgres.

### Reversibility

If we ever outgrow polling latency or want to eliminate the write amplification, switching to Debezium is mechanical — the schema of `job_events` (one row per event, partition+offset keyed) already mirrors what CDC would emit. The outbox relay loop is ~150 lines that can be deleted in one PR.

## Verification

*Both bullets in this section were false as written until 2026 Q3; see the second addendum below for what was wrong and why it mattered.*

- Integration test `test_relay_round_trip_reaches_a_real_consumer` (in `backend/tests/integration/test_outbox_dead_letter.py`) confirms the round-trip against real Postgres and real Redpanda: write outbox row → relay publishes → consumer reads from Kafka.
- The same file's `test_an_unpublishable_row_is_dead_lettered_at_the_cap` and `test_a_healthy_row_behind_a_full_window_of_poison_still_publishes` confirm that a row which can never publish is abandoned rather than retried forever, and that it stops blocking the rows behind it.
- Unit tests in `backend/tests/unit/test_outbox.py` confirm the relay marks rows published only after `publish_raw` returns, and dead-letters them — immediately on `SchemaValidationError`, at the cap otherwise.

## Pointers

- `backend/app/repositories/outbox.py` — outbox row insertion
- `backend/app/repositories/job.py` — `update_status`, the single producer of terminal events (see the addendum below)
- `backend/app/schemas/job_events.py` — the terminal event payload shapes
- `backend/app/workers/dispatcher.py` — `_outbox_relay_loop`
- `backend/app/workers/kafka_producer.py` — `publish_raw` (propagates errors)
- `backend/alembic/versions/b2a8f9c7e103_outbox_events.py` — table + partial index

---

## Addendum (2026 Q3) — terminal events are emitted by the repository, not by each caller

*The decision above is unchanged and remains accepted. This section records how it is now enforced, because the original wording left a gap that four call sites fell into.*

The **Alternatives considered** section argues for outbox over CDC partly on the grounds that "with outbox, the application chooses *what* to publish and *when* (e.g. skip emitting on a no-op status update)". That is a real advantage, but it was read as licence: emission was elective, done by hand at each call site, and nothing failed when a site forgot. Four did.

- `JobDispatcherConsumer._force_dead_letter` wrote `DEAD_LETTER` and an audit row and no event at all. Jobs died silently: the saga stayed `RUNNING` forever, the CQRS read model kept the id pinned in its previous status set, LLM triage never saw the failure and the SSE stream never closed.
- `JobService.resolve_incident` had the identical shape on the `COMPLETED` side — an operator resolving an incident in the admin console changed Postgres and told nobody.
- The retry branch's `queue.push_delayed` was the one call site of four with no `try/except`, so a transient Redis error escaped `_run_job` into the force-dead-letter net above — which is how a job with retries remaining reached the silent path.
- The dispatcher's own guard on that net checked only `DEAD_LETTER`, so a job `_run_job` had already settled `COMPLETED` could be overwritten.

Fixing call sites one at a time invites the fifth occurrence, so the emission moved into the single writer. **`JobRepository.update_status` now writes the matching `outbox_events` row whenever the target status is terminal, in the same session and therefore the same transaction as the status write.** The four sites that already did it correctly no longer hand-write anything; the two that never did are correct for free. The payload is derived from the freshly-read `jobs` row (`app/schemas/job_events.py`), so the event always describes the state that was actually committed, and the sites cannot drift apart from each other again. The one field a caller may still colour is the dead-letter event's human-readable `message`, passed as `event_message=`.

Two deliberate limits:

- **`CANCELLED` is exempt.** It is terminal, but the platform has no `job.cancelled` topic to announce it on, so there is no event to write in the transaction. Its single writer (`SagaCoordinator._handle_failure`) cancels steps of a saga it is already settling, so the saga side stays coherent — but the read model does keep those ids in their previous status set. Adding the topic is a schema-registry entry plus four consumers; tracked in [`docs/ROADMAP.md`](../ROADMAP.md), not smuggled in here.
- **Emission is unconditional, not transition-gated.** Detecting "was this row already terminal?" would need a pre-read `update_status` does not do. A duplicate event is cheap — every consumer is idempotent under the at-least-once delivery this ADR already promises, backed by the `job_events` unique constraint on `(topic, partition, offset)` and `job_id`-keyed read-model sets. A missing event is the expensive one, and it is what this addendum exists to prevent.

The escape hatch the original text described — choosing not to emit — still exists for non-terminal statuses, which is where it was actually useful (a retry writes `PENDING` and its own "retrying" `job.failed` event). It no longer exists for the terminal ones, where every use of it was a bug.

Verification: `backend/tests/unit/test_terminal_event_single_write.py` asserts, against real rows, that each terminal write lands with its event in one transaction — and that both roll back together.

---

## Addendum (2026 Q3) — the failed state described in Decision item 3 now exists

*The decision above is unchanged and remains accepted. This section records that one clause of it was aspirational for two years, what the gap cost, and how it is closed. Everything above the addenda is the original text.*

### What was missing

Decision item 3 says a `SchemaValidationError` "marks the row failed permanently (`published_at=NOW`, `error_message=...`) rather than retrying forever". `kafka_producer.publish_raw`'s docstring repeats the promise from the other side ("so the relay marks the row failed"). [ADR 0002](0002-json-schema-vs-protobuf.md) restates it a third time in its Consequences.

None of it was true. The relay had no such branch, `outbox_events` had no `error_message` column, and `increment_attempts` maintained a counter that no query, alarm or code path ever read. Every failure was treated as transient, so every failure was retried on the next tick, forever.

The **Positive** consequence above — "per-row failure isolation: a bad payload that fails schema validation doesn't block the rest of the queue" — was half right in a way that made the gap easy to miss. The per-row `try/except` does give isolation: one bad payload cannot abort the batch. But isolation without an exit only means the bad row comes back next tick.

### Why it was worse than "some rows retry a lot"

`fetch_unpublished` returns a *fixed* window: the oldest `OUTBOX_RELAY_BATCH` (100) unpublished rows. A permanently unpublishable row does not slow the relay down — it occupies one of exactly 100 slots, permanently. So this is a cliff, not a ramp. At 99 poison rows everything still works. At 100 the window is entirely poison, nothing else is ever fetched, and event delivery stops for every tenant at once — while the API, the database and `QueueDepth` all read perfectly healthy, because `QueueDepth` measures the Redis delayed set and knows nothing about this queue.

One row was enough to get there on its own. A job whose `job.submitted` event can never publish stays `PENDING`; the stale-`PENDING` sweep (`_requeue_stale_pending_loop`, ungated and every 60s) then appends a *fresh copy* of the same unpublishable row. One bad payload reached the 100-row cliff by itself, unassisted.

### What was built

- **`failed_at` + `error_message` columns** (migration `e5c93b7a2d18`). `published_at=NOW` is what lifts a row out of the fetch window, exactly as item 3 specifies, so the partial index on `published_at IS NULL` is untouched. `failed_at` keeps that honest: `published_at IS NOT NULL AND failed_at IS NULL` is a real delivery, and a row that was abandoned can never be mistaken for one that arrived.
- **Two exits from the retry loop.** A `SchemaValidationError` dead-letters on the first attempt — it is deterministic, so the second attempt is pure waste. Everything else dead-letters after `outbox_max_attempts`.
- **`attempts < cap` in `fetch_unpublished`'s predicate**, so a row that reached the cap stays out of the window even if the marking write was lost to a crash.
- **`OutboxUnpublishedDepth` / `OutboxOldestUnpublishedAgeSeconds` gauges and `OutboxDeadLettered`**, emitted by the relay leader, with two CloudWatch alarms (`outbox-relay-stalled`, `outbox-dead-lettered`) and [`rb-outbox-relay-stalled`](../../runbooks/rb-outbox-relay-stalled.yaml). Age rather than depth is the stall signal: a busy system holds many rows for a second each, a stalled one holds three forever.
- **A payload size cap at submission** (`max_job_payload_bytes`, 256 KiB, enforced in `validate_processor_payload` — the one choke point `POST /jobs` and `POST /sagas` share). This bounds the trigger. A >1 MiB event is refused identically by the broker on every retry, and until now any authenticated user could create one through the ordinary API.

### The cap is a backstop, not the mechanism

`outbox_max_attempts` defaults to 900 — deliberately generous, because the relay retries every unpublished row every second, which makes `attempts` a proxy for *seconds of continuous failure* rather than a count of anything row-specific. A broker outage fails every row in the batch, so a tight cap would quarantine an entire healthy backlog over a blip. That is why the deterministic failure gets its own immediate exit: the common poison case never waits for the cap, and the cap can afford to be patient about everything else.

Dead-lettering is quarantine, not deletion. The row keeps its payload and gains a reason; requeueing is one `UPDATE`, and consumers are idempotent under the at-least-once delivery this ADR already promises, so replaying is safe. The runbook has the statement.

### Two corrections to the text above

- The **Verification** section cited an integration test `test_outbox_relay` in `backend/tests/integration/test_kafka_e2e.py`. No test of that name has ever existed in this repo, and `test_kafka_e2e.py` does not touch the outbox at all — it exercises a producer and consumer directly. So the round-trip this ADR is *about* had no end-to-end proof anywhere, which is a large part of how item 3 stayed unimplemented without anyone noticing. The section now cites tests that exist, in the integration tier that [runs on every PR](../../.github/workflows/ci.yml) with a zero-skip census.
- The **Negative** consequence "outbox table grows unboundedly" now has a second reason to be true — dead-lettered rows are kept on purpose — and the same future archival PR covers both. Keep dead-lettered rows out of any archival predicate that assumes `published_at IS NOT NULL` means delivered.

Verification: `backend/tests/integration/test_outbox_dead_letter.py` (real Postgres + real Redpanda; the oversize row is refused by the actual Kafka client, not a mock) and the dead-lettering tests in `backend/tests/unit/test_outbox.py`.
