# ADR 0031 — A held pool and a degraded dependency are flagged, not broken
*Status: Accepted · 2026-09-18 · WO-R3-219 + WO-R3-220 (plan v2.1 WP-8.3 + WP-8.4, Family A platform half) · amended 2026-09-19 by WO-R3-322 (owner decision O-31 D4): a job whose every endpoint call failed fails unconditionally, and the flag decides only the injection*

## Context

Plan 01 §7.3's `api_latency` family needs three faults that a reader can tell
apart from one another: a slow query with a healthy pool (A1, WP-8.2), a
saturated pool with normal queries (A2, WP-8.3), and a failing downstream
dependency with a healthy database and cache (A3, WP-8.4). This ADR covers the
two hooks in this PR, A2 and A3.

Three facts about this platform shaped both of them.

**There are two pools, in two processes.** The API and the worker share one
process and therefore one SQLAlchemy pool, along with all eight consumer groups
and all eleven background loops; the MCP server is a separate process from the
same image (ADR 0006) with a pool of its own. `get_postgres_health` is served
from the MCP process, so a pool reading taken naively there describes the pool
*nothing else uses*. CLAUDE.md has said this since Wave 1 and the work order
names it as the packet's correctness question.

**The only registered circuit breaker is `bulk-api-sync`**, created in
`app/workers/async_tasks.py` with `failure_threshold=3` and
`recovery_timeout=30.0`, and its registry is a module-level dict — process-local
memory in the worker (divergence H7). Nothing in the MCP process can see it.

**A chaos hook runs in the MCP process.** Every existing hook that changes the
worker's behaviour therefore does it the same way: write a `chaos:*` Redis key,
and have the code in the *target* process read it once per tick, poll or job
(`kill_consumer`, `inject_latency`, `pause_control_loop`). The flag is also what
makes the fault reversible in three ways instead of one.

## Decision

### 1. The pool that starves is the worker's, and the hook holds it from there

`saturate_db_pool` writes one key, `chaos:db_pool:hold`, carrying a connection
count and a TTL. A chaos-only task in the worker process
(`app/workers/db_pool_hold.py`) reads it once a second and holds that many
sessions open — each with its transaction begun and one `SELECT 1` run, which is
what checks a connection out of the pool and leaves it `idle in transaction`,
the shape a real held connection has.

Rejected: holding the connections inside the MCP process, where the hook itself
runs. That is the pool `get_postgres_health` can introspect without any shared
state, so it is tempting — and it is self-defeating. Every MCP tool call needs a
session from that pool, so a hook that saturates it blinds the agent it is meant
to be observed by: the reading times out instead of reporting saturation. A
fault the observer cannot survive is not a fault, it is an outage.

The consequence is stated rather than worked around: **`get_postgres_health`
must report the API/worker process's pool, not its own**, or this hook has no
observable. That is the same seam as divergence H7 and it belongs to WO-R3-217.

### 2. The clamp always leaves four connections acquirable

`MIN_FREE_CONNECTIONS = 4` — the outbox relay every job depends on, the
dispatcher's claim, and two spare — and `MAX_HELD_CONNECTIONS = 10` caps one
hold. The runtime clamp is `min(requested, capacity - 4)`, computed from the
engine's own `pool_size + max_overflow`, so a smaller pool clamps harder and a
pool that cannot spare a connection holds none at all. On the stock pool
(5 + 10) a default call holds 10 and leaves 5.

What this buys: the loops slow down instead of stopping, which is the difference
between the world the scenario declares and a world where the outbox relay
stopped publishing and nothing the agent reads means what it says.

What it costs, said plainly: a *wait timeout* needs demand to exceed the free
floor for longer than the pool's own 30-second timeout. This hook makes callers
wait to acquire; it does not guarantee `pool_wait_timeouts_1m` moves on an idle
world. The plan's A2 signature asks for both "at or near max" and "wait timeouts
rising" (01 §7.3), and on one shared pool per process those two cannot both be
had without starving the loops. The signature is therefore two thirds
producible from the hook alone and the third depends on the world's traffic;
narrowing the pool in the eval world to close that gap is an owner decision, not
a builder's, and the deferred row says so.

### 3. The pool holder is a lab task, not a twelfth background loop

It is started by `worker_loop` only under `CHAOS_ENABLED`, lives in its own
module, and is deliberately **absent** from `ControlLoopName`. ADR 0027 closed
that enum at eleven members, and its boundary test says a twelfth loop must
become a pausable member rather than an exemption. That rule is about the
platform's own loops: pausing a lab task with `pause_control_loop` would be two
mechanisms for one off switch, and this task already has one — deleting or
expiring its key, which is what the reset does. `test_saturate_db_pool.py`
pins both halves: the gate, and the absence from the enum.

### 4. The downstream fault is a flag on the endpoints, and only `fail` opens the breaker

`degrade_downstream` writes `chaos:downstream:bulk_api_sync` as
`"<mode>:<delay_ms>"`. `process_bulk_api_sync` reads it **once per job** — not
once per endpoint, so a whole fan-out sees one dependency state — and each
simulated endpoint call then either raises a 503 (`fail`) or answers late and
succeeds (`slow`). The failures go through the existing breaker, so the
breaker's own threshold does the tripping; nothing about its state machine is
touched, including the deliberate rule that a nested `CircuitOpenError` is not a
probe outcome.

`slow` does not open the breaker, and the description says so. A mode that
produced latency *and* an open breaker would collapse A3 into a single
undiscriminating fault.

### 5. Under the flag, a sync that synced nothing is a failed job

`process_bulk_api_sync` catches each endpoint's error and reports it in the
job's result payload. No operational tool reads that payload: `get_trace`
returns a job's status, retry count and error message, and `search_traces`
filters on type and status. So with the breaker open and every endpoint
failing, the job still ended `completed` and **every tool the agent has read
"healthy"**. The plan's corroborating evidence,
`search_traces(type=bulk_api_sync, status=failed)`, could not be produced at all.

So while the flag is set, a job whose every endpoint call failed raises, which
routes it into the ordinary failure path: retry with backoff, then the
dead-letter queue. The error message is operational
(`bulk api sync failed: 0 of N endpoints returned a result`) and names nothing
about the lab (ADR 0012 rule 1).

Rejected: making that true unconditionally. It is arguably the more correct
behaviour — a sync that synced nothing is not a success — but it changes a
production processor's outcome for the organic 10%-per-endpoint failure path
(a one-endpoint job would newly fail one time in ten), and that is a platform
decision with its own SLO consequences, not a side effect of a lab packet. It
is filed as a follow-up instead.

## Consequences

- Two new chaos tools, so `CHAOS_ENABLED=true` serves **35** tools, **14** of
  them chaos. Both are TTL-bounded, gated by `CHAOS_ENABLED` plus
  `chaos:invoke`, idempotent in fixture identity (one key each, so a repeat call
  replaces the previous state rather than stacking), and swept by the reset's
  existing `chaos:*` scan — neither adds a pattern and neither leaves residue
  outside that namespace.
- No new refusal code reaches the commander's `ChaosClient`: both hooks refuse
  only as JSON-RPC invalid params.
- A held pool is visible only once `get_postgres_health` reports the worker
  process's pool (WO-R3-217, divergence H7's sibling); an open breaker is
  visible only once breaker state is shared across processes (WO-R3-217,
  divergence H7). Until then both hooks are mechanisms with no read surface,
  which is stated in the PR body rather than papered over.
- `docker-compose.yml`'s chaos comment, the surface counts in `CLAUDE.md`, and
  the failure-mode catalog in `docs/ARCHITECTURE.md` move with this change.

## Amendment — 2026-09-19: the failed job is unconditional (owner decision O-31 D4, WO-R3-322)

Decision 5's follow-up was taken. The paragraph above stands as the record of what
shipped on 2026-09-18 and is not rewritten; what changed on 2026-09-19 is the one
clause that made the outcome depend on the flag.

**`process_bulk_api_sync` now raises whenever a job's every endpoint call failed,
flag or no flag.** The flag still decides the *injection* — `fail` makes each call
return 503, `slow` makes each answer late and succeed — and it no longer decides
the *semantics*: a sync that synced nothing is a failed job because that is what it
is, not because a lab key is set. The error message names the count and stays
operational: `bulk api sync failed: all N endpoint calls failed (0 of N endpoints
returned a result)`. Nothing else about the processor moves. A partial failure is
unchanged — still a completed job with `errors` counted in its result — and the
breaker's threshold, states and probe rule are untouched.

**The SLO consequence, stated rather than discovered.** The objective that moves is
`job_completion_rate` (99% over a rolling 24h window, `app/services/slo.py`): its
denominator is jobs that settled `completed` or `dead_letter`, so a job that used to
land in the first half can now land in the second. Retries do not spend that budget —
only the dead-letter outcome does. On a quiet window one dead-letter is enough for a
14.4× fast burn, which raises a `critical` Alert and the signed webhook with it; that
is the designed behaviour of the evaluation loop, not a new fault, and it is the same
consequence the eval world already ledgers for the flagged path.

`job_dispatch_latency` does **not** move, and the reason is worth writing down because
it is nearly the opposite: `claim_for_running` stamps `started_at` on *every* claim, so
a retried job's recorded dispatch latency is creation → its last attempt's start, not
its first. With the default backoff (`job_retry_backoff_base = 2.0`, so 2s then 4s) the
last attempt starts well inside the 30-second threshold, and `dead_letter` was already
in `_DISPATCHED_STATUSES`. A deployment with a much larger backoff base would eventually
see retried jobs cross that threshold — that is a property of the retry ladder, not of
this change.

**What the organic path costs, arithmetically.** Each simulated endpoint fails
independently 10% of the time, so a run fails entirely with probability 0.1^N: the
default 5-endpoint job once in 100,000 runs, a 1-endpoint job once in 10. Dead-lettering
takes all three runs failing entirely — one job in a thousand for that 1-endpoint case.
The rejection in Decision 5 called this "a platform decision with its own SLO
consequences"; it is, and the owner took it (O-31, 2026-09-19).

**No contract delta.** No tool name, description, schema, scope or `is_idempotent` flag
changes — `degrade_downstream`'s description already said a job whose every endpoint call
failed is itself failed under `fail`, which is still true, so `tools/list` is byte-identical
and there is nothing to re-pin. The DLQ row's shape does not change either; the one moving
value is the text inside `jobs.error_message`, which no tool contract, canned fixture or
grader pins (it appears only inside an archived recorded world, which stays as recorded).

## Links

- Builds on [ADR 0008](0008-chaos-gating.md) (the triple gate),
  [ADR 0012](0012-the-lab-is-invisible-to-the-agent.md) (rule 1 — the job's
  error message), [ADR 0026](0026-strict-tenant-isolation-and-declared-platform-scope.md)
  (the held sessions declare `app.tenant_scope`), and
  [ADR 0027](0027-control-loop-pause-closed-enum.md) (why the holder is not a
  twelfth loop).
- Constrains [ADR 0006](0006-mcp-server-standalone-process.md): a second
  process means a second pool, and a read tool has to say which one it read.
