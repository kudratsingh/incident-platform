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

    # Force a non-empty window, then stub the paid call. The route composes
    # the three phases itself (WO-R2-127) rather than calling the combined
    # `run_digest_for_tenant`, so the stubs go on the parts.
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
    fake_digest_row.created_at = datetime.now(UTC)  # not set by SQLAlchemy default in this stub

    try:
        with patch(
            "app.services.incident_digest.collect_window_stats",
            new=AsyncMock(return_value=({"completed": 1}, {}, [])),
        ), patch(
            "app.services.incident_digest.generate_digest",
            new=AsyncMock(return_value=(object(), {"input_tokens": 200}, "claude-opus-4-7")),
        ), patch(
            "app.services.incident_digest.persist_digest",
            new=AsyncMock(return_value=fake_digest_row),
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


@pytest.mark.parametrize(
    ("requested", "expected_hours"),
    [
        ("garbage", 24),  # unparseable -> settings.llm_digest_window_hours
        (None, 24),  # omitted -> same default
        (0, 1),  # below the floor -> clamped up
        (-5, 1),
        (10_000, 168),  # above the ceiling -> clamped down
        (169, 168),
        (1, 1),  # the bounds themselves pass through
        (168, 168),
        (72, 72),  # an in-range value is honoured, not silently defaulted
    ],
)
async def test_generate_digest_clamps_hours(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    requested: object,
    expected_hours: int,
) -> None:
    """`hours` is clamped to 1..168, junk falls back to the default.

    The clamp is only observable in the *window* the route hands to the
    read phase, so this inspects the arguments the stub was actually called
    with. Stubbing the call and asserting only that it was awaited — which
    is what this test used to do, with a single unparseable input — passes
    with the clamp deleted outright.

    The stub moved from `run_digest_for_tenant` to `collect_window_stats`
    when WO-R2-127 split the route into read / call / write phases; the
    window is now an argument to the read, and `generate_digest` is the
    paid call that must not happen at all when the window comes back empty.
    """
    from datetime import timedelta

    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    fake = AsyncMock(return_value=None)
    paid = AsyncMock()
    body: dict[str, object] = {} if requested is None else {"hours": requested}
    try:
        with patch(
            "app.services.incident_digest.collect_window_stats", new=fake
        ), patch("app.services.incident_digest.generate_digest", new=paid):
            resp = await client.post(
                "/api/v1/admin/digests/generate",
                json=body,
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 201
    fake.assert_awaited_once()
    _session, _tenant, window_start, window_end = fake.await_args.args
    assert window_end - window_start == timedelta(hours=expected_hours)

    # The same window is what the caller is told it got, so a clamp that
    # only fixed up the reported window would not pass either.
    payload = resp.json()
    assert payload["window_start"] == window_start.isoformat()
    assert payload["window_end"] == window_end.isoformat()

    # And an empty window must not reach the paid call at all — the read
    # phase is what decides that, and it now runs before the round-trip
    # rather than inside the same composed call (WO-R2-127).
    paid.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# WO-R2-127 — the digest write runs under a re-established tenant context
# ---------------------------------------------------------------------------


async def test_generate_digest_reestablishes_rls_context_for_the_write(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`app.tenant_id` is a transaction-local GUC, so the INSERT's transaction
    has to set it for itself.

    R2-63 moved the worker's digest onto read / call / write transactions so
    the Anthropic round-trip holds no connection; the route kept the composed
    single-transaction form, which is the residue #183 documented and could
    not fix. Splitting the route the same way is what creates the hazard this
    asserts: `get_current_user` set the GUC on the *request's* transaction, and
    the write phase is not that transaction. An unscoped INSERT is not
    rejected — every `tenant_isolation` policy's bootstrap branch
    (`current_setting(...) IS NULL OR ... = ''`) admits it — so the row lands
    with RLS not standing behind it at all. That silence is the hazard;
    `tests/integration/test_rls_enforcement.py` proves both halves on a live
    server.

    Asserted on the call rather than on Postgres behaviour because the unit
    tier runs on SQLite, where `_set_rls_tenant` is a deliberate no-op;
    `tests/integration/test_rls_enforcement.py` is where the policy itself is
    proven against a live server.
    """
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    from app.api import admin as admin_mod

    scoped: list[object] = []
    real_set_rls = admin_mod._set_rls_tenant

    async def _spy(db, tenant_id):  # type: ignore[no-untyped-def]
        scoped.append((db, tenant_id))
        return await real_set_rls(db, tenant_id)

    persisted_on: list[object] = []

    async def _fake_persist(session, *a, **kw):  # type: ignore[no-untyped-def]
        persisted_on.append(session)
        row = DigestRow(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID("d3fa17de-7a17-de7a-17de-7a17de7a17de"),
            window_start=datetime.now(UTC) - timedelta(hours=1),
            window_end=datetime.now(UTC),
            summary="s",
            highlights={},
            model_used="m",
            usage={},
        )
        row.created_at = datetime.now(UTC)
        return row

    try:
        with patch.object(admin_mod, "_set_rls_tenant", new=_spy), patch(
            "app.services.incident_digest.collect_window_stats",
            new=AsyncMock(return_value=({"completed": 1}, {}, [])),
        ), patch(
            "app.services.incident_digest.generate_digest",
            new=AsyncMock(return_value=(object(), {}, "m")),
        ), patch(
            "app.services.incident_digest.persist_digest", new=_fake_persist
        ):
            resp = await client.post(
                "/api/v1/admin/digests/generate",
                json={"hours": 1},
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 201, resp.json()
    assert persisted_on, "persist_digest was never reached"
    write_session = persisted_on[0]

    # The context was established on the very session the INSERT used, not
    # merely somewhere in the request.
    assert any(
        db is write_session for db, _tid in scoped
    ), "the write session never had app.tenant_id set"

    # And it was scoped to the caller's tenant, not left at whatever the
    # previous transaction happened to hold.
    tenant_ids = {tid for db, tid in scoped if db is write_session}
    assert tenant_ids == {uuid.UUID("d3fa17de-7a17-de7a-17de-7a17de7a17de")}


async def test_generate_digest_does_not_hold_the_read_open_across_the_paid_call(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of R2-63, applied to the route.

    The aggregate read and the INSERT are separate transactions with the
    Anthropic round-trip between them, so a slow model does not pin the
    digest's connection `idle in transaction`. Pinned by observing that the
    read and the write are handed different sessions — the property that
    re-composing them into one would destroy.
    """
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("LLM_DIGEST_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    seen: dict[str, object] = {}

    async def _fake_collect(session, *a, **kw):  # type: ignore[no-untyped-def]
        seen["read"] = session
        return ({"completed": 1}, {}, [])

    async def _fake_persist(session, *a, **kw):  # type: ignore[no-untyped-def]
        seen["write"] = session
        row = DigestRow(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID("d3fa17de-7a17-de7a-17de-7a17de7a17de"),
            window_start=datetime.now(UTC) - timedelta(hours=1),
            window_end=datetime.now(UTC),
            summary="s",
            highlights={},
            model_used="m",
            usage={},
        )
        row.created_at = datetime.now(UTC)
        return row

    try:
        with patch(
            "app.services.incident_digest.collect_window_stats", new=_fake_collect
        ), patch(
            "app.services.incident_digest.generate_digest",
            new=AsyncMock(return_value=(object(), {}, "m")),
        ), patch(
            "app.services.incident_digest.persist_digest", new=_fake_persist
        ):
            resp = await client.post(
                "/api/v1/admin/digests/generate",
                json={"hours": 1},
                headers=admin_headers,
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 201, resp.json()
    assert seen["read"] is not seen["write"]
