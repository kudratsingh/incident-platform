import uuid
from typing import Any

from app.models.audit import AuditLog
from app.repositories.base import BaseRepository
from sqlalchemy import and_, select


class AuditRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def log(
        self,
        action: str,
        *,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        ip_address: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Convenience wrapper — callers name what happened, repo writes the row.

        tenant_id is optional in this PR: omitting it lets the AuditLog
        model's column-level default drop the row into the bootstrap tenant.
        Phase 12 PR B makes it required and threads it through every call site.
        """
        kwargs: dict[str, Any] = {
            "action": action,
            "user_id": user_id,
            "job_id": job_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_id": request_id,
            "ip_address": ip_address,
            "extra_data": extra_data,
        }
        if tenant_id is not None:
            kwargs["tenant_id"] = tenant_id
        return await self.create(**kwargs)

    async def list_logs(
        self,
        offset: int = 0,
        limit: int = 20,
        user_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        action: str | None = None,
    ) -> tuple[list[AuditLog], int]:
        filters = []
        if user_id is not None:
            filters.append(AuditLog.user_id == user_id)
        if job_id is not None:
            filters.append(AuditLog.job_id == job_id)
        if action is not None:
            filters.append(AuditLog.action == action)

        where = and_(*filters) if filters else None
        total = await self._count(where) if where is not None else await self._count()

        stmt = (
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if where is not None:
            stmt = stmt.where(where)

        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total
