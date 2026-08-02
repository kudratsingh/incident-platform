import uuid
from datetime import UTC, datetime

from app.models.idempotency import IdempotencyRecord
from app.repositories.base import BaseRepository
from sqlalchemy import delete, select


class IdempotencyRepository(BaseRepository[IdempotencyRecord]):
    model = IdempotencyRecord

    async def get_by_key(
        self,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        result = await self.session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.principal_id == principal_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        """DELETE every record whose `expires_at` is in the past. Used
        by the reaper loop — ADR 0010's "no reaper" clause pointed at
        this method as the follow-up when write rate justifies it.
        Records with `expires_at IS NULL` (no TTL — shouldn't happen
        post-v0.4.5 but kept nullable for schema flexibility) are
        never reaped.

        Returns the row count deleted."""
        cutoff = now or datetime.now(UTC)
        result = await self.session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.expires_at.is_not(None),
                IdempotencyRecord.expires_at < cutoff,
            )
        )
        # SQLAlchemy async DML returns a CursorResult (has rowcount) but
        # the annotated return type is Result. Runtime is correct;
        # mypy needs the nudge.
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
