"""Reads and writes for `agent_runs`.

Every query is tenant-scoped in the app layer as well as by RLS: the policy is the
backstop for a forgotten predicate, not a substitute for one (ADR 0015).
"""

import uuid

from app.models.agent_run import AgentRun
from app.repositories.base import BaseRepository
from sqlalchemy import desc, func, select


class AgentRunRepository(BaseRepository[AgentRun]):
    model = AgentRun

    async def get_for_tenant(
        self, run_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> AgentRun | None:
        """Tenant-scoped get. `None` for another tenant's run — never raises, never
        distinguishes "missing" from "not yours", so the id space stays opaque."""
        result = await self.session.execute(
            select(AgentRun).where(
                AgentRun.id == run_id, AgentRun.tenant_id == tenant_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        alert_id: uuid.UUID | None = None,
        active: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AgentRun], int]:
        """One page of runs, newest first, plus the true total.

        `active=True` means `finished_at IS NULL` — a run nobody has closed — and
        `active=False` its complement. Both filters run in SQL, so `total` is a count
        of what the caller asked for rather than of the table.
        """
        filters = [AgentRun.tenant_id == tenant_id]
        if alert_id is not None:
            filters.append(AgentRun.alert_id == alert_id)
        if active is True:
            filters.append(AgentRun.finished_at.is_(None))
        elif active is False:
            filters.append(AgentRun.finished_at.is_not(None))

        total = (
            await self.session.execute(
                select(func.count()).select_from(AgentRun).where(*filters)
            )
        ).scalar_one()
        result = await self.session.execute(
            select(AgentRun)
            .where(*filters)
            .order_by(desc(AgentRun.started_at), desc(AgentRun.id))
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all()), total
