import uuid
from datetime import UTC, datetime
from typing import Any, cast

from app.config import get_settings
from app.models.enums import JobStatus
from app.models.job import Job
from app.models.job_dependency import JobDependency
from app.repositories.base import BaseRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.job_events import completed_event_payload, dlq_event_payload
from sqlalchemy import CursorResult, and_, func, select, update

# The statuses a job never leaves, and the Kafka topic each one announces on.
# `CANCELLED` is terminal too but is deliberately absent: the platform has no
# `job.cancelled` topic, so there is no event to emit in the same transaction.
# Its single writer (`SagaCoordinator._handle_failure`) cancels steps that the
# saga is already settling, so the saga side stays coherent — but the CQRS read
# model does leave those ids in their previous status set. Adding the topic is
# a schema-registry change with four consumers to update; tracked in
# docs/ROADMAP.md rather than smuggled in here.
_TERMINAL_EVENT_STATUSES = (JobStatus.DEAD_LETTER, JobStatus.COMPLETED)

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

        where = and_(*filters)
        total = await self._count(where)

        stmt = (
            select(Job)
            .where(where)
            .order_by(Job.created_at.desc())
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
        if status in ("completed", "failed", "dead_letter") and "completed_at" not in values:
            values["completed_at"] = datetime.now(UTC)

        await self.session.execute(
            update(Job).where(Job.id == job_id).values(**values)
        )
        await self.session.flush()
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
        else:
            topic = settings.kafka_topic_job_completed
            payload = completed_event_payload(job)

        await OutboxRepository(self.session).add(
            tenant_id=job.tenant_id,
            topic=topic,
            key=f"{job.tenant_id}:{job.user_id}",
            payload=payload,
        )

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

        Returns the number of rows cancelled, for the caller's logging.
        """
        reason = f"dependency parent {parent_id} ended in {parent_status}"
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
                        status=JobStatus.CANCELLED, error_message=reason
                    )
                ),
            )
            await self.session.flush()
            cancelled += result.rowcount
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
