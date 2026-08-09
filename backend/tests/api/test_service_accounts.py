"""API contract tests for admin service-account CRUD + scope enforcement.

Covers the PR-1 test bar from the agent-platform Wave 1 plan:
  - wrong-scope 403
  - revoked-token 401
  - expired-token 401
  - scope-subset validation refuses over-scoped mint
Plus round-trip: create → mint → present the token → the scope-guarded
dependency lets the request through.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from app.config import get_settings
from app.core.scopes import Scope
from app.dependencies import (
    Principal,
    get_db,
    get_redis,
    require_scope,
)
from app.main import create_app
from app.repositories.service_account import ServiceAccountTokenRepository
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# admin CRUD
# ---------------------------------------------------------------------------


async def test_admin_creates_service_account(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers=admin_headers,
        json={
            "name": "incident-commander",
            "scopes": [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "incident-commander"
    assert body["is_active"] is True
    assert sorted(body["scopes"]) == sorted(
        [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value]
    )


async def test_non_admin_user_cannot_create_service_account(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers=auth_headers,
        json={"name": "sneaky", "scopes": [Scope.TELEMETRY_READ.value]},
    )
    assert resp.status_code == 403


async def test_tenant_admin_without_platform_flag_cannot_create_sa(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """Service-account management is a platform-operator workflow (X-01
    hop 2): a tenant admin (role=admin, is_platform_admin=False) must get
    403, exactly like the cross-tenant admin endpoints."""
    from app.core.security import create_access_token, hash_password
    from app.models.enums import UserRole
    from app.models.user import User

    tenant_admin = User(
        tenant_id=default_tenant.id,
        email="sa-tenantadmin@example.com",
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
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "not-allowed", "scopes": [Scope.TELEMETRY_READ.value]},
    )
    assert resp.status_code == 403


async def test_create_rejects_duplicate_name(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    payload = {"name": "dup", "scopes": [Scope.TELEMETRY_READ.value]}
    r1 = await client.post(
        "/api/v1/admin/service-accounts", headers=admin_headers, json=payload
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/v1/admin/service-accounts", headers=admin_headers, json=payload
    )
    assert r2.status_code == 409


async def test_create_rejects_unknown_scope(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers=admin_headers,
        json={"name": "bogus", "scopes": ["telemetry:read", "not:a:scope"]},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# mint tokens
# ---------------------------------------------------------------------------


async def _create_sa(
    client: AsyncClient, admin_headers: dict[str, str], scopes: list[str]
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers=admin_headers,
        json={"name": f"sa-{uuid.uuid4().hex[:8]}", "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_mint_returns_plaintext_once(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    resp = await client.post(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens",
        headers=admin_headers,
        json={},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["plaintext"].startswith("sa_")
    assert body["token"]["service_account_id"] == sa["id"]


async def test_mint_rejects_over_scoped_token(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Requested scope not on the account → 403."""
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    resp = await client.post(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens",
        headers=admin_headers,
        json={"scopes": [Scope.TELEMETRY_READ.value, Scope.ACTIONS_EXECUTE.value]},
    )
    assert resp.status_code == 403


async def test_revoke_token_returns_204(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    mint = await client.post(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens",
        headers=admin_headers,
        json={},
    )
    token_id = mint.json()["token"]["id"]
    resp = await client.delete(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens/{token_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# PATCH /admin/service-accounts/{id} — scope updates
# ---------------------------------------------------------------------------


async def test_patch_widens_scopes(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    resp = await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={
            "scopes": [
                Scope.TELEMETRY_READ.value,
                Scope.INCIDENTS_READ.value,
                Scope.ACTIONS_EXECUTE.value,
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["scopes"]) == {
        Scope.TELEMETRY_READ.value,
        Scope.INCIDENTS_READ.value,
        Scope.ACTIONS_EXECUTE.value,
    }


async def test_patch_narrows_scopes(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Dropping scopes is legitimate too — kill switch for a
    compromised SA. Verify PATCH accepts a strictly smaller list."""
    sa = await _create_sa(
        client,
        admin_headers,
        [Scope.TELEMETRY_READ.value, Scope.ACTIONS_EXECUTE.value],
    )
    resp = await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={"scopes": [Scope.TELEMETRY_READ.value]},
    )
    assert resp.status_code == 200
    assert resp.json()["scopes"] == [Scope.TELEMETRY_READ.value]


async def test_patch_unknown_scope_403(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    resp = await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={"scopes": ["not:a:scope"]},
    )
    assert resp.status_code == 403


async def test_patch_missing_sa_404(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    resp = await client.patch(
        f"/api/v1/admin/service-accounts/{uuid.uuid4()}",
        headers=admin_headers,
        json={"scopes": [Scope.TELEMETRY_READ.value]},
    )
    assert resp.status_code == 404


async def test_patch_non_admin_forbidden(
    client: AsyncClient,
    admin_headers: dict[str, str],
    auth_headers: dict[str, str],
) -> None:
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    resp = await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=auth_headers,
        json={"scopes": [Scope.TELEMETRY_READ.value]},
    )
    assert resp.status_code == 403


async def test_patch_idempotent_no_audit_row_when_unchanged(
    client: AsyncClient, admin_headers: dict[str, str], db_session,  # type: ignore[no-untyped-def]
) -> None:
    """update_scopes short-circuits when the new list matches — no
    audit row written. Confirms re-running seed scripts doesn't flood
    the audit log."""
    from app.models.audit import AuditLog
    from sqlalchemy import select as _select

    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    # First PATCH — actual change
    await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={
            "scopes": [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value]
        },
    )
    # Second PATCH — same scopes
    await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={
            "scopes": [Scope.INCIDENTS_READ.value, Scope.TELEMETRY_READ.value]
        },
    )
    rows = (
        await db_session.execute(
            _select(AuditLog).where(
                AuditLog.action == "service_account.scopes_updated",
                AuditLog.resource_id == sa["id"],
            )
        )
    ).scalars().all()
    # Only the first PATCH should have produced a row.
    assert len(list(rows)) == 1


async def test_patch_then_mint_yields_wider_token(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """After PATCH widens the SA, a newly minted token can carry the
    added scopes. Old tokens keep their original set — that's the
    whole point of tokens-are-immutable — but the fresh mint picks up
    the new capability."""
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={
            "scopes": [Scope.TELEMETRY_READ.value, Scope.ACTIONS_EXECUTE.value]
        },
    )
    mint = await client.post(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens",
        headers=admin_headers,
        json={
            "scopes": [Scope.TELEMETRY_READ.value, Scope.ACTIONS_EXECUTE.value]
        },
    )
    assert mint.status_code == 201
    assert set(mint.json()["token"]["scopes"]) == {
        Scope.TELEMETRY_READ.value,
        Scope.ACTIONS_EXECUTE.value,
    }


# ---------------------------------------------------------------------------
# chaos:invoke at the API boundary (X-01 hop 3)
#
# With the chaos gate closed (CHAOS_ENABLED unset/false — the default, and
# forced false in production) chaos:invoke is not grantable through the human
# API on any of the three grant paths, even by a platform admin. On a
# chaos-enabled stack (the platform's own docker-compose, which the
# incident-commander bootstrap targets) a platform admin can still grant it.
# The operator seed script provisions through the service layer and is
# unaffected either way.
# ---------------------------------------------------------------------------


async def test_api_refuses_chaos_scope_grant(
    client: AsyncClient,
    admin_headers: dict[str, str],
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """Chaos gate closed: create, PATCH, and mint all refuse chaos:invoke
    with 403; the service layer (seed-script path) still provisions it."""
    # create
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers=admin_headers,
        json={"name": "chaos-via-create", "scopes": [Scope.CHAOS_INVOKE.value]},
    )
    assert resp.status_code == 403

    # PATCH-widen
    sa = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    resp = await client.patch(
        f"/api/v1/admin/service-accounts/{sa['id']}",
        headers=admin_headers,
        json={"scopes": [Scope.TELEMETRY_READ.value, Scope.CHAOS_INVOKE.value]},
    )
    assert resp.status_code == 403

    # mint — needs an account that legitimately holds chaos:invoke, which
    # only the operator path can produce: provision through the service
    # layer exactly like scripts/seed_incident_commander.py does.
    from app.repositories.audit import AuditRepository
    from app.repositories.service_account import ServiceAccountRepository
    from app.services.service_account import ServiceAccountService

    service = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    seeded = await service.create_service_account(
        tenant_id=default_tenant.id,
        name="chaos-seeded-agent",
        scopes=[Scope.TELEMETRY_READ.value, Scope.CHAOS_INVOKE.value],
        created_by_user_id=None,
    )
    # The seed-script path is NOT gated — the agent's provisioning survives.
    assert Scope.CHAOS_INVOKE.value in seeded.scopes

    resp = await client.post(
        f"/api/v1/admin/service-accounts/{seeded.id}/tokens",
        headers=admin_headers,
        json={"scopes": [Scope.CHAOS_INVOKE.value]},
    )
    assert resp.status_code == 403


@pytest.fixture
def chaos_enabled_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Simulate a chaos-enabled stack (CHAOS_ENABLED=true), matching the
    platform docker-compose environment the incident-commander bootstrap
    script runs against."""
    monkeypatch.setenv("CHAOS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_chaos_scope_grantable_by_platform_admin_when_chaos_enabled(
    client: AsyncClient,
    admin_headers: dict[str, str],
    chaos_enabled_env: None,
) -> None:
    """Chaos gate open: a platform admin can still provision a chaos:invoke
    service account through the API. This is the incident-commander
    `make bootstrap-token` flow (create a 4-scope SA, mint with an empty
    body, PATCH-widen on re-run) and must keep working."""
    bootstrap_scopes = [
        Scope.TELEMETRY_READ.value,
        Scope.INCIDENTS_READ.value,
        Scope.ACTIONS_EXECUTE.value,
        Scope.CHAOS_INVOKE.value,
    ]
    # Fresh bootstrap run: create carries chaos:invoke directly.
    resp = await client.post(
        "/api/v1/admin/service-accounts",
        headers=admin_headers,
        json={"name": "incident-commander", "scopes": bootstrap_scopes},
    )
    assert resp.status_code == 201, resp.text
    sa = resp.json()
    assert set(sa["scopes"]) == set(bootstrap_scopes)

    # Bootstrap mints with an empty body — the token inherits all four.
    mint = await client.post(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens",
        headers=admin_headers,
        json={},
    )
    assert mint.status_code == 201, mint.text
    assert Scope.CHAOS_INVOKE.value in mint.json()["token"]["scopes"]

    # Re-run path: an SA that exists with narrower scopes is PATCH-widened.
    narrow = await _create_sa(client, admin_headers, [Scope.TELEMETRY_READ.value])
    patched = await client.patch(
        f"/api/v1/admin/service-accounts/{narrow['id']}",
        headers=admin_headers,
        json={"scopes": bootstrap_scopes},
    )
    assert patched.status_code == 200, patched.text

    # Explicit chaos mint is likewise allowed while the gate is open.
    explicit = await client.post(
        f"/api/v1/admin/service-accounts/{narrow['id']}/tokens",
        headers=admin_headers,
        json={"scopes": [Scope.CHAOS_INVOKE.value]},
    )
    assert explicit.status_code == 201, explicit.text


# ---------------------------------------------------------------------------
# Scope-guarded endpoint round-trip
#
# We mount a throwaway probe endpoint that requires `telemetry:read` and
# verify the auth path for both principal shapes:
#   - human JWT → 403 (scopes are machine-only, see ADR 0007)
#   - unknown/malformed sa token → 401
#   - correct sa token → 200
#   - sa token missing the required scope → 403
#   - revoked sa token → 401
#   - expired sa token → 401
# ---------------------------------------------------------------------------


def _build_probe_app(db_session) -> FastAPI:  # type: ignore[no-untyped-def]
    app = create_app()

    @app.get("/api/v1/_scope_probe/telemetry")
    async def _probe(
        principal: Principal = Depends(  # noqa: B008
            require_scope(Scope.TELEMETRY_READ)
        ),
    ) -> dict[str, str]:
        return {"principal": principal.kind}

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    return app


def _probe_client(db_session) -> AsyncClient:  # type: ignore[no-untyped-def]
    return AsyncClient(
        transport=ASGITransport(
            app=_build_probe_app(db_session), raise_app_exceptions=False
        ),
        base_url="http://test",
    )


async def test_scope_probe_rejects_human_jwt(
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    async with _probe_client(db_session) as ac:
        resp = await ac.get("/api/v1/_scope_probe/telemetry", headers=admin_headers)
    assert resp.status_code == 403


async def test_scope_probe_rejects_unknown_token(
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    async with _probe_client(db_session) as ac:
        resp = await ac.get(
            "/api/v1/_scope_probe/telemetry",
            headers={"Authorization": "Bearer sa_definitely_not_real"},
        )
    assert resp.status_code == 401


async def _mint_via_api(
    client: AsyncClient,
    admin_headers: dict[str, str],
    scopes: list[str],
    ttl_days: int | None = None,
) -> tuple[str, str, str]:
    sa = await _create_sa(client, admin_headers, scopes)
    payload: dict[str, object] = {}
    if ttl_days is not None:
        payload["ttl_days"] = ttl_days
    mint = await client.post(
        f"/api/v1/admin/service-accounts/{sa['id']}/tokens",
        headers=admin_headers,
        json=payload,
    )
    body = mint.json()
    return sa["id"], body["token"]["id"], body["plaintext"]


async def test_scope_probe_accepts_correct_sa_token(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    _, _, plaintext = await _mint_via_api(
        client, admin_headers, [Scope.TELEMETRY_READ.value]
    )
    async with _probe_client(db_session) as ac:
        resp = await ac.get(
            "/api/v1/_scope_probe/telemetry",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 200
    assert resp.json()["principal"] == "service_account"


async def test_scope_probe_rejects_token_missing_scope(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    # Account has incidents:read but the probe requires telemetry:read
    _, _, plaintext = await _mint_via_api(
        client, admin_headers, [Scope.INCIDENTS_READ.value]
    )
    async with _probe_client(db_session) as ac:
        resp = await ac.get(
            "/api/v1/_scope_probe/telemetry",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 403


async def test_scope_probe_rejects_revoked_token(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    sa_id, token_id, plaintext = await _mint_via_api(
        client, admin_headers, [Scope.TELEMETRY_READ.value]
    )
    revoke = await client.delete(
        f"/api/v1/admin/service-accounts/{sa_id}/tokens/{token_id}",
        headers=admin_headers,
    )
    assert revoke.status_code == 204
    async with _probe_client(db_session) as ac:
        resp = await ac.get(
            "/api/v1/_scope_probe/telemetry",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 401


async def test_scope_probe_rejects_expired_token(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    _, token_id, plaintext = await _mint_via_api(
        client, admin_headers, [Scope.TELEMETRY_READ.value], ttl_days=1
    )
    # Backdate the token by two days so it's expired
    token = await ServiceAccountTokenRepository(db_session).get_by_id(
        uuid.UUID(token_id)
    )
    assert token is not None
    token.expires_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.flush()

    async with _probe_client(db_session) as ac:
        resp = await ac.get(
            "/api/v1/_scope_probe/telemetry",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    assert resp.status_code == 401
