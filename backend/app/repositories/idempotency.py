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

        `INSERT ... ON CONFLICT DO NOTHING RETURNING id`: returns the new
        row's id when this caller won the key, and `None` when someone
        else already holds it. One statement, so there is no window
        between deciding the key is free and taking it — which is the
        whole point. The lookup-then-insert shape this replaces left both
        of two concurrent callers believing they had the key.

        On Postgres a conflicting *uncommitted* row makes this statement
        wait on the holder's transaction rather than returning
        immediately, so the loser resumes once the winner has committed
        its response and reads it back. That blocking is the
        serialisation, not a side effect to design around.

        Dialect-specific by necessity: `ON CONFLICT` is not in core
        SQLAlchemy. Both engines we run on support it with the same
        semantics for this use.
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
        # Conflict target given as the constraint's columns rather than
        # its name: both dialects accept `index_elements`, only Postgres
        # accepts `constraint=`. These are exactly the columns of
        # `uq_idempotency_scope`.
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
        """Attach the response to a claim this caller owns.

        An UPDATE by primary key on a row we inserted ourselves, so it
        cannot collide — which is what removes the "action took effect,
        cache write lost the race" window entirely rather than repairing
        it afterwards."""
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
