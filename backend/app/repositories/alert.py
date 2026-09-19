import uuid

from app.models.alert import Alert
from app.repositories.base import BaseRepository
from sqlalchemy import desc, func, select


class AlertRepository(BaseRepository[Alert]):
    model = Alert

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        active: bool | None = None,
        severity: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Alert], int]:
        """One page of alerts, newest first, plus the true total.

        The operator console's read (WO-R3-312). `active` is a tri-state, unlike
        `list_active_for_tenant`'s fixed `resolved_at IS NULL`: a human looking at a
        timeline needs to see what has already been resolved, which is the one thing
        the agent's `list_active_alerts` never shows.
        """
        filters = [Alert.tenant_id == tenant_id]
        if active is True:
            filters.append(Alert.resolved_at.is_(None))
        elif active is False:
            filters.append(Alert.resolved_at.is_not(None))
        if severity is not None:
            filters.append(Alert.severity == severity)

        total = (
            await self.session.execute(
                select(func.count()).select_from(Alert).where(*filters)
            )
        ).scalar_one()
        result = await self.session.execute(
            select(Alert)
            .where(*filters)
            .order_by(desc(Alert.fired_at), desc(Alert.id))
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total

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
