"""Per-principal and per-identity rate limiting (WO-R2-30).

Three surfaces that made a paid or pool-consuming call per request with
no limiter of any kind, while `CLAUDE.md` and `docs/REDIS.md` asserted
the control existed:

  * `POST /mcp` — no rate limiting at all, despite CLAUDE.md's "every
    MCP request ... is rate-limited per principal" since the server
    shipped. A tool-call storm saturates the MCP process's DB pool
    (SQLAlchemy defaults: 5 + 10 overflow = 15 connections).
  * `POST /admin/query` — one Anthropic call per request (~$0.006).
  * `POST /admin/digests/generate` — one Anthropic call per request
    (~$0.018).

Plus the window-semantics test: the shared limiter is a **fixed**
window that three docstrings and two docs called *sliding*. The
boundary test below pins the behaviour that actually exists so the
`2 * limit` bound is a checked claim rather than a comment.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from app.config import get_settings
from app.core.exceptions import RateLimitError
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.nl_query import JobFilterSpec
from app.services.service_account import ServiceAccountService
from app.utils.rate_limit import check_identity_rate_limit
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _CountingRedis:
    """Redis double that actually counts, so a limiter can trip.

    The MCP suite's existing `_RedisStub` implements only `get`, which
    means an `incr` against it raises `AttributeError` and the limiter
    *fails open* — the tests would pass whether or not the limiter was
    wired up at all. Counting for real is the whole point here.
    """

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.expires: dict[str, int] = {}
        self.fail = False

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis is down")
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, ttl: int) -> bool:
        if self.fail:
            raise ConnectionError("redis is down")
        self.expires[key] = ttl
        return True

    async def get(self, key: str) -> None:
        return None


@pytest.fixture
def tight_limits(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Shrink every ceiling to 2 so the tests are about the mechanism
    rather than about issuing 120 requests."""
    monkeypatch.setenv("MCP_RATE_LIMIT_PER_PRINCIPAL", "2")
    monkeypatch.setenv("ADMIN_NL_QUERY_RATE_LIMIT", "2")
    monkeypatch.setenv("ADMIN_DIGEST_RATE_LIMIT", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# MCP surface — per principal
# ---------------------------------------------------------------------------


async def _mint_token(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    scopes: list[str],
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=default_tenant.id,
        name=f"probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


@pytest_asyncio.fixture
async def mcp_rl_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
    tight_limits,
):
    """MCP app built *after* `tight_limits`, because the factory reads
    the ceilings at construction time."""
    app = create_mcp_app()
    redis = _CountingRedis()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, redis


def _rpc(method: str, id: str = "1") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": {}}


async def test_mcp_refuses_one_principal_past_its_ceiling(
    mcp_rl_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    """N calls pass, N+1 is refused. The headline of the order: before
    this, `POST /mcp` had no limiter of any kind."""
    ac, _redis = mcp_rl_client
    token = await _mint_token(db_session, default_tenant, [Scope.TELEMETRY_READ.value])
    headers = {"Authorization": f"Bearer {token}"}
    limit = get_settings().mcp_rate_limit_per_principal

    for n in range(limit):
        resp = await ac.post("/mcp", json=_rpc("tools/list"), headers=headers)
        assert "result" in resp.json(), f"call {n + 1} of {limit} should pass"

    resp = await ac.post("/mcp", json=_rpc("tools/list"), headers=headers)
    body = resp.json()
    assert resp.status_code == 429
    assert body["error"]["code"] == protocol.MCP_RATE_LIMITED
    # Still a well-formed JSON-RPC envelope — an MCP client that cannot
    # parse the response reads it as a transport failure and retries,
    # which is the opposite of backing off.
    assert body["jsonrpc"] == "2.0"


async def test_mcp_limit_is_per_principal_not_global(
    mcp_rl_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    """A second principal is unaffected by the first exhausting its
    allowance. This is the property the *name* of the control promises,
    and the one an IP-keyed limiter would not deliver: both principals
    reach the MCP process from the same address."""
    ac, redis = mcp_rl_client
    limit = get_settings().mcp_rate_limit_per_principal

    noisy = await _mint_token(db_session, default_tenant, [Scope.TELEMETRY_READ.value])
    quiet = await _mint_token(db_session, default_tenant, [Scope.TELEMETRY_READ.value])

    for _ in range(limit + 1):
        await ac.post("/mcp", json=_rpc("tools/list"), headers={"Authorization": f"Bearer {noisy}"})

    # The noisy principal is now over.
    over = await ac.post(
        "/mcp", json=_rpc("tools/list"), headers={"Authorization": f"Bearer {noisy}"}
    )
    assert over.json()["error"]["code"] == protocol.MCP_RATE_LIMITED

    # The quiet one is untouched.
    ok = await ac.post(
        "/mcp", json=_rpc("tools/list"), headers={"Authorization": f"Bearer {quiet}"}
    )
    assert "result" in ok.json()

    # Two distinct buckets, not one shared counter.
    assert len([k for k in redis.counters if k.startswith("rate:mcp:principal:")]) == 2


async def test_mcp_fails_open_when_redis_is_down(
    mcp_rl_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    """House posture: no signal is not a reason to reject. A Redis
    outage must not take the agent's whole surface down with it."""
    ac, redis = mcp_rl_client
    token = await _mint_token(db_session, default_tenant, [Scope.TELEMETRY_READ.value])
    headers = {"Authorization": f"Bearer {token}"}
    redis.fail = True

    for _ in range(get_settings().mcp_rate_limit_per_principal + 3):
        resp = await ac.post("/mcp", json=_rpc("tools/list"), headers=headers)
        assert "result" in resp.json()


async def test_mcp_unauthenticated_initialize_is_not_rate_limited(
    mcp_rl_client,  # type: ignore[no-untyped-def]
) -> None:
    """`initialize` is deliberately allowed unauthenticated, so there is
    no principal to key on. It must not consume — or be refused by — a
    per-principal bucket."""
    ac, redis = mcp_rl_client
    for _ in range(get_settings().mcp_rate_limit_per_principal + 3):
        resp = await ac.post("/mcp", json=_rpc("initialize"))
        assert "result" in resp.json()
    assert not [k for k in redis.counters if k.startswith("rate:mcp:principal:")]


async def test_mcp_malformed_request_does_not_consume_allowance(
    mcp_rl_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
) -> None:
    """Parse errors are charged to nobody: the request never reached a
    tool, and letting bad framing drain a good caller's bucket would
    make the limiter a denial-of-service vector against its own
    principal."""
    ac, redis = mcp_rl_client
    token = await _mint_token(db_session, default_tenant, [Scope.TELEMETRY_READ.value])
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(get_settings().mcp_rate_limit_per_principal + 3):
        resp = await ac.post("/mcp", json={"not": "json-rpc"}, headers=headers)
        assert resp.json()["error"]["code"] == protocol.JSONRPC_INVALID_REQUEST

    assert not [k for k in redis.counters if k.startswith("rate:mcp:principal:")]
    # And a real call still goes through afterwards.
    ok = await ac.post("/mcp", json=_rpc("tools/list"), headers=headers)
    assert "result" in ok.json()


# ---------------------------------------------------------------------------
# Paid admin endpoints
# ---------------------------------------------------------------------------


async def test_nl_query_refuses_past_its_ceiling(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tight_limits,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every request past the flag check reaches Anthropic exactly once.
    Before this, an admin token could issue them without bound."""
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "1")
    get_settings.cache_clear()

    redis = _CountingRedis()

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    client._transport.app.dependency_overrides[get_redis] = _override_redis  # type: ignore[attr-defined]

    limit = get_settings().admin_nl_query_rate_limit
    with patch(
        "app.services.nl_query.parse_question",
        new=AsyncMock(return_value=(JobFilterSpec(), {}, "claude-opus-4-7")),
    ):
        for n in range(limit):
            resp = await client.post(
                "/api/v1/admin/query",
                json={"question": "failed CSV uploads"},
                headers=admin_headers,
            )
            assert resp.status_code == 200, f"call {n + 1} of {limit} should pass"

        resp = await client.post(
            "/api/v1/admin/query",
            json={"question": "failed CSV uploads"},
            headers=admin_headers,
        )

    assert resp.status_code == 429
    assert resp.json()["error_code"] == "rate_limit_exceeded"


async def test_nl_query_validation_error_does_not_consume_allowance(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tight_limits,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bucket counts *paid calls*. An empty question costs nothing,
    so a typo must not burn the budget for a real query."""
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "1")
    get_settings.cache_clear()

    redis = _CountingRedis()

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    client._transport.app.dependency_overrides[get_redis] = _override_redis  # type: ignore[attr-defined]

    for _ in range(get_settings().admin_nl_query_rate_limit + 3):
        resp = await client.post(
            "/api/v1/admin/query", json={"question": ""}, headers=admin_headers
        )
        assert 400 <= resp.status_code < 500
        assert resp.status_code != 429

    assert not redis.counters


async def test_digest_generate_refuses_past_its_ceiling(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tight_limits,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The more expensive of the two paid calls (~$0.018)."""
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "1")
    get_settings.cache_clear()

    redis = _CountingRedis()

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    client._transport.app.dependency_overrides[get_redis] = _override_redis  # type: ignore[attr-defined]

    limit = get_settings().admin_digest_rate_limit
    # `collect_window_stats` is the route's first call since WO-R2-127 split
    # it into read / call / write; returning None is an empty window, which
    # short-circuits before `generate_digest` — so no paid call is made and
    # the limiter is still what decides the 429. Patching the old composed
    # `run_digest_for_tenant` here would intercept nothing at all.
    read = AsyncMock(return_value=None)
    with patch("app.services.incident_digest.collect_window_stats", new=read):
        for n in range(limit):
            resp = await client.post(
                "/api/v1/admin/digests/generate", json={}, headers=admin_headers
            )
            assert resp.status_code == 201, f"call {n + 1} of {limit} should pass"

        resp = await client.post(
            "/api/v1/admin/digests/generate", json={}, headers=admin_headers
        )

    # The stub has to have been the thing that answered, or this test would
    # pass just as well with the patch pointing at a function the route no
    # longer calls: the real read also returns None on an empty window, so
    # the status codes alone cannot tell the two apart. The refused call is
    # rejected before the read, hence `limit` and not `limit + 1`.
    assert read.await_count == limit

    assert resp.status_code == 429
    assert resp.json()["error_code"] == "rate_limit_exceeded"


async def test_paid_endpoints_have_independent_buckets(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tight_limits,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the digest allowance must not block a
    natural-language query: different costs, different paths."""
    monkeypatch.setenv("LLM_DIGEST_ENABLED", "1")
    monkeypatch.setenv("LLM_NL_QUERY_ENABLED", "1")
    get_settings.cache_clear()

    redis = _CountingRedis()

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis

    client._transport.app.dependency_overrides[get_redis] = _override_redis  # type: ignore[attr-defined]

    # `collect_window_stats` is the route's first call since WO-R2-127 split
    # it into read / call / write; returning None is an empty window, which
    # short-circuits before `generate_digest` — so no paid call is made and
    # the limiter is still what decides the 429. Patching the old composed
    # `run_digest_for_tenant` here would intercept nothing at all.
    with patch(
        "app.services.incident_digest.collect_window_stats",
        new=AsyncMock(return_value=None),
    ):
        for _ in range(get_settings().admin_digest_rate_limit + 1):
            await client.post(
                "/api/v1/admin/digests/generate", json={}, headers=admin_headers
            )

    with patch(
        "app.services.nl_query.parse_question",
        new=AsyncMock(return_value=(JobFilterSpec(), {}, "claude-opus-4-7")),
    ):
        resp = await client.post(
            "/api/v1/admin/query",
            json={"question": "failed CSV uploads"},
            headers=admin_headers,
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Window semantics — the claim the docs used to make
# ---------------------------------------------------------------------------


async def test_fixed_window_admits_2x_across_a_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limiter is a **fixed** window, and this is what that costs.

    `int(time.time()) // window` buckets on absolute boundaries, so a
    caller who spends its whole allowance just before the boundary gets
    a fresh one immediately after: `2 * limit` requests inside a moment,
    while never breaking the rule as implemented.

    Three docstrings, `docs/REDIS.md` and `docs/ARCHITECTURE.md` all
    called this *sliding*, which promises a bound the code has never
    enforced (WO-R2-30). The behaviour is pinned here rather than
    changed: `2 * limit` is a real bound, and every ceiling in this
    change is sized against the doubled figure. A future switch to a
    sorted-set sliding window should flip this test to assert the
    refusal — that it has to be edited at all is the point.
    """
    redis = _CountingRedis()
    clock = {"now": 599.0}  # window 59 of a 10s window
    monkeypatch.setattr("app.utils.rate_limit.time.time", lambda: clock["now"])

    async def _call() -> None:
        await check_identity_rate_limit(
            redis, identity="agent-1", limit=5, window=10, bucket="probe"
        )

    # Fill the window ending at t=600.
    for _ in range(5):
        await _call()
    with pytest.raises(RateLimitError):
        await _call()

    # 0.2s later, across the boundary — a brand new bucket.
    clock["now"] = 600.2
    for _ in range(5):
        await _call()
    with pytest.raises(RateLimitError):
        await _call()

    # 10 admitted inside 0.2s against a documented limit of 5.
    assert sum(redis.counters.values()) == 12  # 10 admitted + 2 refused increments


async def test_identity_limiter_fails_open_on_redis_error() -> None:
    """Mirrors `rate_limiter`, the backpressure check and the per-tenant
    quota check (ADR 0005)."""
    redis = _CountingRedis()
    redis.fail = True
    for _ in range(50):
        await check_identity_rate_limit(
            redis, identity="agent-1", limit=1, window=60, bucket="probe"
        )


async def test_identity_limiter_separates_identities_and_buckets() -> None:
    """Two axes, both load-bearing: a principal's MCP allowance is
    independent of its digest allowance, and of another principal's."""
    redis = _CountingRedis()
    for _ in range(2):
        await check_identity_rate_limit(
            redis, identity="a", limit=2, window=60, bucket="mcp"
        )
    with pytest.raises(RateLimitError):
        await check_identity_rate_limit(
            redis, identity="a", limit=2, window=60, bucket="mcp"
        )
    # Same identity, different bucket — unaffected.
    await check_identity_rate_limit(
        redis, identity="a", limit=2, window=60, bucket="digest"
    )
    # Different identity, same bucket — unaffected.
    await check_identity_rate_limit(
        redis, identity="b", limit=2, window=60, bucket="mcp"
    )
