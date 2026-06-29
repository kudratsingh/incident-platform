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
