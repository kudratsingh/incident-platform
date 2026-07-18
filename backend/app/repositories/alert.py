import uuid

from app.models.alert import Alert
from app.repositories.base import BaseRepository
from sqlalchemy import desc, func, select


class AlertRepository(BaseRepository[Alert]):
    model = Alert

    async def list_active_for_tenant(
        self,
        tenant_id: uuid.UUID,
        offset: int = 0,
        limit: int = 50,
        severity: str | None = None,
    ) -> tuple[list[Alert], int]:
        base = select(Alert).where(
            Alert.tenant_id == tenant_id,
            Alert.resolved_at.is_(None),
        )
        count_stmt = (
            select(func.count())
            .select_from(Alert)
            .where(Alert.tenant_id == tenant_id, Alert.resolved_at.is_(None))
        )
        if severity is not None:
            base = base.where(Alert.severity == severity)
            count_stmt = count_stmt.where(Alert.severity == severity)

        total = (await self.session.execute(count_stmt)).scalar_one()
        result = await self.session.execute(
            base.order_by(desc(Alert.fired_at)).offset(offset).limit(limit)
        )
        return list(result.scalars().all()), total
