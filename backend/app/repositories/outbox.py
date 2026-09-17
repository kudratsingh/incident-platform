"""Repository for the transactional outbox table."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.models.outbox import OutboxEvent
from app.repositories.base import BaseRepository
from sqlalchemy import and_, case, func, select, update

#: Keep well under the column width; the tail of a long driver traceback
#: is rarely the informative part and an over-long value would abort the
#: marking transaction, which is the one transaction that must not fail.
_ERROR_MESSAGE_MAX_CHARS = 900


def _as_utc(value: Any) -> datetime | None:
    """Normalise a timestamp the driver handed back, or `None`.

    Postgres returns aware datetimes and SQLite returns naive ones for the same
    column, and this platform's clock is UTC everywhere — so a naive value is
    read as UTC rather than an otherwise good timestamp being discarded.
    """
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class OutboxDeliverySnapshot:
    """One reading of the outbox's delivery state, for one tenant.

    `measured_at` is the database server's clock at the instant the reading was
    taken, and it is carried alongside the timestamps rather than left for the
    caller to supply: every age derived from this snapshot has to be measured
    against the clock that stamped the rows, or the age and the timestamp it
    came from can disagree.
    """

    measured_at: datetime
    unpublished_count: int
    oldest_unpublished_at: datetime | None
    newest_unpublished_at: datetime | None
    unpublished_past_attempt_limit: int
    last_publish_at: datetime | None


class OutboxRepository(BaseRepository[OutboxEvent]):
    """The relay's whole interface to the queue: add a row, take the oldest
    unpublished ones, and record how each attempt ended."""

    model = OutboxEvent

    async def add(
        self,
        *,
        tenant_id: uuid.UUID,
        topic: str,
        key: str,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        """Insert an event into the outbox (committed with the surrounding tx).

        Partition key convention is f"{tenant_id}:{user_id}" — tenants
        distribute evenly across partitions while per-user ordering is
        preserved within a tenant. Callers build that string themselves
        because they have the local context.
        """
        return await self.create(
            tenant_id=tenant_id, topic=topic, key=key, payload=payload
        )

    async def fetch_unpublished(self, limit: int = 100) -> list[OutboxEvent]:
        """Oldest unpublished events first, capped at `limit`.

        No `.with_for_update(skip_locked=True)` here, on purpose. It looks
        like the obvious guard against two relays draining the same batch,
        and it would be — but only for a caller that keeps this
        transaction open across the Kafka publishes. The relay's does not
        (`_outbox_relay_tick`: fetch tx / publish / mark tx), so the row
        locks would be released before the first publish and buy nothing
        but the appearance of safety. What actually makes the relay a
        single writer is the advisory-lock leader gate the caller holds
        around the whole tick — see ADR 0020. If you ever collapse the
        relay into one transaction, add the clause back and say so there.

        The `attempts` bound is the second half of the poison-row guard.
        `mark_failed` already lifts a capped-out row out of this window by
        setting `published_at`, so in the normal path this predicate never
        fires. It matters when that write did not happen: the relay crashed
        between incrementing and marking, or the rows predate the cap.

        Without it, one such row is not just retried forever — it holds a
        slot in a *fixed-size* window, and this window is the entire input
        to the relay. 100 of them and nothing else is ever fetched, so
        delivery stops for every tenant at once, silently.
        """
        max_attempts = get_settings().outbox_max_attempts
        stmt = (
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.attempts < max_attempts,
            )
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_published(self, ids: list[uuid.UUID]) -> None:
        """Stamp these rows delivered, which is what takes them out of the
        relay's window."""
        if not ids:
            return
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(published_at=datetime.now(UTC))
        )
        await self.session.flush()

    async def increment_attempts(self, ids: list[uuid.UUID]) -> None:
        """Count one failed publish against each row.

        The counter is not decoration: `fetch_unpublished` filters on it and
        the relay dead-letters on it. Anything that increments must be
        prepared for the row to leave the queue once it reaches the cap.
        """
        if not ids:
            return
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(attempts=OutboxEvent.attempts + 1)
        )
        await self.session.flush()

    async def mark_failed(self, ids: list[uuid.UUID], error: str) -> None:
        """Dead-letter rows: abandon them without publishing.

        Sets `published_at=NOW, error_message=...` exactly as ADR 0001
        Decision item 3 specifies, which is what lifts the row out of
        `fetch_unpublished`'s window, plus `failed_at` so a row that was
        abandoned is never mistaken for one that was delivered.

        The row is kept, with its payload intact. This is quarantine, not
        deletion: recovering from a cap that fired too eagerly (a long
        broker outage) is one statement —

            UPDATE outbox_events
               SET published_at = NULL, failed_at = NULL,
                   error_message = NULL, attempts = 0
             WHERE failed_at IS NOT NULL AND ...;

        `error` is truncated to fit the column; a driver-level string
        overflow here would roll back the whole marking transaction and put
        the poison row straight back into the window.
        """
        if not ids:
            return
        now = datetime.now(UTC)
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(
                published_at=now,
                failed_at=now,
                error_message=error[:_ERROR_MESSAGE_MAX_CHARS],
            )
        )
        await self.session.flush()

    async def unpublished_stats(self) -> tuple[int, float]:
        """(depth, age-of-oldest-in-seconds) over the live queue.

        Feeds the relay's stall alarm. `QueueDepth` cannot serve that role:
        it measures the Redis delayed set, which reads perfectly healthy
        while the outbox is completely stalled.

        Deliberately counts every unpublished row, including ones already
        past the attempt cap that `fetch_unpublished` skips — the alarm's
        job is to notice rows that are not moving, whatever the reason.
        """
        result = await self.session.execute(
            select(func.count(), func.min(OutboxEvent.created_at)).where(
                OutboxEvent.published_at.is_(None)
            )
        )
        depth, oldest = result.one()
        if not depth or oldest is None:
            return 0, 0.0
        if oldest.tzinfo is None:
            # SQLite hands back naive datetimes; Postgres gives aware ones.
            oldest = oldest.replace(tzinfo=UTC)
        return int(depth), max(0.0, (datetime.now(UTC) - oldest).total_seconds())

    async def delivery_snapshot(
        self, *, tenant_id: uuid.UUID
    ) -> OutboxDeliverySnapshot:
        """How delivery is going for one tenant, as one reading (WO-R3-201).

        Backs the `get_outbox_status` MCP tool. Five aggregates and the server's
        clock in a single statement, so every number describes the same instant
        — the alternative, one query per number plus `datetime.now()` in Python,
        can hand a caller an age and a timestamp that contradict each other, and
        measures against whichever process asked rather than against the clock
        that stamped `created_at`.

        Two predicates carry the honesty of the whole reading:

          * **Awaiting delivery is `published_at IS NULL`.** Every row still in
            the queue, including ones past the attempt cap that
            `fetch_unpublished` skips. Those are counted separately rather than
            excluded or hidden: they really are undelivered, and the relay will
            not try them again, so a backlog made of them does not drain no
            matter how healthy the relay is. A caller told only the total would
            reasonably expect it to fall on its own.
          * **Delivered is `published_at IS NOT NULL AND failed_at IS NULL`.**
            `mark_failed` stamps `published_at` too (ADR 0001 Decision item 3,
            and the docstring above), so a bare `max(published_at)` would report
            a row the relay gave up on as a successful publish — i.e. it would
            make a completely stalled relay look like it had just delivered
            something. That is the worst reading this tool could produce, and it
            would produce it silently.

        Aggregates are written `count(CASE …)` / `min(CASE …)` rather than
        `FILTER (WHERE …)` so the same statement runs on the SQLite the unit
        tier uses. Scoped to one tenant to match every other agent-facing read,
        and the RLS policy underneath.
        """
        awaiting = OutboxEvent.published_at.is_(None)
        delivered = and_(
            OutboxEvent.published_at.is_not(None), OutboxEvent.failed_at.is_(None)
        )
        max_attempts = get_settings().outbox_max_attempts

        stmt = select(
            func.count(case((awaiting, OutboxEvent.id))),
            func.min(case((awaiting, OutboxEvent.created_at))),
            func.max(case((awaiting, OutboxEvent.created_at))),
            func.count(
                case(
                    (
                        and_(awaiting, OutboxEvent.attempts >= max_attempts),
                        OutboxEvent.id,
                    )
                )
            ),
            func.max(case((delivered, OutboxEvent.published_at))),
            func.now(),
        ).where(OutboxEvent.tenant_id == tenant_id)

        row = (await self.session.execute(stmt)).one()
        return OutboxDeliverySnapshot(
            # The server clock, always in practice: `now()` is a function of the
            # connection, not of the rows, so it answers on an empty table too.
            # The fallback exists so a backend that somehow declined to give one
            # still produces a reading rather than an exception — it is the
            # asking process's clock, which is why it is a last resort and not
            # the default.
            measured_at=_as_utc(row[5]) or datetime.now(UTC),
            unpublished_count=int(row[0] or 0),
            oldest_unpublished_at=_as_utc(row[1]),
            newest_unpublished_at=_as_utc(row[2]),
            unpublished_past_attempt_limit=int(row[3] or 0),
            last_publish_at=_as_utc(row[4]),
        )
