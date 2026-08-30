"""Who may join which tenant, and on whose say-so (WO-R2-25, ADR 0024).

`POST /auth/register` is unauthenticated and took a free-form `tenant_slug`,
with no auth, no invite and no domain check. The founder branch only fires
when the slug is *free*, so naming a slug that already existed was precisely
the path that enrolled a stranger into someone else's tenant.

The verifier tempered the impact — the registrant lands on the least-privileged
role, so today's damage is shared quota and rate-limit exhaustion, idempotency
squatting and roster pollution — but noted it rises the moment any
tenant-scoped read opens to `role=user`, and that no document recorded open
self-enrolment as a decision. These tests are that decision, executable.
"""

from app.models.tenant import DEFAULT_TENANT_ID
from httpx import AsyncClient


async def _register(
    client: AsyncClient, email: str, **extra: object
) -> object:
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "password123", **extra},
    )


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


async def test_registering_into_someone_elses_tenant_is_refused(
    client: AsyncClient,
) -> None:
    """THE assertion for this order.

    A founder creates `acme`. A stranger with no relationship to it names it
    in an unauthenticated register call. Before this change that returned 201
    and put them on the roster; it must now be refused.
    """
    founded = await _register(
        client,
        "founder@acme.example",
        tenant_slug="acme",
        new_tenant_name="Acme Corp",
    )
    assert founded.status_code == 201  # type: ignore[attr-defined]

    intruder = await _register(
        client, "stranger@elsewhere.example", tenant_slug="acme"
    )

    assert intruder.status_code == 403, (  # type: ignore[attr-defined]
        "an unauthenticated caller joined an existing tenant it has no "
        "relationship with"
    )


async def test_a_squatter_cannot_slip_in_via_new_tenant_name(
    client: AsyncClient,
) -> None:
    """The founder branch must not become a side door.

    Passing `new_tenant_name` for a slug that is already taken used to fall
    straight through to "join it" — the creation was skipped and nothing else
    stopped them. It is the same enrolment, wearing the founder path's
    clothes.
    """
    await _register(
        client,
        "founder2@beta.example",
        tenant_slug="beta",
        new_tenant_name="Beta Inc",
    )

    resp = await _register(
        client,
        "squatter@elsewhere.example",
        tenant_slug="beta",
        new_tenant_name="Beta Inc (definitely mine)",
    )

    assert resp.status_code == 403  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The paths that must keep working
# ---------------------------------------------------------------------------


async def test_the_founder_path_still_creates_a_tenant_and_its_admin(
    client: AsyncClient,
) -> None:
    """A brand-new slug has nobody to harm, so it stays open — and the
    founder is still the tenant's admin, still not a platform admin."""
    resp = await _register(
        client,
        "founder@gamma.example",
        tenant_slug="gamma",
        new_tenant_name="Gamma Ltd",
    )

    assert resp.status_code == 201  # type: ignore[attr-defined]
    body = resp.json()  # type: ignore[attr-defined]
    assert body["role"] == "admin"
    assert body["is_platform_admin"] is False


async def test_the_default_tenant_path_still_works(
    client: AsyncClient,
) -> None:
    """The shared default tenant is open by design — it is the self-serve
    pool, and closing it would break the front door rather than the hole."""
    resp = await _register(client, "newcomer@example.com")

    assert resp.status_code == 201  # type: ignore[attr-defined]
    assert resp.json()["tenant_id"] == str(DEFAULT_TENANT_ID)  # type: ignore[attr-defined]


async def test_an_unknown_tenant_is_still_a_404(
    client: AsyncClient,
) -> None:
    """Unchanged: naming a slug that does not exist, without asking to create
    it, is a missing tenant and not a refusal. The two answers are different
    on purpose — see the disclosure note in ADR 0024."""
    resp = await _register(
        client, "nobody@example.com", tenant_slug="no-such-tenant"
    )

    assert resp.status_code == 404  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The authenticated way in
# ---------------------------------------------------------------------------


async def test_an_admin_can_enrol_someone_into_their_own_tenant(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Closing public enrolment must not strand founders.

    Without this endpoint a founder could create a tenant and never add a
    colleague to it, because the path this order shuts was the only one there
    was. The new account is a plain user, in the admin's own tenant.
    """
    resp = await client.post(
        "/api/v1/auth/tenant/members",
        headers=admin_headers,
        json={"email": "colleague@example.com", "password": "password123"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "user"
    assert body["tenant_id"] == str(DEFAULT_TENANT_ID)
    assert body["is_platform_admin"] is False


async def test_enrolment_requires_admin(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """A plain user cannot grow the roster — otherwise the endpoint is just
    the hole again, one login later."""
    resp = await client.post(
        "/api/v1/auth/tenant/members",
        headers=auth_headers,
        json={"email": "friend@example.com", "password": "password123"},
    )

    assert resp.status_code == 403


async def test_enrolment_requires_authentication(
    client: AsyncClient,
) -> None:
    """The whole point: this door needs a key."""
    resp = await client.post(
        "/api/v1/auth/tenant/members",
        json={"email": "anon@example.com", "password": "password123"},
    )

    assert resp.status_code in (401, 403)


async def test_enrolment_body_cannot_choose_a_tenant_or_a_role(
    client: AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """The fields are absent, so extra ones are ignored rather than honoured.

    This is the same defect one layer up: a tenant identifier the caller
    chooses. The admin's own tenant is the only possible destination, and
    `role` is fixed at `user` no matter what is asked for.
    """
    resp = await client.post(
        "/api/v1/auth/tenant/members",
        headers=admin_headers,
        json={
            "email": "sneaky@example.com",
            "password": "password123",
            "tenant_slug": "acme",
            "role": "admin",
            "is_platform_admin": True,
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "user"
    assert body["tenant_id"] == str(DEFAULT_TENANT_ID)
    assert body["is_platform_admin"] is False
