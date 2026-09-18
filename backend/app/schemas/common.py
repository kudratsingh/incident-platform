from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

#: Ceiling on any `page_size` — it reaches Postgres as the LIMIT, so an unbounded
#: one is memory exhaustion. Shared so listing surfaces cannot drift (WO-R2-61).
MAX_PAGE_SIZE = 100


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    details: Any = None
    request_id: str = ""


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    has_next: bool

    @classmethod
    def build(
        cls, items: list[T], total: int, page: int, page_size: int
    ) -> "PaginatedResponse[T]":
        return cls(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            has_next=(page * page_size) < total,
        )


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
