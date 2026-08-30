# ADR 0021 — Processor execution is bounded, and dispatch never blocks the poll loop

**Status:** Accepted · **Date:** 2026 Q3 · **Owner:** Platform
**Amends:** [ADR 0019](0019-stale-running-recovery-sweep.md) §3

## Context

Two findings from two different passes, which turn out to be one failure mode.

`_run_job` awaited `processor(payload, _publish)` with no deadline. `handle_message`
acquired the concurrency semaphore *before* `create_task` — and `handle_message` runs
on the consumer's poll loop, so a saturated worker stopped calling `getmany()`.

Separately, ADR 0019's stale-RUNNING sweep skips `dispatcher.in_flight_job_ids`
unconditionally, because it could not tell a legitimately-slow job from a stuck one
and a false positive dead-letters live work.

Compose them and the platform has a state it cannot leave:

1. Ten long-running jobs take all ten slots.
2. `handle_message` blocks on `semaphore.acquire()`. The poll loop stops.
3. `fetcher_idle_time` passes `max_poll_interval_ms` (300s). The broker evicts the
   consumer from the group. Nothing restarts it — `_supervise_consumer` watches for a
   consumer that has *stopped*, and this one is alive, sitting in `acquire()`.
4. The stale-RUNNING sweep is the only mechanism that reclaims a stuck slot, and it
   skips exactly these ten ids, forever, by design.

Getting there is cheap, which is the part that makes this a P0 rather than a capacity
note. The payload schemas bounded `row_count` (≤ 10⁶) and `chunk_size` (≥ 1)
separately and never bounded their relationship. `process_csv_upload` iterates
`range(0, row_count, chunk_size)`, so its cost is the chunk *count* at ~0.08s of
blocking read each: `{row_count: 1_000_000, chunk_size: 1}` validates at both creation
surfaces, costs one HTTP request, and buys ~22 hours of execution. Ten of them are
comfortably inside the 30/60s rate limit.

The docstring described the blocking acquire as "naturally creating backpressure
against the partition". Kafka backpressure that stops polling is not backpressure; it
is eviction on a timer, and the docstring said so itself in a parenthetical
("otherwise it's kicked from the group") without treating it as a defect.

## Decision

### 1. A hard deadline on processor execution

`settings.job_execution_timeout_seconds` (default 600s), applied in
`_execute_processor` via `asyncio.timeout`. The value sits inside a window with two
load-bearing ends:

- **Below `stale_running_threshold_seconds`** (900s) by a wide margin. The deadline
  must fire first, or the two recovery paths race: the sweep would dead-letter a job
  whose processor is still running and whose own terminal write is still coming.
- **Above the slowest legitimately-bounded job.** After the chunk-count bound below,
  that is ~200s.

Chaos `inject_latency` does **not** enter this window, and ADR 0019's threshold
rationale is imprecise on the point: the hook sleeps the consumer's *poll* loop in
`BaseKafkaConsumer.run`, so its 60s cap delays dispatch, never execution. It stretches
the gap before `started_at`; it cannot stretch the span this bounds.

The deadline raises `JobExecutionTimeout`, deliberately **not** a bare `TimeoutError`.
Processors raise `TimeoutError` themselves whenever an upstream HTTP client gives up,
and that is an ordinary transient failure with retries still owed to it. `asyncio.timeout`
re-raises an inner `TimeoutError` untouched, so `deadline.expired()` is what separates
"we cancelled it" from "it gave up".

### 2. A breach dead-letters immediately — it does not retry

Through `JobRepository.update_status`, so the row and its `job.dlq` outbox event are one
transaction (ADR 0001 addendum). `error_message` is prefixed `Execution timed out:` and
`dead_lettered_by` is `execution_timeout`, so the admin DLQ table can badge it without
an audit join per row (F2-16); the audit row carries `reason: execution_timeout`.

No retry, because the deadline is a function of the payload and a retry does not change
the payload. Three more attempts would spend three more full deadlines, holding a
concurrency slot each, to arrive at the same place — turning a 10-minute waste into a
40-minute one. Dead-lettering routes it instead into the machinery that already exists
for jobs needing a human or agent decision: the DLQ tab, LLM triage, saga compensation,
Tier-1 replay.

`retry_count` is preserved, for ADR 0019's reason: it is the attempt history triage and
the DLQ tab reason about, and this was not an attempt that failed on its merits.

### 3. The concurrency slot is acquired inside the spawned task

`handle_message` claims the id, spawns `_run_and_release`, and returns. The semaphore is
acquired inside that task. Waiting for capacity is now background work the dispatcher
does, not something the consumer does *instead of* polling.

Backpressure comes from two bounded mechanisms that both keep the loop polling:

- **`_MAX_DISPATCH_BACKLOG`** (10 × `MAX_CONCURRENT_JOBS`) caps dispatched-but-unfinished
  tasks. Past it `handle_message` raises `DispatchBacklogFull`, which is the base
  consumer's existing primitive: the offset is not committed and the partition seeks
  back, so the message returns after the worker drains. `getmany()` keeps being called
  throughout, which is the entire difference from the old inline `acquire()`.
- **The deadline** in §1, which means no job holds a slot indefinitely.

The offset still commits at dispatch time, so this adds one window to the two ADR 0019
named: a job may be committed while *queued* rather than executing, and a crash there
leaves it PENDING with no message. That window already has a backstop —
`_requeue_stale_pending_once` re-publishes PENDING rows with no progress — which is why
it is acceptable where the RUNNING window was not.

### 4. The sweep's in-flight exclusion becomes time-bounded

ADR 0019 §3 skipped in-flight ids unconditionally. That was right while execution was
unbounded: the sweep had no way to tell slow from stuck, so it had to assume slow. §1
now draws that line. An in-flight job still RUNNING more than
`_IN_FLIGHT_EXCLUSION_GRACE_SECONDS` (300s) past the threshold is one whose own deadline
should have fired long ago — it is stuck, and the exclusion lapses so the sweep can
reclaim it.

The grace only has to cover the deadline breach plus the dead-letter write it triggers.
Reaping inside that window would fan out a spurious `job.dlq` and then be overwritten by
the write already in flight, which is the exact harm ADR 0019 §3 exists to prevent. Such
a recovery is audited as `reason: stuck_local_job`, distinct from
`worker_crash_recovery`: one means nobody was running the job, the other means something
was and stopped responding.

### 5. The chunk-count bound at the creation surfaces

`CsvUploadPayload` rejects payloads whose `ceil(row_count / chunk_size)` exceeds
`MAX_CSV_CHUNKS` (10,000), via `validate_processor_payload` — so it applies at both
`POST /jobs` and `POST /sagas`, since `SagaStepRequest` does not go through `JobCreate`.

It is the **quotient**, not the product. Bounding `row_count * chunk_size` would have
been exactly backwards: the product is smallest for the pathological shape (10⁶) and
largest for the cheapest legitimate one (10¹¹). The cap is the documented maximum
`row_count` at the default `chunk_size`, so every shape that was reasonable before still
validates, and the worst accepted csv_upload is ~200s.

This is the trigger; the deadline is the backstop. The schema can only bound the
processors whose cost model it knows, and Phase 14's real processors will not be
predictable from their payloads at all.

## Alternatives rejected

**Retry a timed-out job instead of dead-lettering.** Rejected: the deadline is
deterministic in the payload, so a retry re-buys the same breach at the cost of another
slot for another full deadline. See §2.

**Catch bare `TimeoutError` around the processor call.** Rejected: it conflates the
dispatcher's deadline with a processor's own upstream timeout, silently dead-lettering
every transient upstream blip and stripping its retries.

**Keep blocking on the semaphore, but raise `max_poll_interval_ms`.** Rejected: it moves
the eviction threshold without bounding what causes it. A job with no deadline outlasts
any interval, and the larger interval slows every genuine rebalance.

**`consumer.pause()` / `resume()` on the assigned partitions instead of a backlog cap.**
The textbook Kafka answer, and a legitimate future refinement — it applies backpressure
without the redelivery churn of seek-back. Rejected for now as disproportionate: it needs
lifecycle handling across rebalances in `BaseKafkaConsumer`, which every other consumer
group shares, to improve behaviour only under sustained saturation. Recorded as the
revisit trigger below.

**Bound only the payload, with no execution deadline.** Rejected: it fixes the one
processor whose cost we can compute from its payload today and leaves the class of bug
open for every processor after it. The finding is that nothing bounds execution, not that
one schema was too loose.

## Consequences

- A job that overruns 600s now dead-letters where it previously ran to completion. Any
  workload legitimately longer than that must raise the setting (and the stale-RUNNING
  threshold above it, keeping the ordering) rather than expect the old behaviour.
- **A timed-out processor parked in `run_in_executor` releases its slot and its job row
  immediately, but the thread or process it handed work to runs to completion** — Python
  cannot preempt either. `process_csv_upload`'s pool is 4 threads and
  `cpu_processors`' is 4 processes, so repeated deadline breaches leak pool capacity
  even though the dispatcher's own accounting stays correct. The concurrency cap is no
  longer the binding constraint in that scenario; the pool is. Fixing it properly needs
  cooperative cancellation inside the processors (a deadline passed down to
  `_parse_chunk_blocking`), which is Phase 14 work on processors this ADR does not own.
- `DispatchBacklogFull` logs at WARNING and seeks back on every poll while saturated, so
  sustained overload is now noisy by design. That noise is the signal the old behaviour
  swallowed.
- One ADR 0019 test changed meaning: `test_in_flight_and_fresh_running_jobs_are_untouched`
  aged its live job at 2× the threshold to prove the exclusion was absolute. It is no
  longer absolute, so the job is aged inside the grace and the lapse gets its own test.

**Revisit trigger:** if seek-back churn under sustained saturation shows up in consumer
lag or broker metrics, replace the backlog cap with `pause()`/`resume()` on the assigned
partitions — the mechanism this ADR deferred, not a decision it closed.
