"""Repository for the transactional outbox table."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.models.outbox import OutboxEvent
from app.repositories.base import BaseRepository
from sqlalchemy import and_, case, func, select, update

#: Under the column width: an over-long value would abort the marking transaction.
_ERROR_MESSAGE_MAX_CHARS = 900


def _as_utc(value: Any) -> datetime | None:
    """Normalise a timestamp the driver handed back, or `None` — SQLite's naive
    datetimes are read as UTC, the platform's clock everywhere."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class OutboxDeliverySnapshot:
    """One reading of the outbox's delivery state, for one tenant.

    `measured_at` is the database server's clock, so every age from the snapshot is
    measured against the clock that stamped them.
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

        Callers build the f"{tenant_id}:{user_id}" partition key (ADR 0004) themselves.
        """
        return await self.create(
            tenant_id=tenant_id, topic=topic, key=key, payload=payload
        )

    async def fetch_unpublished(self, limit: int = 100) -> list[OutboxEvent]:
        """Oldest unpublished events first, capped at `limit`.

        No row locks — single-writer is the caller's advisory-lock leader gate (ADR 0020).
        The `attempts` bound keeps a capped-out row from stalling this fixed-size window.
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
        """Count one failed publish against each row — at the cap it leaves the queue."""
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

        Sets `published_at` *and* `failed_at` (ADR 0001 item 3) — out of
        `fetch_unpublished`, never read as delivered. `error` is truncated to protect this tx.
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

        Feeds the relay's stall alarm; `QueueDepth` cannot — it measures the Redis delayed
        set. Counts every unpublished row, including ones past the attempt cap.
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

        Backs `get_outbox_status` in one statement, so every number shares an instant.
        Delivered needs `failed_at IS NULL` too, or a stalled relay reads as delivered.
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
            # `now()` is a function of the connection, not the rows, so it answers on
            # an empty table too. The fallback is the asking process's clock — a last
            # resort.
            measured_at=_as_utc(row[5]) or datetime.now(UTC),
            unpublished_count=int(row[0] or 0),
            oldest_unpublished_at=_as_utc(row[1]),
            newest_unpublished_at=_as_utc(row[2]),
            unpublished_past_attempt_limit=int(row[3] or 0),
            last_publish_at=_as_utc(row[4]),
        )
