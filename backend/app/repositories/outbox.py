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
        topic: str,
        key: str,
        payload: dict[str, Any],
        *,
        tenant_id: uuid.UUID | None = None,
    ) -> OutboxEvent:
        """Insert an event into the outbox (committed with the surrounding tx).

        tenant_id is optional in this PR; omitting it falls back to the
        model-level default tenant. Phase 12 PR B makes it required."""
        kwargs: dict[str, Any] = {"topic": topic, "key": key, "payload": payload}
        if tenant_id is not None:
            kwargs["tenant_id"] = tenant_id
        return await self.create(**kwargs)

    async def fetch_unpublished(self, limit: int = 100) -> list[OutboxEvent]:
        """Oldest unpublished events first, capped at `limit`."""
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
