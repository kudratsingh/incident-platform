import uuid
from typing import Any, Generic, TypeVar

from app.models.base import Base
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Generic async repository providing basic CRUD operations."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, id: uuid.UUID) -> ModelT | None:
        result = await self.session.execute(
            select(self.model).where(self.model.id == id)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def create(self, **kwargs: Any) -> ModelT:
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def delete(self, instance: ModelT) -> None:
        await self.session.delete(instance)
        await self.session.flush()

    async def _count(self, *where: Any) -> int:
        stmt = select(func.count()).select_from(self.model)
        if where:
            stmt = stmt.where(*where)
        result = await self.session.execute(stmt)
        return result.scalar_one()
