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

- Integration test `test_outbox_relay` (in `backend/tests/integration/test_kafka_e2e.py`) confirms the round-trip: write outbox row → relay publishes → consumer reads from Kafka.
- Unit test `test_outbox.py` confirms the relay marks rows published only after `send_and_wait` returns, and marks them failed (not retried forever) on `SchemaValidationError`.

## Pointers

- `backend/app/repositories/outbox.py` — outbox row insertion
- `backend/app/workers/dispatcher.py` — `_outbox_relay_loop`
- `backend/app/workers/kafka_producer.py` — `publish_raw` (propagates errors)
- `backend/alembic/versions/b2a8f9c7e103_outbox_events.py` — table + partial index
