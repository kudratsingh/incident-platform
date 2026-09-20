# ADR 0033 — Each process publishes its own pool gauge, and an absent process is not a healthy one
*Status: Accepted · 2026-09-19 · WO-R3-289*

## Context

`saturate_db_pool` holds connections out of the **API and worker** process's pool, because that is the
pool the platform's own work needs — saturating the MCP process's pool would blind the reader instead of
producing a fault ([ADR 0031](0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md)).
`get_postgres_health` reports `pool_size`, `pool_checked_out`, `pool_overflow`, `pool_max_overflow` and
`pool_wait_timeouts_1m` for **the process that answered the call**, which on the agent's surface is the
MCP process, and every one of those field descriptions says so
([ADR 0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md)).

Both halves are correct and together they leave a hole. The hook is real: connections are genuinely
held, acquisitions genuinely time out, the eleven background loops genuinely slow down. And every tool
the agent has reads healthy — the same failure [ADR 0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md)
found for circuit breakers, in the same place, for the same reason: the state is in-process and the
reader is another process (ADR 0006). ADR 0030 named it rather than closing it ("What this does not
close"), ADR 0031 recorded it as its divergence D1, and WP-8.5's `db_pool` scenario has been grading on
downstream effects — dispatch latency, consumer lag, outbox age — because the fault's own signature was
unreadable.

The pattern to copy was already in the tree. A breaker records its state where another process can read
it; the read tells "nothing is open" apart from "nothing is known"; the key is a platform key, so no
`chaos:*` sweep carries it. A pool is the same problem with one difference that matters: a breaker's
state is *latched* — it changes, and between changes the last record is still true — while a pool
reading is a *sample*, true of the instant it was taken and of no other.

## Decision

**1. Every process records its own pool under `pool:state:<process>`, and `<process>` is a closed set.**
`api_worker` (the process serving the REST API and running the eight consumers and eleven loops — one
engine, one pool, the pool the hook holds) and `mcp` (the process serving the tool surface). The record
carries `size`, `checked_out`, `overflow`, `max_overflow`, `wait_timeouts_1m` and `written_at`, which is
the shape `read_pool_stats` already produced for the flat fields plus the time it was produced. A name
outside the pair raises at the write rather than reaching the wire, because `process` is what a caller
keys on.

**2. `get_postgres_health` gains a `pools` group and a `pool_gauges_unknown_reason`, and the existing
`pool_*` fields do not move.** The flat fields are live and describe the answering process; the group is
what every process last published about itself. A caller written against WO-R3-217 reads exactly the
numbers it always did. One entry in the group *is* the answering process's own pool, sampled up to one
cadence earlier — stated in the field description rather than deduplicated away, because the two are
genuinely different readings of the same pool and a caller comparing them is doing something reasonable.

**3. An empty group is `pool_gauges_unknown_reason`, never an empty group on its own.** Three reasons, a
closed set: nothing has published, the store could not be reached, records exist but could not be read.
This is the rule ADR 0030 set for breakers applied to the case that matters more: an empty list that
reads as "no process has a pool problem" is exactly the confident wrong answer the whole order exists to
remove. A process missing from the list has not reported, which is worth noticing and is not evidence
about its pool.

**4. A publisher task per process writes it, on a ten-second cadence, and it is neither chaos-gated nor
pausable.** Not on `metrics.start_metrics_emitter`'s tick: that emitter is a no-op outside production, so
in the environment this reading exists for it never runs. Not on the worker's `_metrics_loop` either:
that loop is a member of [ADR 0027](0027-control-loop-pause-closed-enum.md)'s closed enum, so
`pause_control_loop('metrics')` would silence the one reading that makes another lab hook observable —
a lab mechanism blinding a lab mechanism. So one small task per process, started from that process's
lifespan beside the metrics emitter, publishing at boot and then once per interval. Unlike the holder it
makes visible (`app/workers/db_pool_hold.py`), it is not gated on `CHAOS_ENABLED`: it is a platform
diagnostic, and a diagnostic available only in a lab is a diagnostic nobody can trust in production.

**5. The TTL is a minute, not a day.** Six cadences: enough to ride out a slow pass or a brief Redis
outage, short enough that a process which stops publishing **drops out of the listing** instead of
freezing at its last healthy number. This is the deliberate inversion of `BREAKER_STATE_TTL_SECONDS`
(24 h) and it follows from sample-versus-latched: a breaker's day-old record is still the breaker's
state, where a pool's day-old record is a number about a moment nobody cares about. Absence is then a
reading in its own right — a wedged process is one that stopped reporting — and `reported_age_s` is
carried on every entry so the caller judges freshness rather than trusting the list.

**6. A pool that keeps no counters publishes nothing.** `StaticPool` under the test tier, `NullPool`
anywhere: `read_pool_stats` already answers "unknown, and why", and the honest translation of that into
this namespace is no record. A record of nulls would be a reading nobody can fill, and the flat fields
already have `pool_stats_unknown_reason` for that case in the process that can say it first-hand.

**7. The environment reset does not touch it.** `pool:state:*` is a platform namespace outside
`chaos:*`, so the reset's sweep does not carry it and no step is added
([ADR 0036](0036-the-reset-closes-a-breaker-and-a-registry-it-cannot-restart-honours-it.md)'s counters
stay at eleven plus three). That is correct rather than an oversight: unlike a breaker, nothing here is
*state a scenario set* — it is a live description of a process, republished within a cadence of whatever
the reset did. Clearing it would only create a window in which the platform could say nothing about its
own pools.

## Alternatives considered

**Report the worker's pool from the MCP process by asking Postgres.** `pg_stat_activity` knows how many
backends each application has, so a reading taken anywhere can count connections per client. Rejected:
it counts *connections to the database*, not a pool's state — it cannot say what the pool's `size` or
`max_overflow` is, cannot see an overflow that has not opened yet, and has nothing at all to say about
callers that waited and gave up, which is the counter that separates a saturated pool from a busy one.
It would answer the easy third of the question.

**One key per process instance rather than per process role.** The honest shape if this platform ran two
API replicas: two of them write `pool:state:api_worker` and the listing reports whichever wrote last.
Not built, and recorded rather than solved — compose and the eval world run one of each, the TTL bounds
how long a dead instance's key lingers, and an instance id would make the listing's length
non-deterministic for a grader. If a second replica of a role is ever run, this is the change.

**Publish on every checkout, or on a change in `checked_out`.** The event-driven shape that mirrors a
breaker's state change most closely. Rejected: `checked_out` moves on every request, so this puts Redis
in the hot path of the pool it is measuring, and the failure mode is a saturated pool generating a write
storm about being saturated.

**Ride the worker's `_metrics_loop`, and add a loop to the MCP process for symmetry.** Cheaper by one
task in one process. Rejected for decision 4's reason: that loop is pausable from the lab, and a hook
that can silence this reading is a hook that can make `saturate_db_pool` invisible again.

**Put the group on `get_circuit_breakers`, or in a new tool.** A new tool is a bigger contract delta
(a name, a scope, a count) for a reading that belongs beside the one it corrects, and a caller
investigating a pool already calls `get_postgres_health`. `get_circuit_breakers` is about calls the
platform stopped making, which is a different question.

**Keep the flat `pool_*` fields as the *summary* of every process.** Tempting, and wrong twice: it
changes what five shipped fields mean, and it would have to invent an aggregate — the sum of two pools
is not a pool, and `pool_size` across processes is a number with no referent.

## Consequences

- **This is a contract delta.** `get_postgres_health`'s output gains `pools` and
  `pool_gauges_unknown_reason` plus one `$defs` entry, `PoolGaugeReading`; no tool is added, no scope is
  added, no input changes, and no existing field moves. `saturate_db_pool`'s description changes one
  clause — it used to say a reading taken in the MCP process "does not show this fault", which is now
  false of the group and still true of the flat fields, so it says which. Both are ledgered in
  `CLAUDE.md` and pinned by `backend/tests/unit/test_pool_gauge_shape_delta.py`. A commander-side output
  model with `extra="ignore"` drops new fields silently, so that config needs checking at the re-pin.
- **WO-R3-219's D1 is closed and WP-8.5's `db_pool` scenario can grade on the fault's own signature:**
  the `api_worker` entry's `checked_out` at `size` plus `max_overflow` with `wait_timeouts_1m` climbing,
  while `longest_active_query_ms` stays normal. The downstream effects it graded on instead remain true;
  they are no longer all it has.
- **One new platform Redis key namespace,** `pool:state:*`, catalogued in `docs/REDIS.md`. It carries a
  TTL, so `saturate_redis` can evict it — and the consequence is the honest one: the group empties with
  `pool_gauges_unknown_reason` set, rather than reporting stale pools during a cache outage.
- `written_at` is on the publishing process's clock and `reported_age_s` is computed on the reader's, so
  an age compares two clocks — the same caveat [ADR 0032](0032-a-sticky-kill-re-arms-and-its-window-is-absolute.md)
  records for a sticky kill's deadline, immaterial on one host.
- The reading is up to one cadence stale by construction. A scenario that arms the hook and reads
  immediately can see a clean pool; the hook's own `poll_interval_seconds` already told callers the hold
  lands on a pass, and `reported_age_s` is what lets a reader tell "not yet" from "not happening".
- Cost is one Redis SET per process per ten seconds, and one SCAN plus two GETs per
  `get_postgres_health` call. The call already did database work; the reads fail open.
- `get_postgres_health` now touches Redis, which it did not before. It cannot fail because of it: the
  read is outside the probe's SAVEPOINT, never raises, and answers with a reason — so a Redis outage
  costs the group, not the reading.

## Pointers

- `app/core/pool_state.py` — the key, the closed process set, the TTL and cadence, `publish_pool_state`,
  `record_process_pool`, `read_pool_states`, and the publisher task.
- `app/core/db_pool_stats.py` — where the numbers come from, unchanged by this order.
- `app/mcp/tools/health.py` — `PoolGaugeReading`, the two new output fields, and the description that
  says which question each half answers.
- `app/main.py` and `app/mcp/standalone.py` — the two `start_pool_gauge` calls, one per process, with
  the reason they are not on a metrics tick.
- `backend/tests/unit/test_pool_gauge.py` — absent writer, unreachable store, unreadable record, the
  TTL, the fail-open write, and the publisher's own behaviour.
- `backend/tests/unit/test_pool_gauge_shape_delta.py` — the contract delta, in one place.
- `backend/tests/integration/test_pool_gauge_cross_process.py` — the D1 proof on a real Postgres and a
  real Redis: `saturate_db_pool` armed, the worker's connections held, the reading taken on the MCP side
  with the flat fields still describing the MCP pool.
