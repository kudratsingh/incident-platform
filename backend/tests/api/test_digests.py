"""API contract tests for /admin/digests endpoints."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.config import get_settings
from app.models.digest import IncidentDigest as DigestRow
from httpx import AsyncClient


async def test_list_digests_empty_when_none_persisted(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.get("/api/v1/admin/digests", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []


async def test_list_digests_returns_persisted_rows(
    client: AsyncClient,
    admin_headers: dict[str, str],
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    from datetime import UTC, datetime, timedelta

    row = DigestRow(
        id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        window_start=datetime.now(UTC) - timedelta(hours=24),
        window_end=datetime.now(UTC),
        summary="Quiet day — 3 csv_upload retries succeeded.",
        highlights={"key_concerns": [], "recommended_actions": []},
        model_used="claude-opus-4-7",
        usage={"input_tokens": 100},
    )
    db_session.add(row)
    await db_session.flush()

    resp = await client.get("/api/v1/admin/digests", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["summary"].startswith("Quiet day")


async def test_generate_digest_disabled_returns_503(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/api/v1/admin/digests/generate", json={}, headers=admin_headers
    )
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "digest_unavailable"


async def test_generate_digest_round_trips_when_enabled(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled and the LLM returns a valid digest, the endpoint
    persists + serializes it."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    # Force a non-empty window by stubbing run_digest_for_tenant.
    fake_digest_row = DigestRow(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID("d3fa17de-7a17-de7a-17de-7a17de7a17de"),
        window_start=datetime.now(UTC) - timedelta(hours=24),
        window_end=datetime.now(UTC),
        summary="One bulk_api_sync hit a 502 and was retried successfully.",
        highlights={"key_concerns": [], "recommended_actions": []},
        model_used="claude-opus-4-7",
        usage={"input_tokens": 200, "output_tokens": 50,
               "cache_creation_input_tokens": 0,
               "cache_read_input_tokens": 1200},
    )
    fake = AsyncMock(return_value=fake_digest_row)
    fake_digest_row.created_at = datetime.now(UTC)  # not set by SQLAlchemy default in this stub

    try:
        with patch(
            "app.services.incident_digest.run_digest_for_tenant", new=fake
        ):
            resp = await client.post(
                "/api/v1/admin/digests/generate",
                json={"hours": 24},
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 201
    body = resp.json()
    assert body["summary"].startswith("One bulk_api_sync")
    assert body["model_used"] == "claude-opus-4-7"


async def test_generate_digest_clamps_hours(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A junk `hours` value falls back to the default rather than 500'ing."""
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake = AsyncMock(return_value=None)
    try:
        with patch(
            "app.services.incident_digest.run_digest_for_tenant", new=fake
        ):
            resp = await client.post(
                "/api/v1/admin/digests/generate",
                json={"hours": "garbage"},
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 201
    fake.assert_awaited_once()


async def test_cross_tenant_get_denied_without_platform_flag(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    default_tenant,  # type: ignore[no-untyped-def]
) -> None:
    """A tenant admin in tenant A can't fetch a digest belonging to tenant B."""
    from datetime import UTC, datetime, timedelta

    from app.core.security import create_access_token, hash_password
    from app.models.enums import UserRole
    from app.models.tenant import Tenant
    from app.models.user import User

    other_tenant = Tenant(
        id=uuid.uuid4(),
        slug="rival",
        name="Rival",
        is_active=True,
    )
    db_session.add(other_tenant)
    await db_session.flush()
    other_row = DigestRow(
        id=uuid.uuid4(),
        tenant_id=other_tenant.id,
        window_start=datetime.now(UTC) - timedelta(hours=24),
        window_end=datetime.now(UTC),
        summary="Rival tenant — should not be visible.",
        highlights={},
        model_used="claude-opus-4-7",
        usage={},
    )
    db_session.add(other_row)

    tenant_admin = User(
        tenant_id=default_tenant.id,
        email="ta@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.ADMIN,
        is_active=True,
        is_platform_admin=False,
    )
    db_session.add(tenant_admin)
    await db_session.flush()
    token = create_access_token(
        {
            "sub": str(tenant_admin.id),
            "tenant_id": str(tenant_admin.tenant_id),
            "role": tenant_admin.role,
        }
    )

    resp = await client.get(
        f"/api/v1/admin/digests/{other_row.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
