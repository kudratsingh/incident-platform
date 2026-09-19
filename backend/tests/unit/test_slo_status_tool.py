"""`get_slo_status` — whether "latency above SLO" is a real claim (WO-R3-217, WP-8.1).

The computation already existed behind one admin REST route; nothing the agent could call
reached it, so an alert saying an objective was breached could be neither confirmed nor
refuted. What these tests hold: a real budget and burn rate off real rows; an unbounded rate
is null rather than a number no JSON can carry; the threshold that pages ships beside the
rate so the number can be read; and no-traffic reads as full budget, which the description
says is an absence of evidence rather than a healthy platform.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import app.mcp.tools  # noqa: F401  — import for @tool registration side effects
import pytest
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext, get_tool
from app.mcp.tools.slo_status import (
    GetSloStatusInput,
    GetSloStatusOutput,
    _objective,
    get_slo_status,
)
from app.models.enums import JobStatus, JobType
from app.models.job import Job
from app.services.slo import FAST_BURN_THRESHOLD, SLODefinition, _state
from sqlalchemy.ext.asyncio import AsyncSession

_COMPLETION = "job_completion_rate"


def _ctx(db: AsyncSession, tenant_id: uuid.UUID) -> ToolContext:
    return ToolContext(
        db=db,
        redis=object(),
        principal=Principal(
            kind="service_account",
            tenant_id=tenant_id,
            scopes=frozenset({Scope.TELEMETRY_READ.value}),
        ),
    )


async def _call(db: AsyncSession, tenant_id: uuid.UUID) -> GetSloStatusOutput:
    return await get_slo_status(GetSloStatusInput(), _ctx(db, tenant_id))


async def _add_jobs(
    session: AsyncSession,
    tenant_id: Any,
    user_id: Any,
    *,
    status: str,
    count: int,
) -> None:
    created = datetime.now(UTC) - timedelta(hours=1)
    for _ in range(count):
        session.add(
            Job(
                tenant_id=tenant_id,
                user_id=user_id,
                type=JobType.BULK_API_SYNC.value,
                status=status,
                payload=None,
                created_at=created,
                updated_at=created,
                started_at=created + timedelta(seconds=4),
                completed_at=created + timedelta(seconds=10),
            )
        )
    await session.flush()


def _by_id(out: GetSloStatusOutput, objective_id: str) -> Any:
    matches = [o for o in out.objectives if o.id == objective_id]
    assert matches, f"{objective_id} missing from the reading"
    return matches[0]


async def test_every_declared_objective_is_reported(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """Two objectives are declared; both come back, and `total` is not a page size."""
    out = await _call(db_session, default_tenant.id)

    assert out.total == len(out.objectives)
    assert {o.id for o in out.objectives} == {_COMPLETION, "job_dispatch_latency"}
    assert out.measured_at.tzinfo is not None


async def test_no_traffic_reads_as_full_budget(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """The reading a quiet platform gives. It is not evidence of a healthy one, which
    is why `total` ships beside it."""
    objective = _by_id(await _call(db_session, default_tenant.id), _COMPLETION)

    assert objective.total == 0
    assert objective.budget_remaining_pct == 100.0
    assert objective.burn_rate == 0.0
    assert objective.healthy is True
    assert objective.fast_burn is False


async def test_a_real_budget_and_burn_rate_off_real_rows(
    db_session: AsyncSession, default_tenant: Any, test_user: Any
) -> None:
    """5 failed of 100 against a 99% target: 5× the sustainable rate, budget gone."""
    await _add_jobs(
        db_session,
        default_tenant.id,
        test_user.id,
        status=JobStatus.COMPLETED,
        count=95,
    )
    await _add_jobs(
        db_session,
        default_tenant.id,
        test_user.id,
        status=JobStatus.DEAD_LETTER,
        count=5,
    )

    objective = _by_id(await _call(db_session, default_tenant.id), _COMPLETION)

    assert objective.total == 100
    assert objective.failed == 5
    assert objective.burn_rate is not None
    assert abs(objective.burn_rate - 5.0) < 1e-6
    assert objective.budget_remaining_pct == -100.0
    assert objective.healthy is False
    assert objective.fast_burn is False, "5× is a breach, not a page"


async def test_a_fast_burn_is_flagged_against_the_threshold_it_reports(
    db_session: AsyncSession, default_tenant: Any, test_user: Any
) -> None:
    """20 failed of 100 is 20× — above 14.4, the rate that wakes someone."""
    await _add_jobs(
        db_session, default_tenant.id, test_user.id, status=JobStatus.COMPLETED, count=80
    )
    await _add_jobs(
        db_session,
        default_tenant.id,
        test_user.id,
        status=JobStatus.DEAD_LETTER,
        count=20,
    )

    out = await _call(db_session, default_tenant.id)
    objective = _by_id(out, _COMPLETION)

    assert out.fast_burn_threshold == FAST_BURN_THRESHOLD
    assert objective.burn_rate is not None and objective.burn_rate >= FAST_BURN_THRESHOLD
    assert objective.fast_burn is True


def test_an_unbounded_burn_rate_is_null_not_a_number() -> None:
    """A 100%-target objective with one failure burns at no finite rate, and JSON has
    no spelling for infinity. Null here means unbounded, and the field says so."""
    unreachable = SLODefinition(
        id="u", name="u", description="u", target=1.0, window_hours=24, runbook_id="rb"
    )

    objective = _objective(_state(unreachable, total=10, failed=1))

    assert objective.burn_rate is None
    assert objective.fast_burn is True, "unbounded is above every threshold"
    assert objective.healthy is False


# The description rules (CLAUDE.md "Tool descriptions")


def _description() -> str:
    definition = get_tool("get_slo_status")
    assert definition is not None
    return definition.description


@pytest.mark.parametrize(
    "phrase",
    [
        "clock",
        "no arguments",
        "offset",
        "tenant",
    ],
)
def test_the_description_states_clock_paging_and_scope(phrase: str) -> None:
    assert phrase in _description().lower()


def test_the_description_warns_that_an_empty_window_is_not_a_healthy_platform() -> None:
    text = _description().lower()

    assert "0" in text
    assert "no traffic" in text or "nothing settled" in text


def test_the_tool_is_a_telemetry_read() -> None:
    definition = get_tool("get_slo_status")
    assert definition is not None
    assert definition.required_scope is Scope.TELEMETRY_READ
    assert definition.is_chaos is False
    assert definition.is_idempotent is False
