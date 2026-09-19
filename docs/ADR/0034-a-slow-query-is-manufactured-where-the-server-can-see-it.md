# ADR 0034 — A slow query is manufactured where the database server can see it, not where the application would feel it
*Status: Accepted · 2026-09-19 · WO-R3-218 (plan v2.1 WP-8.2, Family A world A1)*

## Context

Family A asks the agent to tell three faults apart from read tools alone. This
packet is A1: **queries running slowly while the connection pool is healthy** —
the mirror image of A2 (`saturate_db_pool`, [ADR 0031](0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md)),
where the pool is full and each query, once it has a connection, runs at its
normal speed.

The plan states A1's evidence as "`p95_query_ms_1m` high,
`pool_wait_timeouts_1m` ~0" (04 §WP-8.2). **That first reading does not exist and
will not exist.** [ADR 0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md)
decided it and said why: `pg_stat_statements` is enabled nowhere, enabling it
would break the boot of any world whose Postgres command line had not changed
first, and the view could not answer a one-minute percentile even if it were
installed — it is cumulative per normalised statement, with means and maxima and
no percentiles and no window. `p95_query_ms_1m` and `slow_query_count_1m`
therefore ship as `null` in every response, with a reason string.

So this packet's evidence is restated, in the same terms ADR 0030 restated it:

> `longest_active_query_ms` rising and `active_queries_over_slow_threshold` above
> zero, while `pool_wait_timeouts_1m` stays at 0 and `pool_checked_out` is
> normal.

Both halves matter. The pair *is* the discriminator; either reading on its own is
true of more than one fault.

Two facts about the read surface then decide the mechanism, and neither is
negotiable from inside this packet.

**The query readings are server-side; the pool readings are not.**
`longest_active_query_ms` and `active_queries_over_slow_threshold` come from
`pg_stat_activity`, which every connection to the database can see, so they are
the same answer whichever process asks. The `pool_*` fields describe the pool of
**the process that answered the call** — on the agent's surface, the MCP process
([ADR 0006](0006-mcp-server-standalone-process.md)) — and ADR 0030's "what this
does not close" says plainly that a pool held in the API or worker process leaves
them completely unmoved. A fault whose evidence has to reach the agent therefore
has to reach it through `pg_stat_activity`. There is no other channel for query
slowness on this surface.

**A fault the observer cannot survive is not a fault.** ADR 0031 rejected holding
connections in the MCP process for exactly this reason. The same argument applies
here in a different direction: if the delay were injected into the code path the
MCP process uses to answer a tool call, every read the agent takes would slow
down and its own pool counters would move — and A1's signature says the pool
reading is normal. The fault has to be somewhere the reader can watch it without
standing in it.

## Decision

### 1. The mechanism is a real long-running query in the worker process, not an application-level delay

`slow_db_queries` writes one key, `chaos:db_query:slow`, carrying a target and a
chunk length. A chaos-only task in the worker process
(`app/workers/db_slow_query.py`) reads it once a second and, while it is set,
keeps real statements running against the declared relation, each one held open
by a server-side `pg_sleep`.

The work order offered two mechanisms and told us to pick "the one that is
observable and reversible". The cheaper one — a Redis-flagged artificial delay in
the repository layer, mirroring `inject_latency`'s `asyncio.sleep` before a Kafka
poll — **is not observable at all on this platform**, and that is the whole
finding of this packet:

- An `asyncio.sleep` in the application is not a running query. `pg_stat_activity`
  shows nothing, because nothing is executing on the server. `longest_active_query_ms`
  and `active_queries_over_slow_threshold` stay exactly where they were.
- The only readings it *would* move are the ones that do not exist
  (`p95_query_ms_1m`) or that are per-process and therefore invisible from the
  MCP surface.
- So it would ship a fault with no signature — "a fixture defect by 03:161's own
  checklist", in the order's words, and the diagnosis-direction twin of the
  2026-09-07 lesson about a recovery signal the lab cannot produce.

The order names `pg_sleep`'s cost honestly — it "holds a connection and can leave
residue" — and decisions 2 and 3 below are what bound that cost. The trade is
worth taking, because the alternative is a world the agent cannot read.

### 2. Two queries, offset by half a chunk, because one makes the reading a sawtooth

`pg_stat_activity` reports the age of the statement *running now*. One sleeper
looping `pg_sleep(2s)` therefore produces an age that ramps from 0 to 2000 ms and
starts again: for the first 500 ms of every chunk `longest_active_query_ms` is
*below* `slow_query_threshold_ms` and `active_queries_over_slow_threshold` reads
**0**. A quarter of the time, the agent would read a healthy database in a world
declared unhealthy. That is not a flaky test, it is a fixture that is wrong a
quarter of the time.

`SLEEPER_COUNT = 2`, phased half a chunk apart, fixes it by construction: with
*n* sleepers evenly offset inside a chunk, the oldest query in flight is never
younger than `query_ms · (n−1)/n`. Hence the floor on the caller's `query_ms`:
`MIN_QUERY_MS = 1100`, above twice the platform's 500 ms threshold with margin
for scheduling jitter. `test_the_chunk_floor_keeps_the_reading_continuous` pins
that inequality against both constants, so moving either one without the other
fails the build rather than the scenario.

`concurrent_queries` is reported to the caller and is deliberately **not** a
caller argument. It is also the connection count (decision 3), and a dial that
silently trades the fault's visibility against the pool's free floor is a dial
nobody should be offered.

### 3. The sleepers keep `saturate_db_pool`'s free floor, and the pool reading stays normal because of where they run

Each in-flight query holds one connection out of the **worker's** pool. Two of
them, against the stock pool's capacity of 15, is noise; the budget is computed
from the engine's own `pool_size + max_overflow` and reuses ADR 0031's
`MIN_FREE_CONNECTIONS = 4`, so a pool that cannot spare two connections above the
floor runs **none** and logs that it is idle rather than quietly starving the
loops.

What this buys, and it is the point of the whole packet: `pool_checked_out`,
`pool_overflow` and `pool_wait_timeouts_1m` on the agent's surface are the MCP
process's own, and this fault touches the worker's — so they read **normal**, at
the same time as the server-side query readings read **slow**. A1's signature is
not arranged; it falls out of where the fault lives.

Stated rather than papered over: armed together with `saturate_db_pool` on the
stock pool, the two hooks can hold 10 + 2 of 15 connections, leaving three free
rather than four. No Family A world declares that combination — A1 and A2 are
contrasts, and arming both produces a world neither scenario describes — so the
floor is kept per hook and the interaction is recorded here instead of being
solved by arithmetic nobody would read.

### 4. The target is a closed set of the platform's own read paths, and the relation is a lookup, never caller text

`target` is a three-member enum — `job_reads`, `audit_reads`, `outbox_reads` —
each mapping to one relation in `TARGET_RELATIONS`. The statement really reads
it (`count(*)`), so the slow query holds that relation's read lock and a human
looking at `pg_stat_activity` can see *which* read path is slow. An undeclared
target is refused as invalid params before anything is written; a flag value that
names one is looked up in the map and, on a miss, reads as **off**. The relation
name never comes from the flag, which is asserted structurally as well as
behaviourally, so a hand-written key cannot reach the statement text.

Two consequences of reading a real relation, both small and both stated: a
migration taking an exclusive lock on that relation waits up to one chunk, and
the sleeper's snapshot is held for that chunk. `MAX_QUERY_MS = 10_000` is what
bounds both, and it is also the residue bound in decision 5.

### 5. Residue is bounded by the chunk, not by the caller's patience

The order's own tiebreaker was that "a `pg_sleep` still holding a connection at
reset time" does not self-clean the way a Redis flag does. The answer is that no
single sleep is long: the fault is a *sequence* of chunks with the flag re-read
between them, so clearing the key (what `make eval-reset`'s `chaos:*` scan does),
letting the TTL lapse, or restarting the worker stops the next chunk immediately
and leaves at most one in flight — `query_ms`, 2 s by default and 10 s at the
ceiling. A cancelled task also issues the driver's cancel to the server, so a
restart is usually shorter than that.

A single `pg_sleep(ttl_seconds)` would have been one line shorter and would have
left the connection held for up to an hour after the flag was gone, with
`make world-audit` reading a slow database in a world the reset had just declared
clean.

### 6. The task swallows database errors, and therefore never borrows a session

A failed sleep must not stop the fault from being retried, so
`run_one_slow_query` catches and logs. `CLAUDE.md`'s R2-59 rule is that
swallowing a DB error means taking a SAVEPOINT first — because on Postgres the
transaction is now aborted and every later statement on that session fails. This
task takes the stronger option available to it: it opens its own session per
chunk and closes it in a `finally`, so there is no later statement and no caller
to damage. `test_a_failed_query_discards_its_session_instead_of_reusing_it`
proves it through `tests/conftest.py::AbortingSession` (SQLite does not abort, so
the bug is invisible without it), and a structural test pins the signature —
a factory, never a session — so the property survives the next edit.

## Consequences

- One new chaos tool, so `CHAOS_ENABLED=true` serves **38** tools, **15** of them
  chaos; the read tier is unchanged at 16. TTL-bounded, gated by `CHAOS_ENABLED`
  plus `chaos:invoke`, idempotent in fixture identity (one key, so a repeat call
  replaces the fault), and swept by the reset's existing `chaos:*` scan — no new
  pattern, no residue outside that namespace, nothing added to the world-audit
  baseline. A contract delta: the commander re-pins and reblesses.
- No new refusal code reaches the commander's `ChaosClient`, and no new
  `BlastRadius` member: `shared_dependency`, the label `saturate_db_pool` carries
  for the same reason.
- **What this fault does not do**, because a scenario must not grade on it: the
  platform's own jobs still run at their normal speed. Job durations, consumer
  lag, the outbox age and the SLO objectives are unaffected. This is a slow
  *database*, observed through the server's own view of what is running — not a
  slow platform. A world that needs the platform to feel it needs a different
  hook, and building one means putting a delay in the shared request path, which
  costs the transaction-abort risk of decision 6 and depends on traffic the eval
  world does not have. That is a new order, not a line in this one.
- `docker-compose.yml`'s chaos comment, `README.md`'s surface counts, the failure
  mode catalog in `docs/ARCHITECTURE.md` and the key catalog in `docs/REDIS.md`
  move with this change.
- The matched contrast scenario is WO-R3-221 (Family A), which is not this
  packet's; until it ships this hook is a mechanism with a read surface and no
  world. Unlike its two siblings at ADR 0031, it does at least have the read
  surface.

## Links

- Restates the evidence clause of plan v2.1 WP-8.2 in the terms
  [ADR 0030](0030-breaker-state-is-published-and-a-reading-is-never-invented.md)
  left available, and completes the pair whose other half is
  [ADR 0031](0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md).
- Builds on [ADR 0008](0008-chaos-gating.md) (the triple gate),
  [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md) (the statement text
  carries no lab vocabulary, in case a future reading ever exposes query text),
  [ADR 0026](0026-strict-tenant-isolation-and-declared-platform-scope.md) (the
  sleeper's sessions declare `app.tenant_scope`, so the relation read is not
  refused), and [ADR 0027](0027-control-loop-pause-closed-enum.md) (why the task
  is not a twelfth loop).
- Constrains [ADR 0006](0006-mcp-server-standalone-process.md) from the other
  side: because two processes mean two pools, a manufactured fault has to choose
  which process it lives in, and the read surface decides for it.
