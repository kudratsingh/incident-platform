from sqlalchemy import select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def list_all(self, offset: int = 0, limit: int = 20) -> tuple[list[User], int]:
        total = await self._count()
        result = await self.session.execute(
            select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result.scalars().all()), total
