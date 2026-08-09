"""Repository for the transactional outbox table."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.outbox import OutboxEvent
from app.repositories.base import BaseRepository
from sqlalchemy import select, update


class OutboxRepository(BaseRepository[OutboxEvent]):
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
        """
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def mark_published(self, ids: list[uuid.UUID]) -> None:
        if not ids:
            return
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(published_at=datetime.now(UTC))
        )
        await self.session.flush()

    async def increment_attempts(self, ids: list[uuid.UUID]) -> None:
        if not ids:
            return
        await self.session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(attempts=OutboxEvent.attempts + 1)
        )
        await self.session.flush()
