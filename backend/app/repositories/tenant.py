"""Repository for tenants."""

import uuid

from app.models.tenant import Tenant
from app.repositories.base import BaseRepository
from sqlalchemy import func, select


class TenantRepository(BaseRepository[Tenant]):
    model = Tenant

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(
        self, offset: int = 0, limit: int = 50
    ) -> tuple[list[Tenant], int]:
        total = (await self.session.execute(select(func.count()).select_from(Tenant))).scalar_one()
        result = await self.session.execute(
            select(Tenant)
            .order_by(Tenant.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), int(total)

    async def counts_for(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """Per-tenant counts of users + jobs — useful for the admin tenant list."""
        from app.models.job import Job
        from app.models.user import User

        users_total = (
            await self.session.execute(
                select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
            )
        ).scalar_one()
        jobs_total = (
            await self.session.execute(
                select(func.count()).select_from(Job).where(Job.tenant_id == tenant_id)
            )
        ).scalar_one()
        return {"users": int(users_total), "jobs": int(jobs_total)}
