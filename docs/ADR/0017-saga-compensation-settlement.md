# ADR 0017 — Saga compensation steps are real jobs, and a COMPENSATING saga settles COMPENSATED or FAILED

**Status:** Accepted · **Date:** 2026-08-09 · **Owner:** Platform

## Context

Two defects, present since the original DAG+saga commit `ff6a345`, meant saga rollback never
ran and never finished.

- **E1-02 — compensation was announced but never existed.** `SagaCoordinator._handle_failure`
  published a `job.submitted` outbox row per already-completed step with
  `"job_id": str(uuid.uuid4())` and wrote **no `jobs` row**. The dispatcher loads the job by id
  before doing anything (`_run_job`, `backend/app/workers/dispatcher.py`); a random uuid matches
  nothing, so it logged `job not found, skipping` and dropped every compensation job on the
  floor. Compensation had never executed in any environment.
- **E1-02 (second half) — no exit from COMPENSATING.** `SagaStatus.COMPENSATED` was assigned
  nowhere in the codebase, and `handle_message` early-returned for any saga whose status was not
  `RUNNING`. Even if a compensation job had run, its terminal event would have been discarded.
  `SagaStatus.FAILED` was likewise assigned nowhere.
- **E1-13 — the audit row lied.** The `saga.compensating` audit entry reported
  `"cancelled_downstream": len(waiting) - 1`, hard-coding the assumption that the failed step is
  itself in the waiting list. `SagaRepository.waiting_steps` filters `WAITING`/`PENDING` and the
  failed step is `DEAD_LETTER` by the time `job.dlq` is consumed, so the count was always one too
  low — and `-1` whenever there was nothing downstream to cancel.

## Decision

### 1. Compensation steps are real `jobs` rows

`_handle_failure` creates a `Job` (`saga_id` set, `type = f"{step.type}.compensate"`,
`status=PENDING`, `priority`/`max_retries`/`trace_id` copied from the step it compensates) via
`JobRepository.create`, and the outbox row publishes **that row's id**.

`BaseRepository.create` adds and flushes inside the ambient `handle_message` transaction, which
is the same transaction the outbox row is written in. Atomicity therefore comes for free and is
the point: there is no window in which an event announces a job that does not exist, and no
orphan `PENDING` row if the transaction rolls back. Copying `trace_id` keeps compensation jobs on
the admin trace-filter path alongside the work they undo.

A consequence worth stating: compensation rows now appear in `SagaRepository.jobs()`,
`completed_steps()` and `waiting_steps()`, and hence in the saga detail view's step list. The
`.compensate` suffix is the discriminator wherever that matters.

### 2. Settlement semantics

A `COMPENSATING` saga settles when **every** `.compensate` job for it is terminal
(`COMPLETED` / `DEAD_LETTER` / `CANCELLED`):

| Outcome | Condition |
|---|---|
| `COMPENSATED` | every compensation step `COMPLETED` |
| `FAILED` | any compensation step `DEAD_LETTER` or `CANCELLED` |

The asymmetry is deliberate. `COMPENSATED` is a *clean* terminal state — it asserts the workflow's
side effects were undone. A saga whose rollback itself dead-lettered has left the system dirty in
a way nobody has repaired; reporting that as `COMPENSATED` would hide exactly the incidents this
platform exists to surface. `FAILED` is the honest label, and it says "a human must look at this".

**This makes `SagaStatus.FAILED` reachable for the first time.** Nothing assigned it before. Any
consumer that treats the saga status set as `running | completed | compensating | compensated`
must be updated — `docs/DATA_MODEL.md` already listed `failed` in the enum, so this closes a gap
between the documented and the actual state machine rather than widening it.

Settlement is recomputed from job statuses on every call, so it is idempotent. Redelivery is
additionally filtered by `handle_message`'s status gate: after settlement the saga is
`FAILED`/`COMPENSATED`, no longer `COMPENSATING`, so a redelivered event falls through to the
no-op branch. There is deliberately no second status write.

### 3. Routing is scoped to compensation-typed events

At `COMPENSATING`, only terminal events **for `.compensate` jobs** are routed (into settlement).
Non-compensation events are ignored. This type check is the idempotency guard for the original
failure path: Kafka is at-least-once, and a redelivered `job.dlq` for the *original* failed step
would otherwise re-enter `_handle_failure` and mint a duplicate set of compensation rows — this
time as real rows that really execute.

Symmetrically, the `RUNNING` branch excludes compensation-typed events, and `_handle_completion`'s
"all jobs COMPLETED ⇒ saga COMPLETED" check stays gated on `status == RUNNING`. That gate is what
keeps compensation rows from being mistaken for forward progress; do not loosen it.

### 4. The audit count is a counter

`cancelled_downstream` is incremented once per `update_status(..., CANCELLED)` that actually
happened, instead of being derived from the length of the waiting list.

## The forcing function now actually forces

No `*.compensate` processor is registered anywhere in this repo today. `_PROCESSORS` is keyed by
`JobType`, and `csv_upload.compensate` is not a `JobType` member, so every compensation job takes
the dispatcher's unregistered-type branch, dead-letters, and — under this ADR — settles its saga
as **`FAILED`**.

That is the intended behavior, not a regression to fix. The documented policy has always been
that applications must define their compensation logic explicitly (`backend/app/services/saga.py`,
`CLAUDE.md` glossary); until now the "forcing function" forced nothing, because the job never ran
and the saga never left `COMPENSATING`. A saga that dead-letters a step will now visibly end in
`FAILED` with a `saga.compensation_failed` audit row naming how many compensation steps failed.
Registering a real compensation processor is what turns that into `COMPENSATED`.

## Addendum (2026-08-30, WO-R2-49 / WO-R2-58) — settlement is drainage, and the rollback order is written down

Two follow-ups. Both are consequences of decision 2 above being stated in terms of *events*
rather than in terms of the *set* the events were draining.

### Settlement is a function of the compensation set, including the empty set

Decision 2 said a saga settles when every `.compensate` job is terminal, but the code only ever
asked that question from a `.compensate` job's terminal event. A saga whose **first** step
dead-letters has no already-COMPLETED predecessor, so `_handle_failure` minted zero compensation
jobs — and zero jobs produce zero events, so nothing ever asked. The saga was set `COMPENSATING`
and stayed there permanently: a non-terminal status field, no terminal audit row, and a frontend
polling for a transition that could not happen.

The rollback that never ran was vacuously correct — nothing completed, so there was nothing to
undo — which is exactly why the right answer is `COMPENSATED` and not a new "nothing to
compensate" status. An empty set is a **drained** set.

`_handle_compensation_settlement` is accordingly renamed `_settle_if_drained`, its
`if not comp_jobs: return` guard is gone, and `_handle_failure` calls it in its own transaction
when it minted nothing. The audit row is the ordinary `saga.compensated` with
`compensation_steps: 0, dead_lettered: 0` — an honest description of what happened, and the row
the read side needs to see a terminal saga.

The call is guarded on "minted nothing" rather than made unconditionally because the rows
`_handle_failure` has just created are `PENDING` by construction: when there *are* compensations
the set cannot be drained yet, and an unconditional call would be a query that can only answer no.

Downstream `WAITING`/`PENDING` steps are still cancelled first — settling at zero changes what
happens after the cancellation loop, not whether it runs.

### Compensation order comes from a column, not a timestamp

`completed_steps()` ordered by `Job.created_at` and the coordinator reversed the result. But
`TimestampMixin.created_at` is `func.now()` = `transaction_timestamp()`, and every step of a saga
is inserted by one `POST /sagas` request — so all steps share one timestamp, the `ORDER BY` is a
total tie, and "most recent success rolls back first" was whatever order the planner happened to
return. The non-goal below about there being no reverse-order *execution* guarantee was about
dispatch; this was weaker than that, because even the *enqueue* order was arbitrary.

A timestamp that cannot distinguish the rows cannot be repaired into a sequence, so the order is
now written at creation: `jobs.saga_step_index`, 0-based, stamped by `SagaService.create_saga`
(Alembic `d1f6a2b940c7`, which backfills existing sagas from `(created_at, id)` — stable, but no
more meaningful than the tie it froze). `.compensate` rows deliberately carry no index: they are
ordered by the steps they undo.

Both queries that return saga steps — `completed_steps()` (the rollback order) and `jobs()` (what
the API returns as a saga's `steps`, and what the detail view renders) — share one ordering
expression, `_STEP_ORDER`, precisely so they cannot disagree: index first, `NULLS LAST` so
compensation rows sit below the steps they undo, then `(created_at, id)` for rows with no index.

**Determinism and declaration order are different requirements, and treating them as one is its own
bug.** The first cut of this change gave `jobs()` a bare `(created_at, id)` sort, reasoning that any
total order beats a tie. It does not: `id` is a random uuid4, so a tied `created_at` plus an `id`
tiebreaker yields an order that is *stable and stably wrong* — every read agrees, and every read
renders step 3 first. That is arguably worse than the accident it replaced, because it looks
deliberate. It was caught by `test_create_saga_returns_chained_jobs`, which had asserted
`steps[0].status == "pending"` since the saga endpoint shipped, and caught it *probabilistically*
(the correct order is one of six), which is its own lesson about tie-dependent tests. Pagination is
the case where determinism alone is genuinely the whole requirement — hence `(clock, id)` there and
`_STEP_ORDER` here.

The same root cause made OFFSET/LIMIT pagination over `created_at` non-deterministic — a row could
appear on two pages or on none — so every paginated repository query now carries an `id`
tiebreaker. Arbitrary, but total, which is all pagination needs.

## Non-goals

- **In-flight steps are still not cancelled.** `_handle_failure` only cancels `WAITING`/`PENDING`
  steps. A step already `RUNNING` when compensation starts keeps running and its later
  completion/dead-letter is ignored (it is not `.compensate`-typed, so it hits the no-op branch).
  Fixing this needs cancellation semantics for in-flight work — a separate decision.
- **No compensation retry/ordering policy.** Compensation jobs inherit the failed step's
  `max_retries` and are dispatched by the normal queue; the reverse-order *enqueue* is not a
  reverse-order *execution* guarantee.
- **No automatic remediation of a `FAILED` saga.** `FAILED` means "needs a human"; replaying the
  dead-lettered compensation job through the existing DLQ replay path is the manual route.

## Verification

`backend/tests/unit/test_saga_coordinator.py`:

- compensation `jobs` rows are created one per completed step with `saga_id`/`trace_id`/`PENDING`
  set, and every published `job.submitted` payload's `job_id` equals the created row's id;
- comp step `DEAD_LETTER` ⇒ saga `FAILED` + `completed_at` + `saga.compensation_failed` audit;
- comp step `COMPLETED` ⇒ saga `COMPENSATED` + `saga.compensated` audit;
- one comp step still `PENDING` ⇒ saga stays `COMPENSATING`, no audit row;
- a redelivered `job.dlq` for the *original* step at `COMPENSATING` creates no rows and emits
  nothing;
- `cancelled_downstream` equals the real cancellation count (1 with one waiting step, 0 with none);
- a dead-letter with **no completed steps** settles the saga `COMPENSATED` on the same tick, with
  both audit rows (`saga.compensating` then `saga.compensated`, `compensation_steps: 0`) and no
  compensation rows minted — and still cancels the downstream `WAITING` steps.

`backend/tests/unit/test_ordering_determinism.py` covers the ordering half: `completed_steps()`
returns declaration order for steps written back-to-front under one shared `created_at`, `jobs()`
returns declaration order with `.compensate` rows last for a saga whose ids descend as its step
indices ascend (the stable-but-wrong case above, pinned deterministically), `SagaService` stamps
`saga_step_index` 0..N-1 in declaration order, and `list_jobs` paginates an entirely tied set with
every row appearing exactly once.

There is no saga E2E in the integration tier, so these coordinator unit tests are the only
automated proof. `backend/tests/unit/test_dispatcher.py::test_run_job_dead_letters_compensation_when_no_processor`
covers the other half of the loop: the `job.dlq` outbox row that triggers settlement.

## Pointers

- `backend/app/workers/saga_coordinator.py` — routing, `_handle_failure`, `_settle_if_drained`
- `backend/app/workers/dispatcher.py` — `_run_job` job lookup and the unregistered-type DEAD_LETTER branch
- `backend/app/repositories/saga.py` — `jobs()` / `completed_steps()` / `waiting_steps()`
- `backend/app/services/saga.py` — saga creation and the stated compensation policy
- `docs/DATA_MODEL.md` — `sagas.status` value set
