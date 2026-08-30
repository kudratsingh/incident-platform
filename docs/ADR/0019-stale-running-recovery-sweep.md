# ADR 0019 — Stale-RUNNING recovery sweep dead-letters, never re-publishes

**Status:** Accepted, amended by [ADR 0021](0021-bounded-execution-and-non-blocking-dispatch.md) ·
**Date:** 2026 Q3 · **Owner:** Platform

> **Amendment (ADR 0021, WO-R2-07).** §3's in-flight exclusion is no longer
> unconditional. It was correct while processor execution had no deadline — the sweep
> could not tell a legitimately-slow local job from a hung one, so it had to assume
> slow — but that made a hung local job the one state nothing in the tree could
> reclaim. Now that `job_execution_timeout_seconds` bounds execution, the exclusion
> lapses `_IN_FLIGHT_EXCLUSION_GRACE_SECONDS` past the threshold and such a job is
> recovered with `reason: stuck_local_job`. §3's second paragraph is also imprecise on
> chaos `inject_latency`: it sleeps the *poll* loop, so it delays dispatch, not
> execution, and never counted against the threshold the way the text implies.

## Context

`JobDispatcherConsumer` commits its Kafka offset at dispatch time. `handle_message`
acquires a concurrency slot, spawns `_run_job` as a background task, and returns; the
base consumer commits immediately after. That is a deliberate throughput choice — the
partition is never blocked behind a multi-second job — and the class docstring named
its cost:

> Con: a worker crash between commit-and-completion leaves the job in DB as RUNNING.
> Recovery is left to the outbox pattern in a follow-up.

The follow-up never existed. Nothing in the tree scanned for stale `RUNNING` rows.
`_requeue_stale_pending_once` backstops `PENDING`, `_resume_unblocked_waiting_loop`
backstops `WAITING`, `_promote_delayed_once` backstops the retry ZSET — `RUNNING` had
no backstop at all. So a hard worker crash (SIGKILL, OOM, node loss) stranded up to
`MAX_CONCURRENT_JOBS` = 10 rows in `RUNNING` permanently: the Kafka message that would
redeliver them is already committed, no Redis timer points at them, and no sweep looks
at them. They are invisible to the DLQ tab, to triage, and to saga settlement, and they
poison every later run that reads job state.

The docstring is the sharper part of the finding. It described a recovery mechanism
that did not exist, which is the postmortem-0002 "a docstring is a contract" failure:
the only reader signal said the window was handled.

An orderly shutdown is not affected — `worker_loop`'s `CancelledError` path awaits
`dispatcher.in_flight` before returning, so a graceful stop settles its own jobs. This
ADR is about hard crashes only.

## Decision

### 1. A ninth background loop sweeps stale RUNNING rows

`_stale_running_sweep_loop` runs every 60s and calls `_sweep_stale_running_once`, which
selects `RUNNING` jobs whose `started_at` is older than
`settings.stale_running_threshold_seconds` (default 900s), capped at 100 rows per pass.

The age comparison lives **in the SQL WHERE clause**, with the cutoff computed once as
an aware `datetime`. `started_at` is `TIMESTAMP WITH TIME ZONE`, but SQLite (the unit
test substrate) returns naive datetimes; doing the comparison in Python would raise
`TypeError` the moment aware and naive met. `repositories/job.py:91-95` documents the
same trap for the write side.

### 2. Recovery is DEAD_LETTER, not re-publish

This is the load-bearing decision. A crashed job may have executed an arbitrary prefix
of its processor's side effects. Re-publishing `job.submitted` would re-run that prefix,
and today nothing makes that safe — the atomic `PENDING` claim (`claim_for_running`,
the E1-04 fix) arbitrates *concurrent deliveries*, not *a second execution of a job that
already partially ran*.

Dead-lettering instead routes the orphan into machinery the platform already has:

- the admin DLQ tab shows it,
- `LlmTriageConsumer` classifies it,
- `SagaCoordinator` receives the `job.dlq` event and starts compensation,
- the agent's Tier-1 replay tools can replay it deliberately, with the
  `reason: worker_crash_recovery` audit field distinguishing a crash orphan from a
  job that genuinely exhausted its retries.

So each swept job gets, in its own transaction: `status = dead_letter` with an explicit
`error_message`, an `audit_logs` row (`job.dead_letter`, `extra_data.reason =
worker_crash_recovery` plus `stale_seconds` / `started_at`), and an `outbox_events` row
on `job.dlq` carrying the full `DLQ_EVENT_KEYS` set. The event is a schema-valid
`job.failed` payload with `dead_lettered: true` — a partial payload would fail the
outbox relay's `publish_raw` validation and rot in `outbox_events` with `attempts`
incrementing, and no downstream group would ever hear about the crash.

`retry_count` is **preserved**. Replay deliberately resets it; a crash recovery is not
a replay, and zeroing it would erase the attempt history triage and the DLQ tab reason
about.

### 3. Two exclusions, both mandatory

**In-process live work.** `JobDispatcherConsumer` tracks `in_flight_job_ids`, populated
in `handle_message` *before* `create_task` (there is a scheduling gap between spawning
a task and its first step, and the sweep must not see a just-dispatched job as an
orphan) and discarded in `_run_and_release`'s `finally`. The sweep skips those ids.
Without this the sweep reaps its own live jobs: anything legitimately running longer
than the threshold — chaos `inject_latency`, a large payload — would be dead-lettered
out from under its own processor, which then writes `COMPLETED` over `DEAD_LETTER`
after a spurious `job.dlq` has already fanned out.

**The threshold itself.** It must comfortably exceed the longest legitimate processor
runtime. `inject_latency` caps at 60s per poll; 900s leaves a wide margin. In a
multi-replica deployment the threshold is the *only* protection for a sibling replica's
long-running jobs, since the in-flight set is per-process. The platform runs a single
worker task today; growing to several replicas means revisiting the threshold (or
moving to a lease/heartbeat column) before it means anything else.

### 4. Per-job transactions

Each recovery opens its own session and transaction, mirroring
`_promote_dlq_replay_loop`'s per-item isolation. Batching them would recreate the E1-03
antipattern — one bad row rolling back every recovery beside it. A row whose recovery
raises is logged and left `RUNNING`; the next pass retries it.

The loop is **not** gated behind `CHAOS_ENABLED`. This is production-correctness
recovery, not a chaos hook.

## Alternatives rejected

**Re-publish the orphan via the outbox.** Rejected: unsafe until a job can prove it did
not partially execute. This is the revisit trigger below, not a permanent no.

**Commit offsets only on job completion, with consumer pause/resume.** Rejected as
disproportionate. It would serialize the partition behind up to ten concurrent
multi-second jobs, fight `max_poll_interval_ms`, and require rewriting the dispatcher's
whole concurrency model to fix a recovery gap.

**A "no recent progress" heuristic instead of tracking in-flight ids.** Rejected:
weaker and racier. Progress events are emitted by processors at their own cadence, so a
quiet-but-healthy job is indistinguishable from a dead one; the id set is ground truth.

## Consequences

- Orderly shutdown still drains via `worker_loop`'s `CancelledError` path, so the sweep
  only ever sees hard-crash orphans — plus, on first deploy, any rows historically
  stranded in `RUNNING`. Those flush within one interval: a feature (it clears poisoned
  state) that arrives as a burst of `job.dlq` events and triage calls.
- Sweeping a crashed saga step makes `SagaCoordinator` start compensation. Intended.
  Until the ghost-compensation-job fix (E1-02) lands, such a saga sits in
  `COMPENSATING`; that is a separate defect and is not addressed here.
- Detection latency is `stale_running_threshold_seconds` + up to one 60s pass. The
  threshold is deliberately generous rather than responsive — a false positive
  dead-letters live work, a slow true positive only delays recovery of a job that is
  already lost.

**Revisit trigger:** the atomic `PENDING` claim has landed (E1-04 / WO-P4-03, merged),
which is the substrate a safe re-publish needs. Once a job can be shown not to have
partially executed — an explicit `replay_safe` determination, or an execution-attempt
record the claim can key off — orphans classified that way could be re-published
instead of dead-lettered, and this ADR should be amended rather than replaced.
