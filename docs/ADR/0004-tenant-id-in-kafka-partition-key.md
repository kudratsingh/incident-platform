# ADR 0004 — Composite `{tenant_id}:{user_id}` Kafka partition key

**Status:** Accepted (Phase 12 PR #37) · **Date:** 2026 Q2 · **Owner:** Platform

## Context

Phase 7 introduced Kafka as the durable event log. Every published event used `user_id` as the partition key, which gave us per-user ordering: a single user's `job.submitted` → `job.progress` → `job.completed` events always land on the same partition and are consumed in order by each consumer group.

Per-user ordering matters because consumers rely on it: the read-model projector moves a job through `running` → `completed` by removing from one Redis set and adding to another, which would be wrong if `completed` arrived before `running`. Same goes for the event-log appender, the SSE bridge, etc.

In Phase 12 we added multi-tenancy. Now we want **per-tenant** ordering as well as per-user — for the same reasons, scaled up.

## Decision

Change the partition key from `user_id` to a composite `{tenant_id}:{user_id}` string. Applied across every producer:

- 5 outbox publish call sites in `dispatcher.py`
- The `kafka_producer.publish_job_progress` direct path
- The dependency resolver's child-job promotion
- The saga coordinator's compensation enqueue
- The job service's lifecycle events

That's 9 call sites updated in PR #37.

## Why composite, not just `tenant_id`

Three options were on the table. Letting just `tenant_id` be the key felt cleanest. We rejected it.

- **Just `user_id` (status quo):** loses per-tenant ordering. A platform admin reading the CQRS overview for tenant A might see tenant A's events interleaved with sibling tenants' events on the same partition; consumer-group lag for one tenant masks issues in another.
- **Just `tenant_id`:** preserves per-tenant ordering but kills parallelism *within* a tenant. Every event for tenant Acme lands on the same partition; one big tenant becomes the bottleneck for every consumer group reading Acme's traffic.
- **Composite `{tenant_id}:{user_id}` (chosen):** preserves per-tenant *and* per-user ordering. Within a tenant, partition load distributes across users (typically dozens to thousands). The murmur hash of the composite string maps to a partition; reading consumers process per-user streams in parallel.

The composite key also makes the partition assignment dependent on *both* values: a single user can't accidentally get pinned to one partition if they happen to span tenants (in our model they don't, but the invariant is nice).

## Alternatives considered

### Topic per tenant

Strongest isolation. Tenant A's events live on `job.submitted.acme`; tenant B's on `job.submitted.beta`; consumer groups subscribe with a wildcard.

**Why not:**
- **Topic explosion.** N tenants × 5 lifecycle topics = 5N topics. Kafka manages thousands of partitions per topic without complaint; thousands of *topics* gets uncomfortable.
- **Per-consumer-group state explodes.** Each topic-partition has its own consumer-group offset; consumer-group join/leave time scales with topic count.
- **Cross-tenant aggregation requires consuming from N topics.** The CQRS read-model projector that maintains "all dead-lettered jobs in tenant A" would need to subscribe to a tenant-specific topic at runtime — possible but annoying.

### Different partition key per topic

`job.submitted` keyed by `tenant_id`; `job.progress` keyed by `job_id`. Maximizes parallelism per event type.

**Why not:** breaks the ordering invariant that makes consumers correct. The read-model projector specifically needs `progress` and `completed` for the same job to land in order, which means same partition, which means same key.

### Keep `user_id`, accept the limitation

The cheapest option. Per-user ordering is technically sufficient — the consumers that care about ordering only care within a job (which means within a user). Per-tenant ordering doesn't strictly impact correctness today.

**Why not:** the multi-tenancy story is now first-class. Operational diagnostics like "is tenant Acme behind on event-log writes" need per-tenant lag, which we get for free with a partition-aware key. Plus, when we eventually add per-tenant SLO scorecards and per-tenant alerting, having per-tenant ordering already in place avoids a future migration.

## Consequences

### Positive

- **Per-tenant ordering for free.** A future per-tenant SLO dashboard can read consumer-group lag scoped to a tenant by looking at the partitions where that tenant's events land.
- **No partition-count explosion.** Same topic count as before.
- **Backward-compatible at the consumer level.** Consumers read whatever key they get; no consumer code changed.
- **Per-user parallelism preserved.** Within a tenant, load distributes.

### Negative

- **Single big tenant can still dominate a partition.** If tenant Acme has 1000× the traffic of all others combined, Acme's events occupy most partitions. We don't have a "max partitions per tenant" mechanism. Acceptable for now; future Phase 11 work (stream analytics with materialized rollups) provides the lever.
- **Partition key is a string, not bytes.** Slightly larger key. Negligible.
- **Migration produced a discontinuity in partition assignment.** Pre-PR-#37 events keyed by `user_id`; post events keyed by `{tenant_id}:{user_id}`. Same `user_id` lands on a different partition before vs after. Consumers that rely on partition stickiness (none currently do) would have noticed. Documented; no rollback path needed.

### Reversibility

Revert in two PRs: change all producers back to `user_id`, then validate. The consumer-side doesn't care about the key, only the order.

## Pointers

- `backend/app/workers/kafka_producer.py` — `publish_job_progress` uses the composite key
- `backend/app/repositories/outbox.py` — `add()` docstring documents the convention
- `backend/app/workers/dispatcher.py`, `dependency_resolver.py`, `saga_coordinator.py` — the 9 call sites
- `backend/tests/unit/test_kafka_producer.py`, `test_outbox.py` — assertions on key format
