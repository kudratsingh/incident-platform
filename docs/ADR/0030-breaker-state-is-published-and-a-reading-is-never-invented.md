# ADR 0030 — Breaker state is published where every process can read it, and no reading is invented to fill a promised field
*Status: Accepted · 2026-09-18 · WO-R3-217 (plan v2.1 WP-8.1), platform half*

## Context

Plan v2.1's Family A (API latency) asks the agent to tell three faults apart from
read tools alone: queries running slowly, the database connection pool
saturated, and a downstream dependency failing behind a circuit breaker. Three
readings were specified for it — `get_postgres_health` extended with pool and
query fields, a new `get_circuit_breakers`, and a new `get_slo_status`.

Two of the three could not be built as written, and the verification pass said so
before any code was cut
(`audit-ws/docs/plans/research-buildout-v2.1/DIVERGENCES-2026-09-15.md`, rows H6
and H7). Both are decisions, and both are taken here rather than in the packet
that consumes them.

**H7 — a breaker's state lives in the process that owns it.**
`app/core/circuit_breaker.py` kept its registry in a module-level dict, and the
only registered breaker (`bulk-api-sync`, `app/workers/async_tasks.py`) is
created in the worker. The MCP server is a separate process from the same image
([ADR 0006](0006-mcp-server-standalone-process.md)), so a read tool that walked
that dict would walk its own empty one and report every breaker closed — a
confident answer with nothing behind it. Two of the four promised fields did not
exist either: the breaker held `_opened_at` as a `time.monotonic()` value, which
is meaningless to another process and cannot be rendered as a time, and it never
recorded why it last failed.

**H6 — `pg_stat_statements` is not enabled, and it would not answer the question
if it were.** The extension appears nowhere in this repo, nowhere in `infra/`,
and nowhere in the eval world's compose file; the demo Postgres is a bare
`postgres:16-alpine` with no `shared_preload_libraries`. Enabling it is not a
one-line change: without the library preloaded `CREATE EXTENSION` *fails*, so a
migration that created it unconditionally would break every boot of every world
that had not already changed its Postgres command line — including the eval
world, whose compose file is owned by the other repo and is not this packet's to
edit.

And the deeper problem, which the plan does not state: **`pg_stat_statements`
cannot produce `p95_query_ms_1m`.** The view is cumulative per normalised
statement since statistics were last reset. It exposes `calls`,
`total_exec_time`, `mean_exec_time` and `max_exec_time` — no percentiles, and no
way to narrow any of it to the last minute. A "p95 over one minute" derived from
it would be a percentile across per-statement *means* over an unstated window,
presented as a query-latency percentile over a stated one. That is the
silent-confident-answer failure this repo's description rules exist to stop
(`CLAUDE.md`, "say which clock", "never promise completeness you cap").

## Decision

**1. A breaker publishes its state to Redis, and the read tool reads that.**
`app/core/breaker_state.py` owns one key per breaker,
`breaker:state:<name>`, holding a small JSON record: state, consecutive failure
count, the failure threshold it is counted against, the wall-clock time the state
last changed, the class of the last failure, and when the record was written.
`app/core/circuit_breaker.py` writes it; `app/mcp/tools/circuit_breakers.py`
reads it. The MCP process therefore needs no import of the worker package, which
is the same shape [ADR 0028](0028-outbox-relay-heartbeat-and-delivery-reading.md)
used for the outbox relay's heartbeat.

Sub-decisions, each rejecting an easier option:

- **Redis, not a table.** Breaker state is a runtime reading, not a record of
  truth: it is worth nothing after a restart, it must not survive one, and it
  must cost no migration. A table would also put a write on the failure path of
  the very dependency that is failing.
- **Written on every state change, refreshed at most once a minute while calls
  flow.** A write per call would put Redis in the hot path of a loop whose whole
  job is to stop calling something; a write only on change would leave a record
  that never refreshes, so its age could not be read at all. The TTL is 24 hours,
  matching ADR 0028, and a record that expires makes the breaker *absent* from
  the listing rather than reported closed.
- **The write fails open, the read fails known.** A failed publish logs and is
  dropped — a lost diagnostic must never turn into a failed call. A read that
  cannot reach Redis returns an empty listing *with a reason*, never an empty
  listing that reads as "no breaker is open".
- **The key sits outside `chaos:*`.** It is a platform key, so `reset_eval_state`
  must not sweep it, exactly as ADR 0028 argued for `outbox:relay:last_tick`. It
  is listed in `docs/REDIS.md`'s catalog with its writer, reader and TTL.
- **The wall clock is added beside the monotonic one, not instead of it.**
  `_opened_at` still drives the recovery timeout, because that is a duration and
  monotonic time is the correct clock for a duration. `last_state_change_at` is a
  `datetime` recorded alongside it purely so another process can render it.
- **The failure reason is a class, never a message.** Three values —
  `timeout`, `connection`, `other` — pinned by a test. An exception message can
  carry a job id, a URL, or the name of whatever injected the fault, and the
  agent must never read any of those ([ADR 0012](0012-the-lab-is-invisible-to-the-agent.md)).
  `other` is explicitly documented as not distinguishing a failure the far side
  reported from a bug on this side, because the breaker genuinely cannot tell.

**2. A promised reading the platform cannot take is reported as unknown with a
reason, and the reading it *can* take is added beside it.**
`p95_query_ms_1m` and `slow_query_count_1m` ship as the plan specifies them and
are `null` in this release, with one of three stable reasons saying which case it
is (not Postgres / the extension is absent / the platform keeps no per-minute
history of query timings). They are never a zero: a zero and an unknown are
different facts, and a tool that conflates them fails silently.

Beside them go two readings that need no extension, no new loop and no sampler,
and that move the moment queries slow down:

- `longest_active_query_ms` — how long the longest query *currently running* has
  been running, from `pg_stat_activity`, on the database server's clock.
- `active_queries_over_slow_threshold` — how many queries running right now are
  past `slow_query_threshold_ms` (500 ms, a fixed platform constant reported
  beside the count so the number can be read at all).

Both are server-side, so they are the same answer whichever process asks, which
is the property the pool fields below do *not* have.

**3. The pool fields describe the pool of the process that answered the call, and
say so.** `pool_checked_out`, `pool_overflow`, `pool_size`, `pool_max_overflow`
and `pool_wait_timeouts_1m` are read from the SQLAlchemy pool the answering
connection came from. Every one of their descriptions states that scope. Two
consequences are stated rather than papered over: a pool kind that does not
report its counters (SQLite under tests) reads as `null` with a reason, and the
API and worker processes have pools of their own that this reading does not see.
`pool_wait_timeouts_1m` is counted by a `QueuePool` subclass that records the
moment a checkout times out (`app/core/db_pool_stats.py`), in a rolling
sixty-second window, per process.

## What this does not close

**The eval world still has no producible `p95_query_ms_1m`.** Enabling
`pg_stat_statements` would not change that (decision 2), so WP-8.2's stated
evidence — "`p95_query_ms_1m` high, `pool_wait_timeouts_1m` ~0" — has to be
restated in terms of readings that exist: `longest_active_query_ms` and
`active_queries_over_slow_threshold` high, pool counters normal. A packet that
ships a fault whose discriminating signal is always `null` would be shipping a
fixture defect, not an agent test.

**A pool saturated in the API or the worker is invisible to a read tool, so
`saturate_db_pool` has no observable yet.** The fields report the reader's own
pool by construction, and the reader on the agent's surface is the MCP process.
State it plainly, because the sibling order has already shipped the hook: **the
MCP process does not read the worker's pool, and nothing in this PR makes it.**
`saturate_db_pool` ([ADR 0031](0031-a-held-pool-and-a-degraded-dependency-are-flagged-not-broken.md))
holds connections in the **worker** process — deliberately, because saturating
the MCP pool would blind the reader instead of producing a fault — and each
process builds its own engine at import (`app/dependencies.py`), so a held worker
pool leaves `pool_checked_out`, `pool_overflow` and `pool_wait_timeouts_1m` on
the read surface completely unmoved. That is WO-R3-219's divergence D1, and it is
still open after this order.

Two consequences follow, and neither is this packet's to take:

- **WP-8.5's `db_pool` scenario cannot grade on the pool fields.** Until a gauge
  exists it has to grade on what the held pool does to *everything else* —
  dispatch latency and the `job_dispatch_latency` objective through
  `get_slo_status`, a climbing `get_consumer_lag`, an aging
  `get_outbox_status` — or on `longest_active_query_ms` staying normal while
  those move, which is exactly the "pool saturated, queries fine" contrast Family
  A wants. A scenario that asserts `pool_checked_out` at its ceiling would assert
  something the lab cannot produce.
- **Closing it means publishing a per-process pool gauge** the way decision 1
  publishes breaker state: each process stamps its own counters where another can
  read them, and `get_postgres_health` reports the worker's alongside its own,
  each labelled with the process it describes and the age of the reading. That is
  a new order, not a line in this one — it needs a writer on a cadence, and this
  platform's cadences are a closed enum
  ([ADR 0027](0027-control-loop-pause-closed-enum.md)).

**`get_slo_status` reports the caller's own tenant; the alert does not.**
`compute_all` measures whatever the session can see, and an MCP session is
tenant-scoped by RLS ([ADR 0015](0015-force-rls-and-nonowner-app-role.md)),
while the evaluation loop that raises the page runs platform-wide. The tool's
description says this, and says that `total: 0` reads as full budget because
there was no traffic — an absence of evidence, not a healthy platform.

## Consequences

- Three tools change: `get_postgres_health` gains twelve output fields, and
  `get_slo_status` / `get_circuit_breakers` are new. With `CHAOS_ENABLED=true`
  the surface goes from 35 tools to 37 (the two chaos hooks of ADR 0031 landed
  first), the chaos half unchanged at 14, and the read tier from 14 to 16. A
  contract delta: the commander re-pins and reblesses.
- One new Redis key namespace, `breaker:state:<name>`, catalogued in
  `docs/REDIS.md` and outside `chaos:*`.
- The default engine pool class changes for non-SQLite URLs, to the counting
  subclass. It changes no pool behaviour; the only new code path records a
  counter when a checkout has already timed out.
- The integration tier grows two files, both in the CI census: one proves a
  breaker opened in one process is visible to a reader whose own registry is
  empty, which is the only test that proves H7 is actually closed, and one proves
  the Postgres branch of the health reading on a real Postgres — including that
  the query fields degrade with the *extension-absent* reason rather than the
  not-Postgres one.
