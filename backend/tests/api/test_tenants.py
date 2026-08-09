"""API contract tests for the tenant model + admin CRUD."""

import uuid

from app.core.security import decode_token
from app.models.tenant import DEFAULT_TENANT_ID
from httpx import AsyncClient


async def test_login_token_carries_tenant_id(
    client: AsyncClient,
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """Newly-issued access tokens MUST include the tenant_id claim."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": test_user.email, "password": "password123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    payload = decode_token(body["access_token"], expected_type="access")
    assert payload["tenant_id"] == str(test_user.tenant_id)


async def test_register_writes_user_into_named_tenant(
    client: AsyncClient,
) -> None:
    """Default `tenant_slug='default'` lands on the bootstrap tenant."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "fresh@example.com",
            "password": "password123",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["tenant_id"] == str(DEFAULT_TENANT_ID)


async def test_register_against_unknown_tenant_returns_404(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "stranger@example.com",
            "password": "password123",
            "tenant_slug": "no-such-tenant",
        },
    )
    assert resp.status_code == 404


async def test_admin_lists_tenants(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get("/api/v1/admin/tenants", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    slugs = {item["slug"] for item in body["items"]}
    assert "default" in slugs


async def test_admin_creates_tenant(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/api/v1/admin/tenants",
        json={"slug": "acme", "name": "Acme Co."},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "acme"
    assert body["name"] == "Acme Co."
    assert body["is_active"] is True

    # And it's now visible in the list.
    list_resp = await client.get("/api/v1/admin/tenants", headers=admin_headers)
    slugs = {item["slug"] for item in list_resp.json()["items"]}
    assert "acme" in slugs


async def _audit_rows(db_session, action: str, resource_id: str):  # type: ignore[no-untyped-def]
    """Audit rows for one action + resource, newest first."""
    from app.models.audit import AuditLog
    from sqlalchemy import select

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.action == action,
            AuditLog.resource_id == resource_id,
        )
    )
    return list(result.scalars().all())


async def test_admin_create_tenant_writes_audit_row(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Creating a tenant is the most privileged operator action there is —
    it must leave an audit row like every other admin mutation (F1-08)."""
    resp = await client.post(
        "/api/v1/admin/tenants",
        json={"slug": "audited", "name": "Audited Co."},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    new_tenant_id = resp.json()["id"]

    rows = await _audit_rows(db_session, "tenant.created", new_tenant_id)
    assert len(rows) == 1
    row = rows[0]
    # The row belongs to the NEW tenant, and names the operator who made it.
    assert str(row.tenant_id) == new_tenant_id
    assert str(row.user_id) == str(admin_user.id)
    assert str(row.principal_id) == str(admin_user.id)
    assert row.principal_type == "user"
    assert row.resource_type == "tenant"
    assert row.extra_data == {"slug": "audited", "name": "Audited Co."}


async def test_admin_update_tenant_limits_writes_audit_row(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Rate-limit / quota changes are audited with their before + after
    values, so an operator can reconstruct who loosened what (F1-08)."""
    tenant_id = str(admin_user.tenant_id)
    before_resp = await client.get(
        f"/api/v1/admin/tenants/{tenant_id}", headers=admin_headers
    )
    before_quota = before_resp.json()["quota_jobs_per_month"]
    assert before_quota != 4242

    resp = await client.patch(
        f"/api/v1/admin/tenants/{tenant_id}",
        json={"quota_jobs_per_month": 4242},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["quota_jobs_per_month"] == 4242

    rows = await _audit_rows(db_session, "tenant.limits_updated", tenant_id)
    assert len(rows) == 1
    row = rows[0]
    assert str(row.tenant_id) == tenant_id
    assert str(row.user_id) == str(admin_user.id)
    assert row.resource_type == "tenant"
    assert row.extra_data["before"]["quota_jobs_per_month"] == before_quota
    assert row.extra_data["after"]["quota_jobs_per_month"] == 4242
    # Untouched field is reported unchanged, not dropped.
    assert (
        row.extra_data["before"]["rate_limit_per_minute"]
        == row.extra_data["after"]["rate_limit_per_minute"]
    )

    # A PATCH that changes nothing writes no row — the trail records changes,
    # not requests.
    noop = await client.patch(
        f"/api/v1/admin/tenants/{tenant_id}",
        json={"quota_jobs_per_month": 4242},
        headers=admin_headers,
    )
    assert noop.status_code == 200
    assert len(await _audit_rows(db_session, "tenant.limits_updated", tenant_id)) == 1


async def test_admin_create_tenant_duplicate_slug_returns_409(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    # default tenant already exists from the conftest seed.
    resp = await client.post(
        "/api/v1/admin/tenants",
        json={"slug": "default", "name": "Another Default"},
        headers=admin_headers,
    )
    assert resp.status_code == 409


async def test_admin_create_tenant_rejects_bad_slug(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/api/v1/admin/tenants",
        json={"slug": "no spaces allowed", "name": "Bad Slug"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


async def test_admin_get_tenant_includes_counts(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get(
        f"/api/v1/admin/tenants/{admin_user.tenant_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(admin_user.tenant_id)
    # At least the admin_user is in this tenant.
    assert body["users"] >= 1


async def test_admin_get_unknown_tenant_returns_404(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get(
        f"/api/v1/admin/tenants/{uuid.uuid4()}", headers=admin_headers
    )
    assert resp.status_code == 404


async def test_tenant_admin_without_platform_flag_cannot_list_tenants(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """A tenant admin (role=admin, is_platform_admin=False) must not be able
    to list sibling tenants. Only platform admins cross tenant boundaries."""
    from app.core.security import create_access_token, hash_password
    from app.models.enums import UserRole
    from app.models.user import User

    tenant_admin = User(
        tenant_id=default_tenant.id,
        email="tenantadmin@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.ADMIN,
        is_active=True,
        is_platform_admin=False,
    )
    db_session.add(tenant_admin)
    await db_session.flush()
    await db_session.refresh(tenant_admin)
    token = create_access_token(
        {
            "sub": str(tenant_admin.id),
            "tenant_id": str(tenant_admin.tenant_id),
            "role": tenant_admin.role,
        }
    )
    resp = await client.get(
        "/api/v1/admin/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_register_creates_new_tenant_on_demand(
    client: AsyncClient,
) -> None:
    """Self-service tenant creation: the registering user becomes the admin
    of a brand-new tenant when both new_tenant_name and a free slug are
    provided."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "founder@acme.example",
            "password": "password123",
            "tenant_slug": "acme",
            "new_tenant_name": "Acme Corp",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # Founder of a self-created tenant is its admin, regardless of what
    # role they requested.
    assert body["role"] == "admin"
    # is_platform_admin is NOT auto-granted — that's reserved for
    # cross-tenant operators.
    assert body["is_platform_admin"] is False


async def test_platform_admin_cross_tenant_scope(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A platform admin can pass ?tenant_id= and the endpoint scopes to it.

    We can't fully verify RLS in SQLite, but we can verify the parameter is
    plumbed through to the read-model lookup.
    """
    # First, create a second tenant via the platform admin.
    create_resp = await client.post(
        "/api/v1/admin/tenants",
        json={"slug": "beta", "name": "Beta"},
        headers=admin_headers,
    )
    assert create_resp.status_code == 201
    other_tenant_id = create_resp.json()["id"]

    # Now query stats scoped to that tenant — should not 403 / 404.
    resp = await client.get(
        f"/api/v1/admin/stats?tenant_id={other_tenant_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert "by_status" in resp.json()


async def test_token_with_mismatched_tenant_id_is_rejected(
    client: AsyncClient,
    test_user,  # type: ignore[no-untyped-def]
) -> None:
    """A token whose tenant_id claim disagrees with the user's actual tenant
    must not authenticate — defends against forged/stale tokens after a user
    is moved to a different tenant."""
    from app.core.security import create_access_token

    bogus = create_access_token(
        {
            "sub": str(test_user.id),
            "tenant_id": str(uuid.uuid4()),  # not the user's actual tenant
            "role": test_user.role,
        }
    )
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {bogus}"},
    )
    assert resp.status_code == 401
