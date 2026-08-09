import uuid
from typing import Any

from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
    AuditLog,
)
from app.repositories.base import BaseRepository
from sqlalchemy import and_, select


class AuditRepository(BaseRepository[AuditLog]):
    model = AuditLog

    async def log(
        self,
        action: str,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        principal_type: str | None = None,
        principal_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        ip_address: str | None = None,
        extra_data: dict[str, Any] | None = None,
        kafka_topic: str | None = None,
        kafka_partition: int | None = None,
        kafka_offset: int | None = None,
    ) -> AuditLog:
        """Convenience wrapper — callers name what happened, repo writes the row.

        Principal identity: pass either `user_id` (human path, kept for
        backward compat with all existing callers) or an explicit
        `principal_type` + `principal_id` pair (machine path). When only
        `user_id` is provided we default `principal_type='user'` and mirror
        the value into `principal_id`, so every row has a populated
        principal_type going forward.

        Kafka coordinates: set only by the AuditConsumer for event.* rows —
        they feed uq_audit_logs_kafka_coord so redelivery dedups. Inline
        transactional writers leave them None (NULLs never collide).
        """
        if principal_type is None:
            principal_type = PRINCIPAL_TYPE_USER
        if principal_id is None and principal_type == PRINCIPAL_TYPE_USER:
            principal_id = user_id
        return await self.create(
            action=action,
            tenant_id=tenant_id,
            user_id=user_id,
            principal_type=principal_type,
            principal_id=principal_id,
            job_id=job_id,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            ip_address=ip_address,
            extra_data=extra_data,
            kafka_topic=kafka_topic,
            kafka_partition=kafka_partition,
            kafka_offset=kafka_offset,
        )

    async def list_logs(
        self,
        offset: int = 0,
        limit: int = 20,
        user_id: uuid.UUID | None = None,
        job_id: uuid.UUID | None = None,
        action: str | None = None,
        action_prefix: str | None = None,
        principal_type: str | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> tuple[list[AuditLog], int]:
        filters = []
        if user_id is not None:
            filters.append(AuditLog.user_id == user_id)
        if job_id is not None:
            filters.append(AuditLog.job_id == job_id)
        if action is not None:
            filters.append(AuditLog.action == action)
        if action_prefix is not None:
            # `agent.*` / `chaos.*` grouping — used by the MCP audit
            # tool to isolate machine-principal activity streams.
            filters.append(AuditLog.action.like(f"{action_prefix}%"))
        if principal_type is not None:
            filters.append(AuditLog.principal_type == principal_type)
        if tenant_id is not None:
            filters.append(AuditLog.tenant_id == tenant_id)

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


__all__ = [
    "PRINCIPAL_TYPE_SERVICE_ACCOUNT",
    "PRINCIPAL_TYPE_USER",
    "AuditRepository",
]
