"""The A3 world on a real Postgres: an open breaker with a healthy database (WO-R3-220, WP-8.4).

Four claims. The flag drives the *shipped* `bulk-api-sync` breaker to OPEN; the failure it causes
reaches the jobs table, so `search_traces(job_type=…, status="failed")` — the corroborating
evidence 01 §7.3 names — has a row to find; Postgres and Redis read healthy in the same window, so
the discrimination against A1/A2 is real; and `mode=slow` does none of it. The fourth test is
divergence H7 written down as a test: a registry in another process reads the same breaker closed.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import pytest
from app.config import Settings
from app.core import circuit_breaker as breaker_mod
from app.core.circuit_breaker import CircuitState
from app.core.scopes import Scope
from app.dependencies import Principal
from app.mcp.registry import ToolContext, get_tool
from app.mcp.tools.health import (
    get_postgres_health,
    get_redis_health,
)
from app.mcp.tools.traces import SearchTracesInput, search_traces
from app.models.base import Base
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.saga import Saga
from app.models.tenant import Tenant
from app.models.user import User
from app.workers import async_tasks
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

try:
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    _HAS_TC = True
except Exception:  # pragma: no cover
    _HAS_TC = False


def _has_docker() -> bool:
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=True
        )
        return True
    except Exception:  # pragma: no cover - environment-dependent
        return False


pytestmark = pytest.mark.skipif(
    not _HAS_TC or not _has_docker(),
    reason="needs Docker + testcontainers[postgres]",
)

#: `jobs.saga_id` is a FK, so the table it points at has to exist even unused here.
_TABLES = [Tenant.__table__, User.__table__, Saga.__table__, Job.__table__]

#: Three endpoints is the breaker's threshold, so one job opens it.
_ENDPOINTS = 3


class _Redis:
    """The flag, plus the two calls `get_redis_health` makes."""

    def __init__(self, value: str | None = None) -> None:
        self.value = value

    async def get(self, _key: str) -> str | None:
        return self.value

    async def set(self, _key: str, value: Any, ex: int | None = None) -> bool:
        self.value = str(value)
        return True

    async def ping(self) -> bool:
        return True

    async def info(self) -> dict[str, Any]:
        return {"connected_clients": 3, "used_memory": 1024, "used_memory_human": "1K"}


async def _publish(_pct: int, _msg: str) -> None:
    return None


def _empty_input(tool_name: str) -> Any:
    """The no-argument input model of a read tool, taken from the registry rather than imported
    from behind an underscore."""
    spec = get_tool(tool_name)
    assert spec is not None, f"{tool_name} is not registered"
    return spec.input_model()


@pytest.fixture(scope="module")
def pg() -> Any:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


class _World:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self.factory = factory
        self.tenant_id = tenant_id
        self.user_id = user_id

    def ctx(self, session: AsyncSession, redis: Any) -> ToolContext:
        return ToolContext(
            db=session,
            redis=redis,
            principal=Principal(
                kind="service_account",
                tenant_id=self.tenant_id,
                scopes=frozenset(
                    {Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value}
                ),
            ),
        )

    async def submit(self) -> tuple[uuid.UUID, str]:
        """A `bulk_api_sync` job, running, with a trace id — `search_traces` drops NULL ones."""
        job_id = uuid.uuid4()
        trace_id = uuid.uuid4().hex
        async with self.factory() as session:
            async with session.begin():
                session.add(
                    Job(
                        id=job_id,
                        tenant_id=self.tenant_id,
                        user_id=self.user_id,
                        type=JobType.BULK_API_SYNC.value,
                        status=JobStatus.RUNNING.value,
                        payload={"endpoint_count": _ENDPOINTS},
                        trace_id=trace_id,
                    )
                )
        return job_id, trace_id

    async def record_failure(self, job_id: uuid.UUID, message: str) -> None:
        """The transition the dispatcher makes on a processor that raised.

        Written here rather than through `_run_job` because what this test is about is the row the
        agent's tools read; the dispatcher's own path is covered in `tests/unit/test_dispatcher.py`.
        """
        async with self.factory() as session:
            async with session.begin():
                await session.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status=JobStatus.FAILED.value,
                        error_message=message[:1000],
                    )
                )

    async def failed_bulk_api_traces(self, redis: Any) -> Any:
        async with self.factory() as session:
            return await search_traces(
                SearchTracesInput(
                    job_type=JobType.BULK_API_SYNC.value,
                    status=JobStatus.FAILED.value,
                ),
                self.ctx(session, redis),
            )


@pytest.fixture
async def world(pg: Any) -> AsyncIterator[_World]:
    engine = create_async_engine(pg.get_connection_url(), pool_size=5, max_overflow=5)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="downstream")
            )
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"owner-{user_id.hex[:8]}@example.com",
                    hashed_password="not-a-real-hash",
                    role=UserRole.USER,
                    is_active=True,
                )
            )
    yield _World(factory, tenant_id, user_id)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=list(reversed(_TABLES)))
    await engine.dispose()


@pytest.fixture(autouse=True)
def fresh_breaker() -> Iterator[None]:
    """The breaker is a process-wide singleton, so each test starts it closed."""

    def _reset() -> None:
        breaker = async_tasks.bulk_api_breaker()
        breaker._state = CircuitState.CLOSED
        breaker._failure_count = 0
        breaker._opened_at = None
        breaker._probe_in_flight = False

    _reset()
    yield
    _reset()


def _degraded(value: str) -> Any:
    return patch.multiple(
        async_tasks,
        get_settings=lambda: Settings(chaos_enabled=True, environment="test"),
        get_redis_client=lambda: _Redis(value=value),
    )


async def test_the_flag_opens_the_breaker_and_the_failure_reaches_the_jobs_table(
    world: _World,
) -> None:
    job_id, trace_id = await world.submit()
    breaker = async_tasks.bulk_api_breaker()

    with _degraded("fail:0"):
        with pytest.raises(RuntimeError) as failure:
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": _ENDPOINTS}, _publish
            )
    assert breaker.state is CircuitState.OPEN
    await world.record_failure(job_id, str(failure.value))

    found = await world.failed_bulk_api_traces(_Redis())
    assert [m.job_id for m in found.matches] == [str(job_id)]
    assert found.matches[0].trace_id == trace_id
    assert found.matches[0].job_type == JobType.BULK_API_SYNC.value


async def test_postgres_and_redis_read_healthy_in_the_same_window(
    world: _World,
) -> None:
    """The A3 discrimination: an open breaker is only a downstream fault if the shared
    dependencies are fine at the same moment."""
    breaker = async_tasks.bulk_api_breaker()
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": _ENDPOINTS}, _publish
            )
    assert breaker.state is CircuitState.OPEN

    redis = _Redis()
    empty = _empty_input("get_postgres_health")
    async with world.factory() as session:
        async with session.begin():
            ctx = world.ctx(session, redis)
            pg_health = await get_postgres_health(empty, ctx)
            redis_health = await get_redis_health(empty, ctx)
    assert pg_health.ok is True
    assert pg_health.dialect == "postgresql"
    assert redis_health.ok is True


async def test_the_same_breaker_reads_closed_from_another_process(
    world: _World,
) -> None:
    """Divergence H7, as a test rather than a paragraph: the registry is a module-level dict, so
    a second process — the MCP server (ADR 0006) — asks for `bulk-api-sync` and gets a *new*
    breaker, closed, however open the worker's is. This is what WO-R3-217 has to fix before this
    hook has a read surface; when it does, this test is the one to re-point at the shared state.
    """
    breaker = async_tasks.bulk_api_breaker()
    with _degraded("fail:0"):
        with pytest.raises(RuntimeError):
            await async_tasks.process_bulk_api_sync(
                {"endpoint_count": _ENDPOINTS}, _publish
            )
    assert breaker.state is CircuitState.OPEN

    with patch.object(breaker_mod, "_registry", {}):
        elsewhere = breaker_mod.get_circuit_breaker(
            breaker.name, failure_threshold=3, recovery_timeout=30.0
        )
        assert elsewhere is not breaker
        assert elsewhere.state is CircuitState.CLOSED


async def test_the_slow_mode_leaves_the_breaker_and_the_jobs_table_alone(
    world: _World,
) -> None:
    """The contrast that keeps `slow` from being a second way to do `fail`."""
    await world.submit()
    breaker = async_tasks.bulk_api_breaker()

    with _degraded("slow:1"):
        result = await async_tasks.process_bulk_api_sync(
            {"endpoint_count": _ENDPOINTS}, _publish
        )
    assert result["endpoints_synced"] == _ENDPOINTS
    assert breaker.state is CircuitState.CLOSED

    found = await world.failed_bulk_api_traces(_Redis())
    assert found.matches == []
