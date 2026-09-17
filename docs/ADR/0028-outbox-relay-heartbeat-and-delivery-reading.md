# ADR 0028 — The outbox relay records each pass, and one reading reports delivery
*Status: Accepted · 2026-09-17 · WO-R3-201 (plan v2.1 WP-4.2)*

## Context

[ADR 0027](0027-control-loop-pause-closed-enum.md) made the outbox relay
stoppable, which gave the `jobs_not_progressing` family its second world: jobs
accepted, nothing executing, the backlog sitting in Postgres rather than in
Kafka. The contrast only works if the agent can *see* it. Before this order it
could not — `get_consumer_lag` reports the Kafka half and nothing reported the
Postgres half, so both worlds looked identical from every tool the agent had.

Two problems had to be solved together, not one.

**What the outbox itself says.** `outbox_events` carries everything needed —
`created_at`, `published_at`, `failed_at`, `attempts` — but no read tool exposed
any of it, and `unpublished_stats()` (the CloudWatch feeder) is platform-wide,
undated and returns a fabricated `0.0` age for an empty queue.

**What a quiet platform says, which is nothing.** A relay with an empty queue
publishes nothing, and so does a relay that has stopped. "No recent delivery"
therefore cannot distinguish them, and on a lightly-loaded platform that
ambiguity is the *normal* state. The plan named the missing signal — "relay
heartbeat age" — without saying where it would come from, and the obvious
source is wrong: the relay's CloudWatch gauges are emitted at most once every
60 s (`_OUTBOX_GAUGE_INTERVAL`), so a heartbeat derived from them could not tell
a relay that stopped five seconds ago from one running normally, on a loop whose
poll interval is one second.

## Decision

**1. The relay records each completed pass, in Redis, under `outbox:relay:last_tick`.**
Written by `_outbox_relay_tick` on every pass, whether or not there was anything
to deliver, with a 24-hour TTL. `app/core/outbox_heartbeat.py` owns the key, the
writer and the reader, so the MCP process reads it without importing the worker
package (ADR 0006).

Three sub-decisions inside that sentence, each rejecting the easier option:

- **Inside the tick, after the fetch — not in the loop body.** A stamp written
  before the leader gate and the per-iteration skip check would mean "the
  coroutine is alive", and a loop that keeps turning while skipping its work is
  exactly the state this signal exists to expose. After the fetch, so a
  completed pass also means the queue was reachable.
- **Per pass, not per gauge window.** One second of resolution instead of sixty,
  for one `SET` per second on the leader only.
- **Outside the `chaos:*` namespace.** It is a platform signal with a platform
  writer: the reset script's `chaos:*` sweep must not clear it, and the name of
  the key that records *that* a relay is not running must not hint at *why*
  ([ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)).

**2. The write fails open; the read fails known.** A Redis failure during the
write is logged and the tick continues — the relay exists to deliver events, and
a diagnostic must never cost a pass, the same posture `loop_is_paused` takes. A
failure or absence on the read is reported as `relay_heartbeat_known: false`
with `relay_last_tick_at`, `relay_heartbeat_age_s` null and a reason string. It
is never reported as an age: a fabricated `0` would read as a relay that had
ticked at the instant the caller asked, which is the one answer this signal must
never give.

**3. One `telemetry:read` tool, `get_outbox_status`, takes no arguments and
returns one reading.** Counts and timestamps, never rows: no `limit`, no
`offset`, nothing truncated, so the "never promise completeness you cap" rule is
satisfied by having no cap. `telemetry:read` rather than `incidents:read`
because it is the partner reading to `get_consumer_lag` — one token sees both
halves of the contrast or neither.

**4. Every age is measured against the database's clock.** The counts, the
timestamps and `measured_at` come from a single statement including `now()`, so
an age and the timestamp it was derived from cannot disagree, and neither is
measured against whichever process happened to ask. The relay's recorded pass
time is the one value written on another host's clock; the tool says so, and
clamps a negative age to 0 rather than printing a minus sign.

**5. Delivered means delivered.** `mark_failed` stamps `published_at` as well as
`failed_at` ([ADR 0001](0001-outbox-vs-cdc.md) Decision item 3), so
`last_publish_at` filters on `failed_at IS NULL`. Without that filter a
completely stalled relay reports "published a moment ago" — the worst possible
answer, reached silently. Rows past the attempt cap are counted inside
`unpublished_count` *and* reported separately as
`unpublished_past_attempt_limit`, because a backlog made of them does not drain
however healthy the relay is.

**6. Counts are tenant-scoped; the relay heartbeat is not.** The counts match
every other agent-facing read and the RLS policy underneath. The relay is one
process serving every tenant, so its pass time is platform-wide and the
description says which half is which.

## Rejected

- **Deriving the heartbeat from `OutboxOldestUnpublishedAgeSeconds`.** 60-second
  resolution on a 1-second loop, and it says nothing when the queue is empty —
  which is the case the signal exists for.
- **A `last_tick` column on a table.** A write per second to Postgres to record
  that a poller polled, plus a migration, for a value nothing durable depends
  on. Redis losing it degrades the reading to "unknown", which is already
  handled.
- **Returning the waiting rows themselves.** They are internal plumbing, they
  would need paging, and every question this family asks is answered by counts
  and ages. The tool advertises exactly that.
- **Reporting `oldest_unpublished_age_s: 0` on an empty queue**, as
  `unpublished_stats()` does for CloudWatch. A zero age reads as "a row arrived
  this instant". Null with `unpublished_count: 0` is the honest shape, and the
  description states the correspondence.
- **Widening `get_consumer_lag`.** Two different queues, two different clocks,
  two different failure modes. Merging them would make the discriminating
  comparison a comparison between fields of one response that the agent has no
  reason to trust as independent.

## Consequences

- A tool-surface delta: `master` with `CHAOS_ENABLED=true` serves **32** tools
  (11 chaos). The coordinator cuts a release, the commander re-pins by index
  digest and reblesses `+get_outbox_status`; the commander half of WO-R3-201
  (the `ReadToolName` literal, the registry entry, the output model and the
  `RESOURCE_ARG_FIELDS` classification) follows that release.
- `docs/REDIS.md` gains one key. It is not cleared by the eval reset, on
  purpose: it describes the platform, not a world someone arranged.
- A relay whose Redis is unreachable keeps delivering and stops being
  observable. That is the right trade for a production outbox, and the reading
  says which of the two it is looking at.
- The reading cannot see past its own transaction boundary: it reports what the
  relay recorded as published, not what Kafka retained. The description says so
  rather than implying an end-to-end guarantee it cannot make.
