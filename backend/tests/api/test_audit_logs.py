"""API tests for /api/v1/audit/logs — principal_type filter, response
shape carries principal_type + principal_id, human rows still visible
without the filter."""

import uuid

from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
    AuditLog,
)
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_rows(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    human_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            action="job.created",
            principal_type=PRINCIPAL_TYPE_USER,
            principal_id=human_id,
            user_id=human_id,
        )
    )
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            action="agent.tool_invoked",
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=sa_id,
            user_id=None,
            resource_type="mcp_tool",
            resource_id="get_consumer_lag",
            extra_data={
                "tool_name": "get_consumer_lag",
                "arguments": {},
                "scope_used": "telemetry:read",
                "latency_ms": 3.2,
                "outcome": "success",
            },
        )
    )
    await db.flush()


async def test_admin_sees_both_principal_types_by_default(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get("/api/v1/audit/logs", headers=admin_headers)
    assert resp.status_code == 200
    actions = {item["action"] for item in resp.json()["items"]}
    assert {"job.created", "agent.tool_invoked"} <= actions


async def test_principal_type_user_filter_hides_agent_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get(
        "/api/v1/audit/logs?principal_type=user", headers=admin_headers
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "expected at least one human row"
    assert all(row["principal_type"] == "user" for row in items)
    assert not any(row["action"] == "agent.tool_invoked" for row in items)


async def test_principal_type_service_account_filter_hides_human_rows(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get(
        "/api/v1/audit/logs?principal_type=service_account", headers=admin_headers
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "expected at least one agent row"
    assert all(row["principal_type"] == "service_account" for row in items)
    assert all(row["user_id"] is None for row in items)


async def test_bad_principal_type_returns_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get(
        "/api/v1/audit/logs?principal_type=bogus", headers=admin_headers
    )
    assert resp.status_code == 422


async def test_response_carries_principal_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get("/api/v1/audit/logs", headers=admin_headers)
    for row in resp.json()["items"]:
        assert "principal_type" in row
        assert "principal_id" in row
