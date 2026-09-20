"""Writes and reads for the audit log."""

import uuid
from collections.abc import Sequence
from typing import Any

from app.core.logging import get_logger
from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
    REQUEST_ID_MAX_LENGTH,
    AuditLog,
)
from app.repositories.base import BaseRepository
from sqlalchemy import and_, or_, select

logger = get_logger(__name__)


class AuditRepository(BaseRepository[AuditLog]):
    """Appends audit rows for both human and machine principals, and reads
    them back for the admin console and the agent's audit tool."""

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

        `user_id` alone defaults to `principal_type='user'` mirrored into `principal_id`;
        Kafka coords are the AuditConsumer's only, feeding uq_audit_logs_kafka_coord.
        """
        if principal_type is None:
            principal_type = PRINCIPAL_TYPE_USER
        if principal_id is None and principal_type == PRINCIPAL_TYPE_USER:
            principal_id = user_id

        # Last resort — the middleware already refuses over-long correlation ids, but
        # other writers set the contextvar themselves, and losing the whole row is
        # worse than losing an id's tail. Postgres raises here where SQLite does not.
        if request_id is not None and len(request_id) > REQUEST_ID_MAX_LENGTH:
            logger.warning(
                "truncating over-long request_id for the audit row",
                extra={"action": action, "supplied_length": len(request_id)},
            )
            request_id = request_id[:REQUEST_ID_MAX_LENGTH]

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
        action_prefixes: Sequence[str] = (),
        exclude_action_prefixes: Sequence[str] = (),
        principal_type: str | None = None,
        tenant_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> tuple[list[AuditLog], int]:
        """Rows matching the filters, newest first, plus the full count.

        `total` uses the same `WHERE`, exclusions included: a caller is told only about
        rows it may read (`hidden_audit_action_prefixes`).

        `action_prefix` (one stream) and `action_prefixes` (several, OR-ed) are both
        accepted and combine into one OR — the agent's audit tool names one prefix, the
        operator console names three. Exclusions are AND-ed over the result either way,
        so a prefix that is both asked for and excluded is excluded.
        """
        filters = []
        wanted = [*([action_prefix] if action_prefix is not None else []), *action_prefixes]
        if user_id is not None:
            filters.append(AuditLog.user_id == user_id)
        if job_id is not None:
            filters.append(AuditLog.job_id == job_id)
        if action is not None:
            filters.append(AuditLog.action == action)
        if wanted:
            # `agent.*` / `chaos.*` grouping — used by the MCP audit tool to isolate one
            # machine-principal activity stream, and by the console to ask for the three
            # operator streams at once. OR-ed: they are alternatives, not a conjunction
            # no row could satisfy.
            filters.append(
                or_(*(AuditLog.action.like(f"{prefix}%") for prefix in wanted))
            )
        for excluded in exclude_action_prefixes:
            # Whole streams a caller may not see. A predicate, so `_count` reports
            # only readable rows — a `total` counting withheld rows would disclose
            # them. AND-ed with the caller's filters, so it empties a page, not errors.
            filters.append(~AuditLog.action.like(f"{excluded}%"))
        if principal_type is not None:
            filters.append(AuditLog.principal_type == principal_type)
        if tenant_id is not None:
            filters.append(AuditLog.tenant_id == tenant_id)
        if request_id is not None:
            # Correlation-id lookup (`get_trace`); ix_audit_logs_request_id.
            filters.append(AuditLog.request_id == request_id)

        where = and_(*filters) if filters else None
        total = await self._count(where) if where is not None else await self._count()

        stmt = (
            select(AuditLog)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
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
