# ADR 0002 — JSON Schema over Protobuf for Kafka message contracts

**Status:** Accepted (Phase 7 PR #28) · **Date:** 2026 Q1 · **Owner:** Platform

## Context

Every Kafka topic in this platform (`job.submitted`, `job.progress`, `job.completed`, `job.failed`, `job.dlq`) needs a wire contract. Producers must serialize correctly; consumers must reject malformed payloads before they reach business logic; the contract must evolve over time as we add fields (e.g. `tenant_id` was added in Phase 12 without breaking existing consumers).

The two real options for typed messaging at our scale are:

1. **JSON Schema** with a file-based registry (`backend/app/schemas/kafka/*.schema.json`), validated on both producer and consumer sides.
2. **Protocol Buffers** (or Avro) with a binary wire format and either a Confluent Schema Registry or generated stubs in each language.

## Decision

JSON Schema, with:

- One `.schema.json` file per topic, checked into `backend/app/schemas/kafka/`.
- `schema_registry.validate(topic, payload)` called inside `publish_raw` (so the outbox relay sees the validation error and marks the row failed) and inside `BaseKafkaConsumer._process_one` (so a bad redelivered message is dropped rather than poisoning the consumer).
- `additionalProperties: true` on every schema so adding fields is backward-compatible by construction.
- A custom format checker so `format: uuid` actually validates UUID strings (the default jsonschema package treats `format` as documentation only).

## Alternatives considered

### Protocol Buffers

The "correct" answer for high-throughput, strongly-typed messaging. Schema compilation generates typed stubs; the wire format is compact; field-number-based evolution rules are explicit.

**Why not:**
- **Tooling overhead.** We'd need `protoc` in the build, generated Python stubs checked in or generated at install time, and (for the frontend, when it eventually consumes events directly via SSE-of-binary-frames) a TypeScript codegen path. Today the frontend reads decoded events through the SSE bridge — adding proto would mean either decoding server-side and re-encoding to JSON, or a parallel binary path. Not worth it at our scale.
- **Schema Registry as a service.** Idiomatic proto with Kafka uses Confluent Schema Registry, which is another service to operate, secure, and back up. We're a single ECS Fargate region with two Kafka brokers. Adding Schema Registry doubles the broker-adjacent infra footprint.
- **Debuggability.** `kafkacat -t job.submitted` returning binary blobs hurts on-call. JSON in a terminal is readable.
- **No real perf win at our scale.** Payload sizes are ~500 bytes. Protobuf would save us maybe 200 bytes per message. We're producing well under 1000 msg/sec — bandwidth is not the bottleneck.

### Avro

Same family as proto — binary, schema-evolution rules, registry-typically-needed. Same trade-off; same reasons not to.

### No schema validation (just dicts)

The path of least resistance. What we'd do for a prototype.

**Why not:** the entire point of having a schema is to fail loudly when producers and consumers diverge. Two Phase 12 PRs in this repo (`tenant_id` rollout, the read-model leak) were caught early because a missing/malformed field tripped validation immediately rather than corrupting downstream state.

## Consequences

### Positive

- **Zero build-time tooling.** Read JSON, validate, move on.
- **Readable on the wire.** Kafkacat / Redpanda Console show human-readable payloads. On-call doesn't need a decoder ring.
- **Field-based evolution is trivial.** `additionalProperties: true` plus optional-by-default means adding a field never breaks an old consumer.
- **Frontend can consume the same payloads.** The SSE bridge forwards Kafka payloads to Redis Pub/Sub then to the browser unchanged. No re-encoding.
- **Fast unit tests.** Schema validation is a pure function with no I/O — testable with `pytest.raises(SchemaValidationError)` against a literal dict.
- **Producer-side validation catches bugs before publish.** A schema-invalid event from the outbox relay marks the outbox row failed (`error_message=`) rather than poisoning Kafka.

### Negative

- **No language-level type safety.** Producer code passes a `dict[str, Any]`; the schema is the only thing keeping us honest. Mitigated by `mypy --strict` plus tight payload-construction sites (each producer has ~3 call sites).
- **Larger wire payloads.** ~2-3× the byte size of protobuf. Doesn't matter at our throughput.
- **No code generation.** Both sides hand-write field access. Trade-off accepted; the alternative is operating proto tooling for a single-language backend.
- **Schema evolution requires discipline.** A field rename is a breaking change for old consumers because old code reads the old name. Convention: never rename, only add. Documented in `docs/KAFKA.md`.

### When we'd revisit

- Throughput climbs above ~10K msg/sec where binary efficiency starts to matter.
- We add a second backend language that needs strong typing across the wire (e.g. a Go service consuming `job.completed`).
- We outgrow file-based registry — typically when schemas need versioning + compatibility checking on PR (not just on producer-side validation at runtime).

## Pointers

- `backend/app/schemas/kafka/*.schema.json` — the schemas themselves
- `backend/app/workers/schema_registry.py` — `validate()` + format checker setup
- `backend/app/workers/kafka_producer.py` — producer-side validation in `publish_raw`
- `backend/app/workers/kafka_consumer.py` — consumer-side validation in `BaseKafkaConsumer._process_one`
- `backend/tests/integration/test_kafka_e2e.py` — Testcontainers round-trip including schema validation

---

## Addendum (2026 Q3) — the Consequences claim about outbox dead-lettering is now true

*The decision above is unchanged and remains accepted. This section exists because one line of it described behaviour the code did not have.*

Under **Consequences → Positive**, this ADR claims:

> **Producer-side validation catches bugs before publish.** A schema-invalid event from the outbox relay marks the outbox row failed (`error_message=`) rather than poisoning Kafka.

Half of that was always true and is the part this ADR actually decides: `publish_raw` validates before sending, so a schema-invalid event never reaches consumers. That much held.

The second half — "marks the outbox row failed (`error_message=`)" — did not. There was no `error_message` column and no failed state. A schema-invalid outbox row was retried on every relay tick, once a second, indefinitely, while holding a slot in the relay's fixed 100-row fetch window. The claim was inherited from [ADR 0001](0001-outbox-vs-cdc.md) Decision item 3, which specified the same unimplemented behaviour; repeating it here made it look twice-confirmed rather than never-built.

Both are now implemented. A `SchemaValidationError` from `publish_raw` dead-letters the row on its first attempt — `published_at=NOW, failed_at=NOW, error_message=...` — so the sentence above now describes what happens. See [ADR 0001's 2026 Q3 addendum](0001-outbox-vs-cdc.md) for the mechanism, the attempt cap that covers non-schema failures, and the alarms.

One consequence specific to this ADR's subject: a schema violation is the *cleanest* dead-letter case, because it is deterministic. The same payload fails the same way on every future tick, so unlike a broker error there is nothing to gain by retrying and no ambiguity about whether the failure belongs to the row or the infrastructure. That is why it exits on attempt one rather than waiting out the cap — a distinction only producer-side validation makes possible, and a concrete argument for validating before the send that the original Consequences list was reaching for.

Verification: `test_relay_dead_letters_a_schema_invalid_row_immediately` in `backend/tests/unit/test_outbox.py`, and the hundred schema-invalid rows in `backend/tests/integration/test_outbox_dead_letter.py::test_a_healthy_row_behind_a_full_window_of_poison_still_publishes`.
