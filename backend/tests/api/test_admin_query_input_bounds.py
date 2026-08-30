"""Bounds on the admin query inputs that reach SQL (WO-R2-61).

Three inputs on the admin surface were validated by hand, or not at all,
and each one reached Postgres:

  * `page` / `page_size` on `GET /admin/users` and `GET /admin/tenants`
    were bare `int` defaults, so `offset=(page - 1) * page_size` sent a
    negative OFFSET for any page below 1.
  * `PATCH /admin/tenants/{id}` took an untyped `dict` and checked
    `isinstance(value, int)` — which is `True` for a Python bool, so a
    JSON `true` was written as a rate limit of 1.

The repair is structural (Field bounds and a typed body model), so these
tests assert against the *rejection*, not against a particular hand-rolled
message.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.parametrize("path", ["/api/v1/admin/users", "/api/v1/admin/tenants"])
@pytest.mark.parametrize("page", [0, -1, -1000])
async def test_page_below_one_is_rejected(
    client: AsyncClient,
    admin_headers: dict[str, str],
    path: str,
    page: int,
) -> None:
    """A page below 1 makes `(page - 1) * page_size` negative, and
    Postgres refuses a negative OFFSET with a 500 rather than a 422 —
    the caller's bad input rendered as the server's fault."""
    resp = await client.get(f"{path}?page={page}", headers=admin_headers)
    assert resp.status_code == 422


@pytest.mark.parametrize("path", ["/api/v1/admin/users", "/api/v1/admin/tenants"])
@pytest.mark.parametrize("page_size", [0, -5, 100_000])
async def test_page_size_outside_bounds_is_rejected(
    client: AsyncClient,
    admin_headers: dict[str, str],
    path: str,
    page_size: int,
) -> None:
    """`page_size` needs a floor (0 and below are meaningless as a LIMIT)
    and a ceiling (an unbounded LIMIT is a one-request memory exhaustion)."""
    resp = await client.get(
        f"{path}?page_size={page_size}", headers=admin_headers
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("path", ["/api/v1/admin/users", "/api/v1/admin/tenants"])
async def test_first_page_still_works(
    client: AsyncClient, admin_headers: dict[str, str], path: str
) -> None:
    """The bounds must not move the happy path."""
    resp = await client.get(f"{path}?page=1&page_size=10", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["page"] == 1
    assert resp.json()["page_size"] == 10


@pytest.mark.parametrize("field", ["rate_limit_per_minute", "quota_jobs_per_month"])
@pytest.mark.parametrize("value", [True, False])
async def test_boolean_is_not_a_tenant_limit(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
    field: str,
    value: bool,
) -> None:
    """`isinstance(True, int)` is `True` in Python, so a JSON `true`
    passed the hand-rolled guard and silently became a rate limit of 1 —
    a tenant throttled to one request a minute by a typo."""
    resp = await client.patch(
        f"/api/v1/admin/tenants/{admin_user.tenant_id}",
        json={field: value},
        headers=admin_headers,
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("field", ["rate_limit_per_minute", "quota_jobs_per_month"])
@pytest.mark.parametrize("value", [-1, "120", 12.5, None, 2**40])
async def test_non_integer_tenant_limits_are_rejected(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
    field: str,
    value: object,
) -> None:
    """Negatives, strings, floats, explicit null, and anything wider than
    the `INTEGER` column are all refused before they reach the UPDATE."""
    resp = await client.patch(
        f"/api/v1/admin/tenants/{admin_user.tenant_id}",
        json={field: value},
        headers=admin_headers,
    )
    assert resp.status_code == 422


async def test_unknown_tenant_limit_field_is_rejected(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """A misspelled field used to be dropped on the floor and answered
    200, so an operator who thought they had raised a quota had not."""
    resp = await client.patch(
        f"/api/v1/admin/tenants/{admin_user.tenant_id}",
        json={"rate_limit_per_min": 500},
        headers=admin_headers,
    )
    assert resp.status_code == 422


async def test_valid_tenant_limits_still_apply(
    client: AsyncClient,
    admin_user,  # type: ignore[no-untyped-def]
    admin_headers: dict[str, str],
) -> None:
    """Zero stays legal — it is how an operator disables a check — and a
    partial body still leaves the untouched field alone."""
    tenant_id = str(admin_user.tenant_id)
    before = (
        await client.get(
            f"/api/v1/admin/tenants/{tenant_id}", headers=admin_headers
        )
    ).json()

    resp = await client.patch(
        f"/api/v1/admin/tenants/{tenant_id}",
        json={"rate_limit_per_minute": 0},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["rate_limit_per_minute"] == 0
    assert (
        resp.json()["quota_jobs_per_month"] == before["quota_jobs_per_month"]
    )


async def test_tenant_limits_validation_precedes_the_lookup(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Body validation runs before the tenant is fetched, so a bad body
    against an unknown tenant is still a 422 and never a 404 — the
    caller learns about every problem with their request at once."""
    resp = await client.patch(
        f"/api/v1/admin/tenants/{uuid.uuid4()}",
        json={"rate_limit_per_minute": True},
        headers=admin_headers,
    )
    assert resp.status_code == 422
