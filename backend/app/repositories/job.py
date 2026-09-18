"""Every read and write against the `jobs` table.

Each terminal write also adds the outbox row that announces it.
"""

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
# CANCELLED was the silent exception until WO-R2-113; asserted against
# `TERMINAL_JOB_STATUSES` in `tests/unit/test_job_cancelled_topic_wiring.py`.
_TERMINAL_EVENT_STATUSES = (
    JobStatus.DEAD_LETTER,
    JobStatus.COMPLETED,
    JobStatus.CANCELLED,
)

# The terminal statuses that stamp `completed_at` (WO-R2-114) — all of them, since
# a cancellation stops the job too. `FAILED` refreshes the stamp on the next
# terminal write, because a failed job may still be retried.
_COMPLETED_AT_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.DEAD_LETTER,
    JobStatus.CANCELLED,
)

# The statuses that strand a dependency DAG below them: a parent here never reaches
# COMPLETED, so `unmet_count` counts it as unmet forever and reaching one cascades
# CANCELLED down the non-saga descendants (R2-09). `FAILED` is deliberately absent —
# the retry cycle re-enters from it, so such a parent may yet complete.
_CASCADE_SOURCE_STATUSES = (JobStatus.DEAD_LETTER, JobStatus.CANCELLED)

# Cycle guard: only a corrupted edge set could reach this depth.
_CASCADE_MAX_DEPTH = 50


class JobSort(StrEnum):
    """Which clock `list_jobs` sorts on — submission time or dead-letter time (WO-R2-53)."""

    CREATED_AT = "created_at"
    DEAD_LETTERED_AT = "dead_lettered_at"


class JobRepository(BaseRepository[Job]):
    model = Job

    async def get_by_idempotency_key(
        self, key: str, tenant_id: uuid.UUID
    ) -> Job | None:
        """The job an earlier submission with this key created, if any. Keys
        are unique per tenant, so two tenants may reuse one freely."""
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
        """One page of jobs plus the true total; every filter runs in SQL, before the limit."""
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
            # `NOT IN (...)` is NULL for a NULL column, so it would also drop
            # uncategorised jobs — unclassified is not fenced (R2-22).
            filters.append(
                or_(
                    Job.remediation_hint.is_(None),
                    Job.remediation_hint.not_in(list(exclude_remediation_hints)),
                )
            )

        if require_trace_id:
            # In SQL, ahead of the LIMIT: filtering afterwards spent the result budget
            # on rows about to be discarded (WO-R2-53). "" is as untraced as NULL.
            filters.append(Job.trace_id.is_not(None))
            filters.append(Job.trace_id != "")

        where = and_(*filters)
        total = await self._count(where)

        # `id` is the tiebreaker, and not cosmetic (WO-R2-58): `created_at` is
        # `transaction_timestamp()`, so a non-unique ORDER BY under OFFSET/LIMIT can
        # return one row on two pages and another on none. Same for `completed_at`.
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

        Row and outbox insert share one transaction (ADR 0001). `guard` (WO-R2-28) makes
        it a compare-and-set: predicates that no longer hold write nothing, return None.
        """
        values: dict[str, Any] = {"status": status}
        if extra:
            values.update(extra)
        # Always aware UTC: the columns are TIMESTAMP WITH TIME ZONE, and mixing a
        # naive `utcnow()` here with aware datetimes downstream raises TypeError.
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

        Same transaction as the status write (ADR 0001). Emits on every terminal write,
        not only on transitions — a duplicate event is cheaper than a missing one.
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

        True only when THIS caller filled it: an existing `human_required` fence must not
        be downgraded (R2-22). No lifecycle event; replay resets the column (R2-23).
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

        Conditional UPDATE on `status='pending'`, True only when THIS caller flipped the
        row: Kafka is at-least-once, so the loser must skip execution.
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
        """Renew the lease on the RUNNING jobs this worker is executing (WO-R2-28).

        The lease tells the sweep in another replica that a job is live work, not a crash
        orphan. Bounded so a hung worker cannot defend its own job; `updated_at` is pinned.
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

        CAS like `claim_for_running`: on rowcount == 0 the caller lost and must skip
        its outbox add, else a duplicate job.submitted.
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

        Each cancelled row gets its own `job.cancelled` event and `completed_at` in the
        caller's transaction (WO-R2-113/114) — a set-based second writer, not `update_status`.
        """
        settings = get_settings()
        outbox = OutboxRepository(self.session)
        reason = f"dependency parent {parent_id} ended in {parent_status}"
        # One timestamp for the cascade: it also identifies the rows this UPDATE touched.
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

            # Re-read what actually changed and announce it: a child a concurrent
            # writer moved first is that writer's to announce.
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

    async def existing_ids_for_tenant(
        self, job_ids: Collection[uuid.UUID], tenant_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Which of `job_ids` this tenant actually has rows for.

        Ids only — the one caller (`get_cache_key_info`) asks whether the record exists,
        not what is in it. Another tenant's id reads as absent, disclosing nothing.
        """
        ids = set(job_ids)
        if not ids:
            return set()
        result = await self.session.execute(
            select(Job.id).where(Job.id.in_(ids), Job.tenant_id == tenant_id)
        )
        return set(result.scalars().all())

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
