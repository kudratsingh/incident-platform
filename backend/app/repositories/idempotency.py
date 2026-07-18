import uuid

from app.models.idempotency import IdempotencyRecord
from app.repositories.base import BaseRepository
from sqlalchemy import select


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
