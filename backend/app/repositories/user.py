import uuid

from app.models.user import User
from app.repositories.base import BaseRepository
from sqlalchemy import func, select


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def get_for_tenant(
        self, user_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> User | None:
        """Tenant-scoped get_by_id. Returns None when the user belongs to a
        different tenant — never raises, never leaks the row.

        `users` is the one deliberate exclusion from RLS (auth reads it
        before `app.tenant_id` is set, ADR 0003), so this predicate is the
        only tenant boundary on a user lookup — there is nothing underneath
        it to catch a miss.
        """
        result = await self.session.execute(
            select(User).where(User.id == user_id, User.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def list_all(
        self,
        offset: int = 0,
        limit: int = 20,
        tenant_id: uuid.UUID | None = None,
    ) -> tuple[list[User], int]:
        base = select(User)
        if tenant_id is not None:
            base = base.where(User.tenant_id == tenant_id)
            total = (
                await self.session.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.tenant_id == tenant_id)
                )
            ).scalar_one()
        else:
            total = await self._count()
        result = await self.session.execute(
            base.order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars().all()), total
