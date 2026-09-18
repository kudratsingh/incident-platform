"""Storage for idempotency claims — the rows that let a repeated Tier-1 call
return the first call's answer instead of running again."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.idempotency import IdempotencyRecord
from app.repositories.base import BaseRepository
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


class IdempotencyRepository(BaseRepository[IdempotencyRecord]):
    model = IdempotencyRecord

    async def get_by_key(
        self,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        """The record for this key, scoped to the tenant and principal that
        own it."""
        result = await self.session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == tenant_id,
                IdempotencyRecord.principal_id == principal_id,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def insert_claim(
        self,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        tool_name: str,
        idempotency_key: str,
        arguments_hash: str,
        expires_at: datetime | None,
    ) -> uuid.UUID | None:
        """Reserve the key with a response-less row, atomically.

        `ON CONFLICT DO NOTHING RETURNING id` — the new id if this caller won the key,
        `None` otherwise. On Postgres an uncommitted conflict blocks — the serialisation.
        """
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "principal_id": principal_id,
            "tool_name": tool_name,
            "idempotency_key": idempotency_key,
            "arguments_hash": arguments_hash,
            "response_json": None,
            "expires_at": expires_at,
        }
        # Exactly the columns of `uq_idempotency_scope`; only Postgres takes `constraint=`.
        conflict_columns = ["tenant_id", "principal_id", "idempotency_key"]
        if self.session.get_bind().dialect.name == "postgresql":
            stmt: Any = pg_insert(IdempotencyRecord)
        else:
            stmt = sqlite_insert(IdempotencyRecord)
        stmt = (
            stmt.values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
            .returning(IdempotencyRecord.id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def complete_claim(
        self,
        *,
        record_id: uuid.UUID,
        response_json: dict[str, Any],
        expires_at: datetime | None,
    ) -> None:
        """Attach the response to a claim this caller owns — an UPDATE by primary key on
        our own row, so it cannot collide."""
        await self.session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id)
            .values(response_json=response_json, expires_at=expires_at)
        )
        await self.session.flush()

    async def delete_by_id(self, *, record_id: uuid.UUID) -> None:
        """Drop a single record. Used to release an unfinished claim and
        to evict an expired record before taking its key over."""
        await self.session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.id == record_id)
        )
        await self.session.flush()

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        """DELETE every record whose `expires_at` has passed, for the reaper loop
        (ADR 0010's "no reaper" follow-up). `expires_at IS NULL` is never reaped.
        Returns the row count deleted."""
        cutoff = now or datetime.now(UTC)
        result = await self.session.execute(
            delete(IdempotencyRecord).where(
                IdempotencyRecord.expires_at.is_not(None),
                IdempotencyRecord.expires_at < cutoff,
            )
        )
        # Async DML returns CursorResult, not the declared Result.
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
