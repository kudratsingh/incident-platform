import uuid

from app.models.service_account import ServiceAccount, ServiceAccountToken
from app.repositories.base import BaseRepository
from sqlalchemy import func, select


class ServiceAccountRepository(BaseRepository[ServiceAccount]):
    model = ServiceAccount

    async def get_by_name(
        self, tenant_id: uuid.UUID, name: str
    ) -> ServiceAccount | None:
        result = await self.session.execute(
            select(ServiceAccount).where(
                ServiceAccount.tenant_id == tenant_id,
                ServiceAccount.name == name,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ServiceAccount], int]:
        total = (
            await self.session.execute(
                select(func.count())
                .select_from(ServiceAccount)
                .where(ServiceAccount.tenant_id == tenant_id)
            )
        ).scalar_one()
        result = await self.session.execute(
            select(ServiceAccount)
            .where(ServiceAccount.tenant_id == tenant_id)
            .order_by(ServiceAccount.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total


class ServiceAccountTokenRepository(BaseRepository[ServiceAccountToken]):
    model = ServiceAccountToken

    async def get_by_hash(self, token_hash: str) -> ServiceAccountToken | None:
        result = await self.session.execute(
            select(ServiceAccountToken).where(
                ServiceAccountToken.token_hash == token_hash
            )
        )
        return result.scalar_one_or_none()

    async def list_for_service_account(
        self, service_account_id: uuid.UUID
    ) -> list[ServiceAccountToken]:
        result = await self.session.execute(
            select(ServiceAccountToken)
            .where(ServiceAccountToken.service_account_id == service_account_id)
            .order_by(ServiceAccountToken.created_at.desc())
        )
        return list(result.scalars().all())
