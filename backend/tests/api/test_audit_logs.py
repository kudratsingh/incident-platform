"""API tests for /api/v1/audit/logs — principal_type filter, response shape carries
principal_type + principal_id, human rows still visible without the filter, and rows are
scoped to the caller's tenant."""

import uuid

from app.core.security import create_access_token, hash_password
from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
    AuditLog,
)
from app.models.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from httpx import AsyncClient
from sqlalchemy import select
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


async def test_action_prefix_isolates_one_stream(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """WO-R3-313: `action=` is an exact match, so isolating `agent.*` or `chaos.*` —
    whole streams the console filters on — needed a prefix filter of its own."""
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get(
        "/api/v1/audit/logs?action_prefix=agent.", headers=admin_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"], "expected the agent row"
    assert all(row["action"].startswith("agent.") for row in body["items"])
    # `total` uses the same WHERE, so the pager cannot offer pages of rows the
    # filter excludes.
    assert body["total"] == len(body["items"])


async def test_action_prefix_matching_nothing_returns_an_empty_page(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get(
        "/api/v1/audit/logs?action_prefix=nothing.", headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


async def test_action_prefix_composes_with_principal_type(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    await _seed_rows(db_session, default_tenant.id)
    resp = await client.get(
        "/api/v1/audit/logs?action_prefix=agent.&principal_type=user",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    # The agent row is the only `agent.*` row and it is a service account, so
    # the two filters AND to nothing rather than one winning.
    assert resp.json()["items"] == []


async def test_chaos_rows_are_visible_to_a_human_operator_by_prefix(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """ADR 0012 withholds `chaos.*` from the AGENT's MCP reads, never from the REST
    audit API a human operator reads. The /demo page's phase strip depends on it."""
    # Seed the agent + human rows too, so an ignored filter fails this test
    # rather than passing on a single-row table.
    await _seed_rows(db_session, default_tenant.id)
    db_session.add(
        AuditLog(
            tenant_id=default_tenant.id,
            action="chaos.tool_invoked",
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            user_id=None,
        )
    )
    await db_session.flush()

    resp = await client.get(
        "/api/v1/audit/logs?action_prefix=chaos.", headers=admin_headers
    )
    assert resp.status_code == 200
    assert [row["action"] for row in resp.json()["items"]] == ["chaos.tool_invoked"]


async def test_the_world_reset_boundary_is_visible_to_a_human_operator(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """The other half of WO-R3-327. The `lab.` stream is withheld from the agent's MCP
    reads and must reach this endpoint unfiltered, payload included — the `/demo` page
    reads the boundary from here, and an operator reading the Audit tab is entitled to
    know what the reset did."""
    from app.services.operator_audit import WORLD_RESET_ACTION

    await _seed_rows(db_session, default_tenant.id)
    counters = {"chaos_keys_cleared": 4, "hot_set_reseeded": 1}
    db_session.add(
        AuditLog(
            tenant_id=default_tenant.id,
            action=WORLD_RESET_ACTION,
            principal_type=PRINCIPAL_TYPE_SERVICE_ACCOUNT,
            principal_id=uuid.uuid4(),
            user_id=None,
            resource_type="world",
            extra_data=counters,
        )
    )
    await db_session.flush()

    unfiltered = await client.get("/api/v1/audit/logs", headers=admin_headers)
    assert unfiltered.status_code == 200
    assert WORLD_RESET_ACTION in {
        row["action"] for row in unfiltered.json()["items"]
    }, "the boundary must be in the operator's unfiltered timeline"

    resp = await client.get(
        "/api/v1/audit/logs?action_prefix=lab.", headers=admin_headers
    )
    assert resp.status_code == 200
    rows = resp.json()["items"]
    assert [row["action"] for row in rows] == [WORLD_RESET_ACTION]
    assert rows[0]["extra_data"] == counters


async def test_over_long_action_prefix_returns_422(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """The bound is 200 characters since WO-R3-328, because the parameter is a comma
    list of prefixes rather than one prefix — still bounded, because it reaches a LIKE."""
    resp = await client.get(
        f"/api/v1/audit/logs?action_prefix={'x' * 201}", headers=admin_headers
    )
    assert resp.status_code == 422

    long_exclude = await client.get(
        f"/api/v1/audit/logs?exclude_prefix={'x' * 201}", headers=admin_headers
    )
    assert long_exclude.status_code == 422


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


async def test_audit_logs_scoped_to_caller_tenant(
    client: AsyncClient,
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """F1-02 regression: a tenant admin (NOT platform admin) must only see their own
    tenant's audit rows — the other tenant's rows are absent."""
    other = Tenant(
        id=uuid.uuid4(), slug="other-audit-tenant", name="Other Co.", is_active=True
    )
    db_session.add(other)
    await db_session.flush()

    await _seed_rows(db_session, default_tenant.id)
    await _seed_rows(db_session, other.id)

    admin = User(
        tenant_id=default_tenant.id,
        email="tenant-admin@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.ADMIN,
        is_active=True,
        is_platform_admin=False,
    )
    db_session.add(admin)
    await db_session.flush()
    token = create_access_token(
        {
            "sub": str(admin.id),
            "tenant_id": str(admin.tenant_id),
            "role": admin.role,
        }
    )

    resp = await client.get(
        "/api/v1/audit/logs", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "caller must still see their own tenant's rows"

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    own_ids = {str(r.id) for r in rows if str(r.tenant_id) == str(default_tenant.id)}
    other_ids = {str(r.id) for r in rows if str(r.tenant_id) == str(other.id)}
    assert other_ids, "sanity: the cross-tenant rows must exist in the DB"

    returned_ids = {item["id"] for item in items}
    assert returned_ids & other_ids == set(), "cross-tenant audit rows leaked"
    assert returned_ids <= own_ids, "every returned row must be in the caller's tenant"
    assert own_ids <= returned_ids, "caller's own rows must all be visible"
    assert resp.json()["total"] == len(own_ids)
