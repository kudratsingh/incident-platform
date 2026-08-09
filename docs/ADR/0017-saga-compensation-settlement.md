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
- `cancelled_downstream` equals the real cancellation count (1 with one waiting step, 0 with none).

There is no saga E2E in the integration tier, so these coordinator unit tests are the only
automated proof. `backend/tests/unit/test_dispatcher.py::test_run_job_dead_letters_compensation_when_no_processor`
covers the other half of the loop: the `job.dlq` outbox row that triggers settlement.

## Pointers

- `backend/app/workers/saga_coordinator.py` — routing, `_handle_failure`, `_handle_compensation_settlement`
- `backend/app/workers/dispatcher.py` — `_run_job` job lookup and the unregistered-type DEAD_LETTER branch
- `backend/app/repositories/saga.py` — `jobs()` / `completed_steps()` / `waiting_steps()`
- `backend/app/services/saga.py` — saga creation and the stated compensation policy
- `docs/DATA_MODEL.md` — `sagas.status` value set
