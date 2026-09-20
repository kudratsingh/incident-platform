"""`get_postgres_health` over the wire, with and without a published pool gauge.

The unit tier proves the record round-trips; this proves the agent's surface carries it —
same harness as `test_mcp_wave2_read_tools.py`: build the standalone MCP app, mint a
`telemetry:read` token, POST JSON-RPC, read the JSON the agent would read.

What it holds: a process that published shows up in `pools[]` with the numbers it wrote and
an age; a world where nobody has published answers `pool_gauges_unknown_reason` with an empty
group rather than an empty group on its own; and the flat `pool_*` fields, which describe the
answering process only, are unchanged either way (WO-R3-289, ADR 0033).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from app.core.pool_state import (
    POOL_GAUGES_UNKNOWN_NONE_PUBLISHED,
    PROCESS_API_WORKER,
    PROCESS_MCP,
    publish_pool_state,
)
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp.standalone import create_mcp_app
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    """Enough async Redis for this surface: SET with an expiry, GET, SCAN."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        self._store[key] = str(value)
        return True

    async def scan(
        self, cursor: int, match: str = "*", count: int = 10
    ) -> tuple[int, list[str]]:
        prefix = match.rstrip("*")
        return 0, [k for k in self._store if k.startswith(prefix)]


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    app = create_mcp_app()
    redis_stub = _RedisStub()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, redis_stub


async def _token(db_session: AsyncSession, tenant_id: uuid.UUID) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=f"probe-{uuid.uuid4().hex[:8]}",
        scopes=[Scope.TELEMETRY_READ.value],
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


async def _health(ac: AsyncClient, token: str) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": "get_postgres_health", "arguments": {}},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    return json.loads(body["result"]["content"][0]["text"])


async def test_a_published_pool_reaches_the_agents_surface(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The whole order in one assertion: a pool the answering process cannot see, read
    off the group beside the flat fields."""
    ac, redis = mcp_client
    await publish_pool_state(
        redis,
        process=PROCESS_API_WORKER,
        size=5,
        checked_out=14,
        overflow=9,
        max_overflow=10,
        wait_timeouts_1m=6,
    )
    token = await _token(db_session, default_tenant.id)

    out = await _health(ac, token)

    assert out["pool_gauges_unknown_reason"] is None
    assert len(out["pools"]) == 1
    gauge = out["pools"][0]
    assert gauge["process"] == PROCESS_API_WORKER
    assert gauge["checked_out"] == 14
    assert gauge["overflow"] == 9
    assert gauge["size"] == 5
    assert gauge["max_overflow"] == 10
    assert gauge["wait_timeouts_1m"] == 6
    assert gauge["reported_age_s"] >= 0.0
    assert gauge["written_at"]


async def test_no_gauge_reads_as_unknown_with_a_reason(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """An empty list on its own would read as "no process has a pool problem"."""
    ac, _ = mcp_client
    token = await _token(db_session, default_tenant.id)

    out = await _health(ac, token)

    assert out["pools"] == []
    assert out["pool_gauges_unknown_reason"] == POOL_GAUGES_UNKNOWN_NONE_PUBLISHED


async def test_both_processes_come_back_when_both_have_published(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, redis = mcp_client
    for process, checked_out in ((PROCESS_MCP, 1), (PROCESS_API_WORKER, 14)):
        await publish_pool_state(
            redis,
            process=process,
            size=5,
            checked_out=checked_out,
            overflow=0,
            max_overflow=10,
            wait_timeouts_1m=0,
        )
    token = await _token(db_session, default_tenant.id)

    out = await _health(ac, token)

    by_process = {p["process"]: p for p in out["pools"]}
    assert set(by_process) == {PROCESS_API_WORKER, PROCESS_MCP}
    assert by_process[PROCESS_API_WORKER]["checked_out"] == 14
    assert by_process[PROCESS_MCP]["checked_out"] == 1


async def test_an_old_gauge_reports_its_age_rather_than_being_hidden(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Staleness is the reader's judgement to make, so it is reported rather than
    silently dropped — the TTL is what removes a record that is genuinely too old."""
    ac, redis = mcp_client
    await publish_pool_state(
        redis,
        process=PROCESS_API_WORKER,
        size=5,
        checked_out=14,
        overflow=9,
        max_overflow=10,
        wait_timeouts_1m=6,
        now=datetime.now(UTC) - timedelta(seconds=30),
    )
    token = await _token(db_session, default_tenant.id)

    out = await _health(ac, token)

    assert out["pools"][0]["reported_age_s"] >= 25.0


async def test_the_flat_pool_fields_still_describe_the_answering_process(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The group is added beside them, not instead of them. Under the test suite the
    answering process runs SQLite on a pool that keeps no counters, so the flat fields
    are null *with their own reason* — which is exactly the case the group improves on."""
    ac, redis = mcp_client
    await publish_pool_state(
        redis,
        process=PROCESS_API_WORKER,
        size=5,
        checked_out=14,
        overflow=9,
        max_overflow=10,
        wait_timeouts_1m=6,
    )
    token = await _token(db_session, default_tenant.id)

    out = await _health(ac, token)

    assert out["pool_checked_out"] is None
    assert out["pool_stats_unknown_reason"] is not None
    assert out["pools"][0]["checked_out"] == 14
