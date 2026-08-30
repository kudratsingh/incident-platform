# ADR 0022 — The resume sweep selects only promotable work, and a stranded parent cascades CANCELLED to its children

**Status:** Accepted · **Date:** 2026 Q3 · **Owner:** Platform
**Amends:** [ADR 0011](0011-dag-pause-enforcement.md) §2 and its "New always-on loop" consequence

## Context

[ADR 0011](0011-dag-pause-enforcement.md) §2 introduced `_resume_unblocked_waiting_loop` to make a DAG pause temporary rather than terminal. It shipped as:

```sql
SELECT * FROM jobs WHERE status = 'waiting' LIMIT 200
```

with no `ORDER BY`, no cursor, and no eligibility predicate. Each returned row was then tested in Python — `unmet_count(child) > 0`, then the ancestry pause probe — and discarded if it failed.

Three properties of the platform combine badly here, and only the combination is fatal:

1. **A `WAITING` child of a `DEAD_LETTER` or `CANCELLED` parent can never be promoted.** `unmet_count` counts every non-`COMPLETED` parent as unmet, and neither status ever becomes `COMPLETED`.
2. **Nothing removed those rows.** The `CANCELLED` enum comment has said `# saga rollback / dependency parent failed` since the DAG landed, but only the saga half was ever implemented. There was no cascade and no purge, so the blocked set grew monotonically.
3. **The sweep is cross-tenant by design** (ADR 0011 §2), so the candidate set is platform-wide.

Together: once ~200 permanently-blocked `WAITING` rows existed anywhere in the platform, they filled every page the sweep fetched, were rejected one by one, and the sweep promoted nothing for anyone — for as long as the rows existed, which was forever. One tenant's stuck backlog starved every other tenant's held children, and the symptom (children never resuming after a pause lifts) appears nowhere near the cause (an unrelated tenant's dead-lettered DAG).

ADR 0011's own consequence section anticipated pressure here and named the wrong remedy: *"if `WAITING` volume ever grows, the natural fix is an index-backed query on `(status, created_at)` and a smaller limit"*. Neither helps. A smaller limit makes it strictly worse, and any constant limit is eventually swallowed by a set that grows without bound. The limit was never the problem; the *composition of the candidate set* was.

## Decision

### 1. The eligibility test moves into SQL

The sweep now selects `WAITING` jobs carrying `NOT EXISTS (a parent whose status is not COMPLETED)`. Permanently-blocked rows are no longer candidates at all, rather than being fetched and discarded, so `LIMIT` can only ever truncate work a later pass can still promote.

This is the substantive fix. It converts the limit from a cap on *rows examined* into a cap on *promotable rows examined*, which is what a limit on a work queue has to mean to be safe.

### 2. `ORDER BY created_at, id` plus a rotating keyset cursor

A second line of defence, for the one starvation case the predicate cannot see. A DAG pause lives in Redis, so paused children are promotable *in SQL* and legitimately occupy the page; more than `_RESUME_SWEEP_LIMIT` of them under one long pause would re-create the same starvation inside the now-eligible set.

Each pass returns the `(created_at, id)` of the last row it examined and the next pass resumes past it. A short page means the tail was reached and the cursor resets to the oldest row — the "rotating" part, and the thing that keeps the sweep from marching off the end and going permanently blind. `id` is in the sort key because `created_at` is not unique: bulk-created siblings share a timestamp, and a non-deterministic tiebreak would let the cursor skip rows.

The cursor is **per-replica in-memory state, and deliberately not durable**. It is a fairness hint, not a correctness mechanism: two replicas holding different cursors simply scan different pages, and a restart that loses one only means that replica starts from the oldest row again. Nothing is missed by losing it, because §1 already guarantees the candidate set contains only real work.

### 3. A terminal non-COMPLETED parent cascades `CANCELLED` to its non-saga descendants

This stops the stuck set growing at the source, and it is the behaviour the enum comment has advertised all along. `JobRepository.update_status` now cascades when the target status is `DEAD_LETTER` or `CANCELLED`, in the caller's transaction — the same placement, and the same reasoning, as the terminal-event emission from the addendum to [ADR 0001](0001-outbox-vs-cdc.md). A parent cannot be dead-lettered in Postgres while its children stay `WAITING` in the same database.

Three bounds, each load-bearing:

- **`saga_id IS NULL`.** Saga steps belong to `SagaCoordinator`, which cancels them by saga membership (`SagaRepository.waiting_steps`) and not by the dependency DAG ([ADR 0017](0017-saga-compensation-settlement.md)). Excluding them keeps the two mechanisms disjoint: no double-cancel, no race with the coordinator.
- **`status = WAITING`.** A CAS in set form. A child already `RUNNING` or terminal is not ours to touch, and the predicate is what makes a concurrent cascade from a sibling parent idempotent.
- **`FAILED` is not a cascade source.** For the same reason it is absent from `TERMINAL_JOB_STATUSES`: the retry cycle re-enters from `FAILED`, so such a parent is still in flight and may yet complete. Cascading from it would cancel the children of a job that is about to succeed.

The walk continues through cancelled children, because `unmet_count` treats a `CANCELLED` parent as unmet too — stopping at the first level would relocate the stuck set one generation down rather than draining it. It is level-by-level rather than one recursive CTE: `UPDATE … WITH RECURSIVE` is not portable to the SQLite the unit suite runs on, and real job DAGs are single-digit deep. A `_CASCADE_MAX_DEPTH` bound exists so a corrupted edge set cannot spin the worker.

## Consequences

**`CANCELLED` gains a second writer, and it is not saga-scoped.** This is the significant consequence, and it sharpens an accepted limitation rather than introducing a new one. The addendum to [ADR 0001](0001-outbox-vs-cdc.md) exempted `CANCELLED` from the terminal-event invariant on the grounds that its only writer was `SagaCoordinator._handle_failure`, "so the saga side stays coherent". That justification does not extend here: cascaded plain-DAG children have no coordinator behind them, so they leave stale ids in the CQRS read model and hold their SSE streams open, with nothing to reconcile them. The `job.cancelled` topic in [`docs/ROADMAP.md`](../ROADMAP.md) is the fix and its priority rises accordingly. Until then, the `error_message` written on the cascaded row (`dependency parent <id> ended in <status>`) is the only trace an operator gets of why a child left the DAG.

**Rows that used to accumulate now settle.** Existing blocked rows already in the database are *not* retroactively cancelled — the cascade fires on transition, not on inspection. They are, however, no longer harmful: §1 excludes them from the candidate set, so they are inert rather than starving. A backfill was considered and rejected below.

**`create_stuck_dag` still works, and it is worth knowing why.** The chaos hook manufactures exactly the state §3 eliminates, and its docstring calls it "the platform's real stuck mode". It survives because it inserts rows directly with a terminal status rather than transitioning a parent through `update_status`, so the cascade never fires on it. That is a genuine property of the chokepoint, not luck — but the hook's stated rationale ("the platform's own rules" strand these children) is now true only of directly-inserted rows, and should be re-worded when that hook is next touched.

**The sweep's cost profile is unchanged in the good case and better in the bad one.** The `NOT EXISTS` is a correlated semi-join against `job_dependencies.depends_on_job_id`, which is already indexed, and `jobs.status` is indexed. ADR 0011's suggested `(status, created_at)` composite index is now the natural next step if the sweep ever shows up in slow queries, since the `ORDER BY` is new — but it is not required at current scale and is not added speculatively here.

## Alternatives considered

**Raise `_RESUME_SWEEP_LIMIT`.** Rejected, explicitly, and it is the obvious wrong answer. The blocked set has no upper bound and nothing that removes it, so any constant is eventually swallowed; raising the limit buys time proportional to nothing and makes each starved pass more expensive.

**Purge or archive blocked `WAITING` rows on a timer.** Deletes user-visible job history to work around a scheduler bug, and picks an arbitrary age threshold that is wrong for any DAG legitimately paused longer than it. The cascade gives those rows a correct terminal status instead of removing the evidence.

**Cascade from the sweep rather than from `update_status`.** Tempting — it needs no new writer in the repository — but it fires on rows the sweep merely *observes*, which means it would cancel the descendants `create_stuck_dag` deliberately manufactures on the next 10s pass, breaking that hook, its idempotency drift check, and the eval scenarios built on it. Transition-time is also simply the correct moment: the cascade is a consequence of the parent dying, not of anyone later noticing.

**Backfill the existing blocked rows in a migration.** Rejected for this change. The rows are inert once §1 lands, the correct status for a historical row is genuinely ambiguous (some belong to DAGs an operator may still want to inspect), and a data migration that cancels jobs is not something to bundle into a scheduler fix. If the read-model staleness proves noisy, it should ride with the `job.cancelled` topic work, which is when there is an event to announce the change on.

**Cascade `FAILED` too.** Rejected — see §3. It would cancel children of jobs that are mid-retry and about to succeed.
