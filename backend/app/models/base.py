from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class PortableJSON(TypeDecorator[Any]):
    """Renders as JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite in tests).

    Using a TypeDecorator keeps the model definition dialect-agnostic while still
    giving us GIN-indexable JSONB columns in production.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(AsyncAttrs, DeclarativeBase):
    """Shared declarative base for all SQLAlchemy models."""
    pass


class TimestampMixin:
    """Adds created_at / updated_at columns managed by the DB server."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
