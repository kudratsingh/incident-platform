import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from app.config import get_settings
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.repositories.base import BaseRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.job_events import (
    cancelled_event_payload,
    completed_event_payload,
    dlq_event_payload,
)
from sqlalchemy import CursorResult, and_, func, or_, select, update

# The statuses a job never leaves, and the Kafka topic each one announces on.
# Every one of them has a topic: `CANCELLED` was the exception until
# WO-R2-113, and being the exception is exactly what made it dangerous. A
# cancelled job stopped in Postgres while every consumer of the lifecycle went
# on believing whatever it had last been told — the CQRS read model held the
# id in its previous status set forever, the SSE stream stayed open until the
# browser gave up, and the timeline just ended mid-job.
#
# Asserted against `TERMINAL_JOB_STATUSES` in
# `tests/unit/test_job_cancelled_topic_wiring.py`, so the next terminal status
# added to the enum fails a test until it has somewhere to announce on rather
# than inheriting CANCELLED's silence.
_TERMINAL_EVENT_STATUSES = (
    JobStatus.DEAD_LETTER,
    JobStatus.COMPLETED,
    JobStatus.CANCELLED,
)

# The terminal statuses that stamp `completed_at` (WO-R2-114). All of them:
# the column records when the job stopped, and a cancellation stops it. It
# used to omit CANCELLED, so a saga-rollback or cascade cancellation landed
# terminal with `completed_at IS NULL` and nothing could answer "when did this
# stop" for the one class of terminal job an operator is most likely to ask it
# about. `FAILED` is here because a failed job may still be retried and the
# stamp is refreshed on the next terminal write — the pre-existing behaviour,
# unchanged.
_COMPLETED_AT_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.DEAD_LETTER,
    JobStatus.CANCELLED,
)

# The statuses that strand a dependency DAG below them. A parent here will
# never reach COMPLETED under its own power, so every WAITING descendant is
# blocked forever — `unmet_count` counts a non-COMPLETED parent as unmet and
# nothing else ever clears it. Reaching one of these cascades CANCELLED down
# the non-saga descendants (R2-09), which is the behaviour the `CANCELLED`
# enum comment has advertised ("dependency parent failed") since the DAG
# landed but nothing implemented.
#
# `FAILED` is deliberately absent, for the same reason it is absent from
# `TERMINAL_JOB_STATUSES`: the retry cycle re-enters from it, so a `failed`
# parent is still in flight and may yet complete. Cascading from it would
# cancel children of a job that is about to succeed.
_CASCADE_SOURCE_STATUSES = (JobStatus.DEAD_LETTER, JobStatus.CANCELLED)

# Cycle guard for the cascade walk. `job_dependencies` can only point backward
# in time (see the model docstring), so a real DAG terminates long before
# this; the bound exists so a corrupted edge set cannot spin the worker.
_CASCADE_MAX_DEPTH = 50


class JobSort(StrEnum):
    """Which clock `list_jobs` sorts on.

    `CREATED_AT` is submission time — when the caller asked for the work.
    `DEAD_LETTERED_AT` is when the job stopped, which for a DLQ listing is
    the question actually being asked: a job submitted yesterday that died a
    minute ago is a *newer* dead-letter than one submitted an hour ago that
    died three hours ago (WO-R2-53). Backed by `completed_at`, which
    `update_status` stamps on every terminal write, falling back to
    `created_at` for the rows old enough to have neither.
    """

    CREATED_AT = "created_at"
    DEAD_LETTERED_AT = "dead_lettered_at"


class JobRepository(BaseRepository[Job]):
    model = Job

    async def get_by_idempotency_key(
        self, key: str, tenant_id: uuid.UUID
    ) -> Job | None:
        result = await self.session.execute(
            select(Job).where(
                Job.idempotency_key == key, Job.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def get_for_tenant(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Job | None:
        """Tenant-scoped get_by_id. Returns None when the job belongs to a
        different tenant — never raises, never leaks the row."""
        result = await self.session.execute(
            select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def list_jobs(
        self,
        tenant_id: uuid.UUID,
        offset: int = 0,
        limit: int = 20,
        user_id: uuid.UUID | None = None,
        status: str | None = None,
        job_type: str | None = None,
        trace_id: str | None = None,
        created_after: Any = None,
        created_before: Any = None,
        retry_count_min: int | None = None,
        retry_count_max: int | None = None,
        remediation_hint: str | None = None,
        exclude_remediation_hints: Sequence[str] | None = None,
        require_trace_id: bool = False,
        sort: JobSort = JobSort.CREATED_AT,
    ) -> tuple[list[Job], int]:
        filters: list[Any] = [Job.tenant_id == tenant_id]
        if user_id is not None:
            filters.append(Job.user_id == user_id)
        if status is not None:
            filters.append(Job.status == status)
        if job_type is not None:
            filters.append(Job.type == job_type)
        if trace_id is not None:
            filters.append(Job.trace_id == trace_id)
        if created_after is not None:
            filters.append(Job.created_at >= created_after)
        if created_before is not None:
            filters.append(Job.created_at <= created_before)
        if retry_count_min is not None:
            filters.append(Job.retry_count >= retry_count_min)
        if retry_count_max is not None:
            filters.append(Job.retry_count <= retry_count_max)
        if remediation_hint is not None:
            filters.append(Job.remediation_hint == remediation_hint)
        if exclude_remediation_hints:
            # `NOT IN (...)` alone would be wrong here: SQL evaluates it to
            # NULL for a NULL column, so every *uncategorised* job would be
            # filtered out as well. An uncategorised DLQ entry is simply one
            # triage has not classified yet — the opposite of fenced — and
            # dropping it would quietly shrink the blast radius of the bulk
            # replay far past what R2-22 asked for.
            filters.append(
                or_(
                    Job.remediation_hint.is_(None),
                    Job.remediation_hint.not_in(list(exclude_remediation_hints)),
                )
            )

        if require_trace_id:
            # In SQL, ahead of the LIMIT — not in Python afterwards. A caller
            # that filtered after the fact spent its result budget on rows it
            # was about to discard, so on a table dominated by untraced jobs
            # it returned nothing while matching traced rows sat just past the
            # window (WO-R2-53). Empty string is as untraced as NULL.
            filters.append(Job.trace_id.is_not(None))
            filters.append(Job.trace_id != "")

        where = and_(*filters)
        total = await self._count(where)

        # `id` is the tiebreaker, and it is not cosmetic (WO-R2-58).
        # `created_at` is `transaction_timestamp()`, so every job a request
        # writes shares one value; OFFSET/LIMIT over a non-unique ORDER BY
        # lets the server return a row on two pages and another on none.
        # A uuid tiebreaker is arbitrary but *total*, which is all
        # pagination needs. Same shape on every paginated query in this
        # package — and it applies to whichever clock `sort` selected, since
        # `completed_at` ties for exactly the same reason `created_at` does.
        sort_key = (
            func.coalesce(Job.completed_at, Job.created_at)
            if sort is JobSort.DEAD_LETTERED_AT
            else Job.created_at
        )
        stmt = (
            select(Job)
            .where(where)
            .order_by(sort_key.desc(), Job.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        extra: dict[str, Any] | None = None,
        event_message: str | None = None,
        *,
        guard: Sequence[Any] | None = None,
    ) -> Job | None:
        """Write a job status, emitting its lifecycle event when terminal.

        A terminal status and its outbox event are one write. The row and the
        `outbox_events` insert land in the caller's transaction, so a job
        cannot be `dead_letter`/`completed` in Postgres while no consumer ever
        hears about it — no saga stranded in RUNNING, no id pinned in the read
        model's failed set, no gap in the event log.

        This used to be each caller's job, and four of the nine call sites got
        it wrong or partly wrong (`_force_dead_letter` emitted nothing,
        `resolve_incident` emitted nothing). Emission is no longer elective:
        every path that writes a terminal status goes through here, which is
        the point. See the addendum on ADR 0001.

        `event_message` colours the human-readable `message` field of a
        dead-letter event and is ignored by events that have no such field.
        It is deliberately the *only* thing a caller can vary — the rest of the
        payload is derived from the row, so the sites cannot drift apart again.

        `guard` turns the write into a compare-and-set: the extra predicates
        are ANDed into the WHERE clause, and if they no longer hold the method
        writes nothing, emits nothing, cascades nothing and returns None. It
        exists for writers that decided from a *previous* read that a row needs
        settling and must not act if the row moved underneath them — today the
        stale-RUNNING sweep (WO-R2-28), which re-checks the lease and
        `started_at` it observed during its scan so it cannot dead-letter a job
        another replica is still executing.

        A guarded refusal and a missing row both return None, which is the same
        answer to the same question: nothing was written. Callers that need the
        distinction have already read the row.

        The guard lives here rather than in a separate CAS method because the
        ADR 0001 addendum makes terminal-event emission non-elective — every
        path that writes a terminal status goes through this method, so a
        conditional terminal write has to be a mode of it, not a second copy
        of it that can drift.
        """
        values: dict[str, Any] = {"status": status}
        if extra:
            values.update(extra)
        # Always aware UTC — the columns are TIMESTAMP WITH TIME ZONE and
        # downstream Python math (SLO service, digest window computation)
        # uses aware datetimes. Mixing naive `utcnow()` here with aware
        # `datetime.now(UTC)` elsewhere raises TypeError the moment those
        # code paths meet on a real Postgres value.
        if status == "running" and "started_at" not in values:
            values["started_at"] = datetime.now(UTC)
        if status in _COMPLETED_AT_STATUSES and "completed_at" not in values:
            values["completed_at"] = datetime.now(UTC)

        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(Job)
                .where(Job.id == job_id, *(guard or ()))
                .values(**values)
            ),
        )
        await self.session.flush()
        if guard is not None and result.rowcount != 1:
            return None
        job = await self.get_by_id(job_id)
        if job is not None and status in _TERMINAL_EVENT_STATUSES:
            await self._emit_terminal_event(job, status, event_message)
        if job is not None and status in _CASCADE_SOURCE_STATUSES:
            await self.cascade_cancel_blocked_children(job_id, status)
        return job

    async def _emit_terminal_event(
        self, job: Job, status: str, event_message: str | None
    ) -> None:
        """Insert the outbox row announcing a terminal status.

        Same session, therefore the same transaction as the status write —
        that is the whole invariant (ADR 0001). Built from the freshly-read
        row, so the event always describes the state that was actually
        committed rather than what the caller believed it was writing.

        Emitting on every terminal write (rather than only on a real
        transition) is deliberate: detecting "was it already terminal?" needs
        a pre-read this method does not do, and a duplicate event is cheap —
        every consumer downstream is idempotent under at-least-once delivery
        already, which is what the outbox promises. A *missing* event is the
        expensive one.
        """
        settings = get_settings()
        if status == JobStatus.DEAD_LETTER:
            topic = settings.kafka_topic_job_dlq
            payload = dlq_event_payload(job, message=event_message)
        elif status == JobStatus.CANCELLED:
            topic = settings.kafka_topic_job_cancelled
            payload = cancelled_event_payload(job)
        else:
            topic = settings.kafka_topic_job_completed
            payload = completed_event_payload(job)

        await OutboxRepository(self.session).add(
            tenant_id=job.tenant_id,
            topic=topic,
            key=f"{job.tenant_id}:{job.user_id}",
            payload=payload,
        )

    async def set_remediation_hint_if_unset(
        self,
        *,
        job_id: uuid.UUID,
        tenant_id: uuid.UUID,
        hint: str,
    ) -> bool:
        """Fill in `remediation_hint` only if the job has none (R2-24).

        Emits the conditional UPDATE

            UPDATE jobs SET remediation_hint=:hint
            WHERE id=:id AND tenant_id=:t AND remediation_hint IS NULL

        and returns True only when THIS caller filled it. "Only if unset"
        is the whole contract: a value already there was put there by
        somebody making a statement about this dead-letter episode — an
        operator's or agent's `mark_dlq_permanent`, the eval seed, a
        chaos hook, or an earlier triage of the same episode — and
        arriving later is not a reason to overwrite it. The
        `human_required` case is the one that matters most: that fence is
        what keeps a job out of `replay_dlq_messages`' blind batch
        (R2-22), and a re-triage downgrading it to `replay_safe` would
        lower a fence somebody raised on purpose.

        Predicate, not read-then-write, for the same reason
        `claim_for_running` is: the triage consumer and an agent's
        `mark_dlq_permanent` can land in the same window, and Postgres
        arbitrating on the row is the only version of this that does not
        have a race in it.

        Deliberately not routed through `update_status` — this changes no
        status and must emit no lifecycle event. The episode is already
        `dead_letter` and announced; this only labels it.

        Note the column is reset to NULL on replay (R2-23), so "unset" is
        per-episode rather than once-per-job: a job that dead-letters
        again is categorised again, on the new failure's own evidence.
        """
        result = await self.session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.tenant_id == tenant_id,
                Job.remediation_hint.is_(None),
            )
            .values(remediation_hint=hint)
        )
        return cast(CursorResult[Any], result).rowcount == 1

    async def claim_for_running(self, job_id: uuid.UUID) -> bool:
        """Atomically claim a PENDING job for execution (E1-04).

        Emits the conditional UPDATE

            UPDATE jobs SET status='running', started_at=now()
            WHERE id=:id AND status='pending'

        and returns True only when THIS caller flipped the row
        (rowcount == 1). Kafka delivers job.submitted at-least-once, so
        two deliveries of the same job can race into the dispatcher; the
        status predicate makes the database arbitrate — the loser's
        UPDATE matches zero rows and it must skip execution. Unlike the
        generic `update_status` (whose callers intentionally overwrite
        from many prior states), this CAS never fires on a non-PENDING
        row, and it never re-fetches: the boolean is the whole contract.
        """
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.PENDING)
                .values(
                    status=JobStatus.RUNNING, started_at=datetime.now(UTC)
                )
            ),
        )
        await self.session.flush()
        return result.rowcount == 1

    async def renew_running_leases(
        self,
        job_ids: Collection[uuid.UUID],
        *,
        max_age_seconds: float,
    ) -> int:
        """Check in on behalf of the jobs this worker is executing (WO-R2-28).

        Emits

            UPDATE jobs SET heartbeat_at=now()
            WHERE id IN :ids AND status='running'
              AND started_at >= now() - :max_age

        and returns the number of rows renewed. The lease is what tells the
        stale-RUNNING sweep in *another* replica that a job it can see is
        someone's live work rather than a crash orphan; before it, the only
        signal was a set in one process's memory, so a second replica read
        every other replica's jobs as orphaned.

        `max_age_seconds` is the reason this is not an unconditional renewal.
        A worker that hangs would otherwise defend its own stuck job forever,
        which is WO-R2-07's finding wearing a new hat — the state nothing in
        the tree can reclaim. Past that age the renewal stops, the lease goes
        stale on its own, and the sweep reclaims the job like any other. The
        caller sets the age from `stale_running_threshold_seconds` plus the
        in-flight grace, so the lease never outlives the point at which the
        sweep is already entitled to act.

        `started_at IS NULL` rows drop out of the comparison rather than being
        renewed, matching the sweep, which skips them for the same reason:
        there is no age to reason about.

        `updated_at` is pinned to its own value so the ORM's `onupdate` does
        not fire. A check-in is not progress, and this statement runs every
        renewal interval for every running job — letting it move `updated_at`
        would churn a column the DLQ list and trace views render, and would
        make a job that has been wedged for an hour look freshly touched.
        """
        if not job_ids:
            return 0
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(Job)
                .where(
                    Job.id.in_(list(job_ids)),
                    Job.status == JobStatus.RUNNING,
                    Job.started_at
                    >= now - timedelta(seconds=max_age_seconds),
                )
                .values(heartbeat_at=now, updated_at=Job.updated_at)
            ),
        )
        await self.session.flush()
        return int(result.rowcount)

    async def promote_waiting_to_pending(self, job_id: uuid.UUID) -> bool:
        """Atomically promote a WAITING job to PENDING (E1-04).

        Same CAS shape as `claim_for_running`, for the two concurrent
        promoters — the DependencyResolver and the dispatcher's resume
        sweep. On rowcount == 0 the caller lost the race and MUST also
        skip its outbox add: CAS-ing the status alone still mints the
        duplicate job.submitted if the loser falls through to the outbox.
        """
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.WAITING)
                .values(status=JobStatus.PENDING, error_message=None)
            ),
        )
        await self.session.flush()
        return result.rowcount == 1

    async def cascade_cancel_blocked_children(
        self, parent_id: uuid.UUID, parent_status: str
    ) -> int:
        """Cancel the WAITING non-saga descendants of a stranded parent (R2-09).

        A parent in DEAD_LETTER or CANCELLED will never reach COMPLETED, so
        `unmet_count` counts it as unmet forever and its WAITING children can
        never be promoted by anything — not the DependencyResolver, which only
        reacts to `job.completed`, and not the resume sweep, which requires the
        same predicate. Before this, those rows simply accumulated: the enum
        comment promised the cascade, nothing performed it, and the resume
        sweep's `LIMIT 200` candidate set filled up with rows no mechanism
        could remove, starving the healthy children queued behind them.

        Runs in the caller's transaction, same as `_emit_terminal_event`, so a
        parent cannot be dead-lettered in Postgres while its children stay
        WAITING in the same database.

        Scope, and why each bound is here:

        * `saga_id IS NULL` — saga steps belong to `SagaCoordinator`, which
          cancels them by saga membership (`SagaRepository.waiting_steps`) and
          not by the dependency DAG. Filtering them out keeps the two
          mechanisms disjoint: no double-cancel, no race with the coordinator.
        * `status == WAITING` — a CAS in set form. A child already RUNNING or
          terminal is not ours to touch, and the predicate is what makes a
          concurrent cascade from a sibling parent idempotent.
        * Level-by-level, not one recursive CTE: `UPDATE ... WITH RECURSIVE`
          is not portable to the SQLite the unit suite runs on, and the depth
          of a real job DAG is single digits.

        The walk continues through cancelled children because `unmet_count`
        treats a CANCELLED parent as unmet too — stopping at the first level
        would just relocate the stuck set one generation down.

        Announcing (WO-R2-113 + WO-R2-114): every row this cancels gets its
        own `job.cancelled` outbox event and its own `completed_at`, written
        in the caller's transaction alongside the status.

        This method is the reason both work orders needed more than the one
        branch in `update_status` they were scoped as. #152 made terminal
        emission non-elective by routing every terminal write through that
        method — but this is a set-based `UPDATE ... WHERE id IN (...)` that
        never passes through it, so it is a *second* terminal writer, and the
        one that produces cancellations in bulk. Adding CANCELLED to
        `_TERMINAL_EVENT_STATUSES` alone would have announced the saga
        coordinator's cancellations and left a whole DAG level's worth silent
        per stranded parent, which is the failure mode inverted, not fixed.

        It stays set-based rather than looping through `update_status` per
        child for two reasons: the per-row path would re-enter the cascade
        from every child (CANCELLED is itself a `_CASCADE_SOURCE_STATUSES`
        member), re-walking the subtree once per node; and the level-by-level
        UPDATE is what keeps this portable to the SQLite the unit suite runs
        on. The payload is built by the same `cancelled_event_payload` the
        single writer uses, so the two producers cannot drift.

        Returns the number of rows cancelled, for the caller's logging.
        """
        settings = get_settings()
        outbox = OutboxRepository(self.session)
        reason = f"dependency parent {parent_id} ended in {parent_status}"
        # One timestamp for the whole cascade: it stamps the rows, and it is
        # also how the re-read below identifies exactly the rows this UPDATE
        # touched (a concurrent cascade computes its own, to the microsecond).
        cancelled_at = datetime.now(UTC)
        cancelled = 0
        frontier: list[uuid.UUID] = [parent_id]
        seen: set[uuid.UUID] = {parent_id}

        for _ in range(_CASCADE_MAX_DEPTH):
            if not frontier:
                break
            rows = await self.session.execute(
                select(JobDependency.job_id)
                .join(Job, Job.id == JobDependency.job_id)
                .where(
                    JobDependency.depends_on_job_id.in_(frontier),
                    Job.status == JobStatus.WAITING,
                    Job.saga_id.is_(None),
                )
            )
            targets = [r[0] for r in rows.all() if r[0] not in seen]
            if not targets:
                break

            result = cast(
                CursorResult[Any],
                await self.session.execute(
                    update(Job)
                    .where(
                        Job.id.in_(targets),
                        Job.status == JobStatus.WAITING,
                        Job.saga_id.is_(None),
                    )
                    .values(
                        status=JobStatus.CANCELLED,
                        error_message=reason,
                        completed_at=cancelled_at,
                    )
                ),
            )
            await self.session.flush()
            cancelled += result.rowcount

            # Re-read what actually changed and announce it. The UPDATE
            # re-checks `status == WAITING`, so `targets` is what we intended
            # to cancel and this is what we did — the difference is a child a
            # concurrent writer moved first, and that writer owns its event.
            newly_cancelled = (
                await self.session.execute(
                    select(Job).where(
                        Job.id.in_(targets),
                        Job.completed_at == cancelled_at,
                    )
                )
            ).scalars().all()
            for child in newly_cancelled:
                await outbox.add(
                    tenant_id=child.tenant_id,
                    topic=settings.kafka_topic_job_cancelled,
                    key=f"{child.tenant_id}:{child.user_id}",
                    payload=cancelled_event_payload(child),
                )

            seen.update(targets)
            frontier = targets

        return cancelled

    async def dlq_stats(self, tenant_id: uuid.UUID) -> tuple[int, dict[str, int]]:
        """Total DLQ count plus per-job-type breakdown, scoped to one tenant."""
        stmt = (
            select(Job.type, func.count().label("n"))
            .where(
                Job.status == JobStatus.DEAD_LETTER,
                Job.tenant_id == tenant_id,
            )
            .group_by(Job.type)
        )
        result = await self.session.execute(stmt)
        by_type = {row.type: int(row.n) for row in result.all()}
        return sum(by_type.values()), by_type
