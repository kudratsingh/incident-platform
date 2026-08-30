"""WO-R2-54 — the cache tools are scoped to the caller's tenant.

`invalidate_cache_key` deleted, and `get_cache_key_info` inspected, any
key under the prefix allowlist. `cache:job:{tenant_id}:{job_id}` is under
that allowlist on purpose — force-refreshing a stale job read is the
remediation the pair exists for — so a service account in tenant A could
evict tenant B's cached job (a cross-tenant write) and confirm its
existence, TTL and size (a cross-tenant existence oracle, which withholding
the payload does not close).

Driven through the real JSON-RPC surface, because the tenant now comes
from the authenticated principal and that only exists on a real request.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp.standalone import create_mcp_app
from app.models.tenant import Tenant
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from app.utils.cache import JobCache
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

_FORBIDDEN = "cache_key_forbidden"


class _RedisStub:
    """Records what it was asked for, so a refusal that still touched
    Redis is a visible failure rather than a silent one."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.seen: list[str] = []

    async def get(self, key: str) -> str | None:
        self.seen.append(key)
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        self.seen.append(key)
        return 1 if self.store.pop(key, None) is not None else 0

    async def type(self, key: str) -> str:
        self.seen.append(key)
        return "string" if key in self.store else "none"

    async def ttl(self, key: str) -> int:
        self.seen.append(key)
        return 30 if key in self.store else -2

    async def strlen(self, key: str) -> int:
        self.seen.append(key)
        return len(self.store.get(key, ""))


@pytest_asyncio.fixture
async def other_tenant(db_session: AsyncSession) -> Tenant:
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Tenant B",
        slug=f"tenant-b-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(tenant)
    await db_session.flush()
    return tenant


async def _mint_token(
    db_session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=f"cache-probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


@pytest_asyncio.fixture
async def cache_client(db_session: AsyncSession, default_tenant):  # type: ignore[no-untyped-def]
    """Yields (client, redis, token) for a principal in the default tenant
    holding both cache scopes."""
    app = create_mcp_app()
    redis = _RedisStub()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    token = await _mint_token(
        db_session,
        default_tenant.id,
        [Scope.TELEMETRY_READ.value, Scope.ACTIONS_EXECUTE.value],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, redis, token


async def _call(
    ac: AsyncClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return dict(resp.json())


# ---------------------------------------------------------------------------
# Cross-tenant keys are refused
# ---------------------------------------------------------------------------


async def test_invalidate_refuses_another_tenants_job_cache(
    cache_client, other_tenant: Tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, redis, token = cache_client
    victim = JobCache._key(uuid.uuid4(), other_tenant.id)
    redis.store[victim] = '{"id": "..."}'

    body = await _call(
        ac,
        token,
        "invalidate_cache_key",
        {"key": victim, "idempotency_key": uuid.uuid4().hex},
    )

    assert "error" in body, body
    assert body["error"]["data"]["error_code"] == _FORBIDDEN
    assert victim in redis.store, "the other tenant's entry was evicted"
    assert victim not in redis.seen, "refused, but Redis was touched anyway"


async def test_cache_key_info_refuses_another_tenants_job_cache(
    cache_client, other_tenant: Tenant  # type: ignore[no-untyped-def]
) -> None:
    """The existence oracle: shape-only is not a defence when existence is
    the thing being asked about."""
    ac, redis, token = cache_client
    victim = JobCache._key(uuid.uuid4(), other_tenant.id)
    redis.store[victim] = '{"id": "..."}'

    body = await _call(ac, token, "get_cache_key_info", {"key": victim})

    assert "error" in body, body
    assert body["error"]["data"]["error_code"] == _FORBIDDEN
    assert victim not in redis.seen


async def test_the_refusal_does_not_reveal_whether_the_key_exists(
    cache_client, other_tenant: Tenant  # type: ignore[no-untyped-def]
) -> None:
    """A refusal that varied with what is in Redis would rebuild the
    oracle it closes."""
    ac, redis, token = cache_client
    present = JobCache._key(uuid.uuid4(), other_tenant.id)
    absent = JobCache._key(uuid.uuid4(), other_tenant.id)
    redis.store[present] = '{"id": "..."}'

    for_present = await _call(ac, token, "get_cache_key_info", {"key": present})
    for_absent = await _call(ac, token, "get_cache_key_info", {"key": absent})

    assert for_present["error"]["code"] == for_absent["error"]["code"]
    assert for_present["error"]["data"] == for_absent["error"]["data"]
    # Messages differ only by the key the caller itself supplied.
    assert for_present["error"]["message"].replace(
        present, "K"
    ) == for_absent["error"]["message"].replace(absent, "K")


async def test_a_malformed_tenant_segment_is_refused(
    cache_client,  # type: ignore[no-untyped-def]
) -> None:
    """`cache:job:` with a non-UUID segment cannot be this principal's
    tenant, so it is refused rather than compared as a string."""
    ac, redis, token = cache_client
    body = await _call(
        ac, token, "get_cache_key_info", {"key": "cache:job:not-a-uuid:xyz"}
    )
    assert "error" in body, body
    assert body["error"]["data"]["error_code"] == _FORBIDDEN


# ---------------------------------------------------------------------------
# The remediation loop still works inside your own tenant
# ---------------------------------------------------------------------------


async def test_own_tenant_job_cache_stays_inspectable_and_deletable(
    cache_client, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The regression guard. `cache:job:` is allowlisted precisely so an
    agent can force-refresh a stale job read — scoping must not cost that."""
    ac, redis, token = cache_client
    mine = JobCache._key(uuid.uuid4(), default_tenant.id)
    redis.store[mine] = '{"id": "..."}'

    info = await _call(ac, token, "get_cache_key_info", {"key": mine})
    assert "result" in info, info
    assert json.loads(info["result"]["content"][0]["text"])["exists"] is True

    deleted = await _call(
        ac,
        token,
        "invalidate_cache_key",
        {"key": mine, "idempotency_key": uuid.uuid4().hex},
    )
    assert "result" in deleted, deleted
    assert json.loads(deleted["result"]["content"][0]["text"])["deleted"] is True
    assert mine not in redis.store


async def test_platform_global_keys_are_unaffected(
    cache_client,  # type: ignore[no-untyped-def]
) -> None:
    """Key families with no tenant segment have no tenant to compare and
    expose nothing of one tenant's — they stay reachable on the allowlist
    alone, or the metrics-cache and hot_set remediations break."""
    ac, redis, token = cache_client
    for key in (
        "cache:jobs:worker-dispatcher:hot_set",
        "kafka:consumer_lag:worker-dispatcher",
        "read_model:something",
    ):
        redis.store[key] = "x"
        body = await _call(ac, token, "get_cache_key_info", {"key": key})
        assert "result" in body, (key, body)


async def test_keys_outside_the_allowlist_are_still_refused_first(
    cache_client,  # type: ignore[no-untyped-def]
) -> None:
    """The prefix gate keeps its own message — tenant scoping is the
    second gate, not a replacement."""
    ac, _redis, token = cache_client
    body = await _call(ac, token, "get_cache_key_info", {"key": "jobs:tenant:x"})
    assert body["error"]["data"]["error_code"] == _FORBIDDEN
    assert "readable namespace" in body["error"]["message"]
