# Kafka — topic catalog and consumer ops

This is the canonical reference for every topic, every consumer group, the partition key strategy, and the operational shape of Kafka in this platform. Read this when you're adding a new topic, debugging consumer lag, or wondering why a particular event lands where it does.

For the broader architectural context (where Kafka sits in the runtime topology, what the outbox pattern looks like), see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) and [ADR 0001](ADR/0001-outbox-vs-cdc.md).

---

## Topics

Topic names come from `Settings.kafka_topic_*` so they're configurable; the table below uses the defaults.

| Topic | Producer(s) | Payload shape | Retention | Partition count (dev / prod) |
|---|---|---|---|---|
| `job.submitted` | Outbox relay (originates in `JobService.create_job`, `DependencyResolver`, `SagaCoordinator`) | `JobSubmitted` schema | 7 days | 12 / 48 |
| `job.progress` | `kafka_producer.publish_job_progress` (direct, from `_run_job`) | `JobProgress` schema | 1 day | 12 / 48 |
| `job.completed` | Outbox relay (from `_run_job` success path) | `JobCompleted` schema | 7 days | 12 / 48 |
| `job.failed` | Outbox relay (from `_run_job` failure path, both retry and DLQ) | `JobFailed` schema | 7 days | 12 / 48 |
| `job.dlq` | Outbox relay (from `_run_job` exhaustion or LLM-forced dead-letter) | `JobFailed` schema with `dead_lettered: true` | 30 days | 12 / 48 |

Schemas live in `backend/app/schemas/kafka/*.schema.json` and are validated on both producer and consumer paths. See [ADR 0002](ADR/0002-json-schema-vs-protobuf.md) for the why.

### Why these topics, not one mega-topic

Different lifecycle stages have different retention needs (progress events are high-volume and low-value 24h later; DLQ events are forensic and kept longer), different consumer subscription patterns (the SSE bridge wants all of them; the saga coordinator only wants completed + DLQ), and different partition counts in the future as volumes diverge. Splitting at lifecycle boundaries makes each of those tunable independently.

### `job.progress` is the only direct-publish topic

Every other topic is published via the **outbox relay**: the application writes a row to `outbox_events` in the same DB transaction as the state change, and the relay loop reads it within the next ~1s and publishes to Kafka. This gives us at-least-once delivery without coordinating DB + broker (see [ADR 0001](ADR/0001-outbox-vs-cdc.md)).

`job.progress` is the exception. Progress events fire often (every percentage point of work), don't represent durable state ("the job was 47% done at 10:14:03" is not something we need to recover), and the SSE bridge is the only meaningful consumer. Adding outbox overhead per progress tick would amplify writes to no benefit. So `publish_job_progress` calls the producer directly and swallows broker errors via `_publish` — the worst case is the user's progress bar pauses, never that the system loses state.

---

## Partition key strategy

**Every event is keyed by `{tenant_id}:{user_id}`** — a composite string. The full reasoning is in [ADR 0004](ADR/0004-tenant-id-in-kafka-partition-key.md). Summary:

- **Per-tenant ordering** is preserved. All events for tenant Acme hash to a fixed subset of partitions.
- **Per-user ordering within a tenant** is preserved. All events for the same user within a tenant hash to one partition.
- **Per-user parallelism within a tenant** is preserved. Different users spread across partitions.

Pre-Phase-12 events were keyed by `user_id` only. The migration to composite happened in PR #37 across 9 producer call sites.

---

## Consumer groups

Seven consumer groups run concurrently inside the worker process (`worker_loop` in `app/workers/dispatcher.py`). Each one is its own group, so they all receive every event independently — failure in one doesn't affect the others.

| Consumer group | Subscribes to | What it does | Critical for | Failure mode |
|---|---|---|---|---|
| `worker-dispatcher` | `job.submitted` | Pops the message, runs the processor for that job type, emits progress/completed/failed events | Job execution. If this is down, no jobs run. | The worker is "disabled" and `worker_loop` returns. Other consumers stop. |
| `audit-writer` | All lifecycle topics | Writes `event.*` rows to `audit_logs`. Combined with the existing audit events written by `AuditRepository.log()` from application code, this gives every state change a second, event-sourced record. | Audit completeness. | `audit_logs` table stops growing event-sourced rows. Application-written audit entries continue. |
| `sse-broadcaster` | All lifecycle topics | Bridges Kafka events to Redis Pub/Sub channels that SSE clients read from `GET /jobs/{id}/stream`. | Live UI updates. | Browser progress bars freeze; user reloads page to see latest. |
| `event-log` | All lifecycle topics | Appends every event to the immutable `job_events` table (event sourcing). | `GET /admin/jobs/{id}/timeline`. | The job timeline view stops growing. |
| `read-model` | All lifecycle topics | Maintains Redis-backed denormalized per-tenant + per-user sets keyed by status (CQRS read side). | `GET /admin/stats` returns stale numbers. | Numbers stop updating; reads still work. |
| `dependency-resolver` | `job.completed` | Promotes child jobs from `WAITING` to `PENDING` when their parents complete. | DAG progression. | Jobs with deps get stuck in `WAITING`. |
| `saga-coordinator` | `job.completed`, `job.dlq` | Marks sagas complete or kicks off compensation. | Saga lifecycle. | Sagas stall; compensation doesn't fire. |
| `llm-triage` | `job.dlq` | Calls Claude to classify the failure, writes a `job_triages` row. | DLQ triage column in admin UI shows no analysis. | Triage rows stop appearing; raw error_message still visible to admins. |

Group IDs come from `Settings.kafka_consumer_group_*` so they're per-environment configurable.

### Why a separate group per concern

Each one has independent offsets. If the event-log consumer falls behind because Postgres is slow, the SSE bridge keeps up. If we deploy a buggy version of the read-model projector and have to rewind, we rewind only that group. Coupling them would mean one slow consumer holds back every other surface.

### At-least-once delivery

Every consumer in this codebase commits its Kafka offset **only after** `handle_message` returns successfully (see `BaseKafkaConsumer._process_one`). If the consumer crashes mid-message, Kafka redelivers on next start. Combined with:

- **Job idempotency keys** (`idempotency_key` UNIQUE constraint on `jobs`) — prevent double-execution when the dispatcher redelivers `job.submitted`
- **Event log uniqueness** (`UNIQUE (kafka_topic, kafka_partition, kafka_offset)` on `job_events`) — `IntegrityError` is caught and swallowed in `EventLogConsumer.handle_message`, so redelivery is a no-op
- **Idempotent set operations** in the read-model (`SADD` / `SREM` on Redis sets are no-ops for existing/missing members)
- **Triage `UNIQUE (job_id)`** — second triage call for the same job is a no-op

The effect is **at-least-once delivery with effectively-once consumer effects**.

---

## Local dev: Redpanda

Local dev uses **Redpanda** in `docker-compose.yml`, a Kafka-API-compatible broker. Compose starts it on `localhost:9092`. The integration test (`backend/tests/integration/test_kafka_e2e.py`) uses `testcontainers-redpanda` to spin up an ephemeral broker per test run on a pre-allocated host port.

Useful commands:

```bash
# Console UI (browser, list topics, peek messages)
docker compose up redpanda-console -d   # localhost:8080

# CLI: list topics
docker compose exec redpanda rpk topic list

# Peek messages on a topic
docker compose exec redpanda rpk topic consume job.completed --num 5

# Reset a consumer group offset (use sparingly, only in dev)
docker compose exec redpanda rpk group seek read-model --to start
```

In production we use **Amazon MSK**, provisioned in `infra/msk.tf`. Same `kafka-python` / `aiokafka` client paths; the only thing that changes is `kafka_bootstrap_servers`.

---

## Schema evolution rules

`additionalProperties: true` on every schema means any new field is backward-compatible at parse time: old consumers ignore it. This is the lever we relied on for the Phase 12 `tenant_id` rollout — we shipped producers writing the new field before consumers depended on reading it.

The discipline:

1. **Never rename a field.** Old code reads the old name. Add a new field and deprecate the old.
2. **Never change a field's type.** `int → str` breaks every consumer reading the old type. Add a new field with the new type.
3. **Never remove a required field.** Producers writing the field still work; consumers reading it after a producer drops it get a `KeyError` or `None`. Add new fields freely; deprecate but keep writing old ones until all consumers stop reading.
4. **Required vs optional matters.** A consumer reading an optional field with `value.get("x")` is unaffected by absence; `value["x"]` raises. Be deliberate.

Producer-side validation in `publish_raw` catches schema violations *before* they hit the broker, so a bad payload in a new producer fails loudly in the outbox row (`error_message=`) rather than poisoning a topic.

---

## Operational ops

### Consumer lag

`kafka:consumer_lag:dispatcher` in Redis (TTL 90s) is the cached lag value from the dispatcher consumer's `consumer_lag()` method. The metrics loop emits it every 60s; the backpressure check in `POST /jobs` reads it without ever round-tripping to Kafka. Threshold: `Settings.backpressure_lag_threshold` (default 1000); above that, the API returns 503 with `BackpressureError`.

For lag on the other 6 consumer groups, use Redpanda Console or `rpk group describe <group>`.

### Pause + resume a consumer group

To roll back a buggy projection or to investigate a poison message:

```bash
# Stop the worker process (in ECS: scale the service to 0)
# The consumer group's offsets are committed only after successful handle_message,
# so unprocessed messages will be redelivered when you bring the worker back.
```

To rewind:

```bash
# Reset the read-model group to start replaying from a specific offset
rpk group seek read-model --to <offset>
```

### Add a new consumer group

1. Subclass `BaseKafkaConsumer` in `backend/app/workers/`.
2. Implement `handle_message(topic, key, value, **_kafka_meta)`.
3. Pick a `group_id` and add to `Settings.kafka_consumer_group_<name>`.
4. Instantiate in `worker_loop` in `dispatcher.py`, add to the `consumers` list.
5. Decide whether failures should be idempotent (most are — use a UNIQUE constraint or a SETNX pattern).

The base class handles schema validation, offset management, error handling, and graceful shutdown.

### Add a new topic

1. Add `kafka_topic_<name>` to `Settings` with a default.
2. Add `kafka_consumer_group_<name>` if it'll have a consumer.
3. Write the JSON Schema in `backend/app/schemas/kafka/<name>.schema.json`.
4. Register it in `schema_registry.py` (it's auto-loaded by filename; the topic name in the schema must match `kafka_topic_*`).
5. In production: add the topic + partition count to `infra/msk.tf` (we don't auto-create topics in MSK).

---

## Pointers

- `backend/app/workers/kafka_producer.py` — the producer + `publish_raw` (validation-loud) + `publish_job_progress` (validation-swallow)
- `backend/app/workers/kafka_consumer.py` — `BaseKafkaConsumer` with offset + validation + retry handling
- `backend/app/workers/schema_registry.py` — JSON Schema loading + format checker setup
- `backend/app/workers/dispatcher.py` — `worker_loop` (orchestrates all 7 groups)
- `backend/app/schemas/kafka/*.schema.json` — schema definitions
- `backend/tests/integration/test_kafka_e2e.py` — Testcontainers round-trip
- `infra/msk.tf` — production MSK provisioning
- `docker-compose.yml` — Redpanda for local dev
