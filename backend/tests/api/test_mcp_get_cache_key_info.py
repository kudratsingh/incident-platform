"""End-to-end tests for `get_cache_key_info` — the cache-key read tool.

Same JSON-RPC pattern as `test_mcp_wave2_read_tools.py`: build the
standalone MCP app, mint a scoped SA token, POST, assert shape.

The load-bearing test is the observe → remediate → confirm round trip
(`test_stale_cache_write_is_observable_and_invalidatable`): the
`create_stale_cache` chaos hook writes the hot_set key, this tool sees
it (existence + TTL + type + size), `invalidate_cache_key` deletes it,
and this tool confirms it gone. Before this tool existed, no read
surface could observe that key at all — the fault the hook injects was
invisible to the caller expected to find and fix it.
"""

from __future__ import annotations

import importlib
import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.registry import _restore_for_tests, _snapshot_for_tests
from app.mcp.standalone import create_mcp_app
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    """Enough of the async Redis surface for `get_cache_key_info` and
    its round-trip partners (`create_stale_cache` → SET,
    `invalidate_cache_key` → DEL): `get`, `set`, `delete`, `type`,
    `ttl`, `strlen`, and the collection size commands.

    Values are held as native Python objects; `type()` derives the
    Redis type name from the Python type, mirroring a
    `decode_responses=True` client (str returns, `"none"` for a
    missing key)."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: bytes | str, ex: int | None = None) -> bool:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._ttls.pop(k, None)
                removed += 1
        return removed

    async def type(self, key: str) -> str:
        value = self._store.get(key)
        if value is None:
            return "none"
        if isinstance(value, bytes | str):
            return "string"
        if isinstance(value, list):
            return "list"
        if isinstance(value, set):
            return "set"
        if isinstance(value, dict):
            return "hash"
        raise AssertionError(f"unmapped stub value type: {type(value)!r}")

    async def ttl(self, key: str) -> int:
        # redis-py convention: -2 missing, -1 present with no expiry.
        if key not in self._store:
            return -2
        return self._ttls.get(key, -1)

    async def strlen(self, key: str) -> int:
        value = self._store.get(key)
        if value is None:
            return 0
        return len(value if isinstance(value, bytes) else str(value).encode())

    async def llen(self, key: str) -> int:
        return len(self._store.get(key) or [])

    async def scard(self, key: str) -> int:
        return len(self._store.get(key) or set())

    async def hlen(self, key: str) -> int:
        return len(self._store.get(key) or {})


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    app = create_mcp_app()
    redis_stub = _RedisStub()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        yield ac, redis_stub


def _mcp_app_with_chaos_enabled(db_session: AsyncSession, redis_stub: _RedisStub):  # type: ignore[no-untyped-def]
    """CHAOS_ENABLED-true reload trick from `test_mcp_wave2_chaos_hooks`,
    trimmed to the three tools the round-trip test invokes."""
    with patch(
        "app.mcp.standalone.assert_chaos_gate", lambda *a, **kw: None
    ), patch(
        "app.mcp.chaos.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ):
        from app.mcp.tools import actions as actions_pkg
        from app.mcp.tools import cache_key_info as cache_key_info_mod
        from app.mcp.tools import chaos as chaos_pkg

        snap = _snapshot_for_tests()
        _restore_for_tests({})
        importlib.reload(chaos_pkg.create_stale_cache)  # type: ignore[attr-defined]
        importlib.reload(actions_pkg.invalidate_cache_key)  # type: ignore[attr-defined]
        importlib.reload(cache_key_info_mod)

        from app.mcp.standalone import create_mcp_app as _create

        app = _create()

    async def _override_db():
        yield db_session

    async def _override_redis():
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    def _teardown() -> None:
        _restore_for_tests(snap)

    return app, _teardown


async def _token(
    db_session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=f"probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa,
        scopes=None,
        ttl=None,
        minted_by_user_id=None,
    )
    return plaintext


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}


async def _call(
    ac: AsyncClient, token: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": tool_name, "arguments": arguments}),
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(body["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# tools/list registration
# ---------------------------------------------------------------------------


async def test_get_cache_key_info_is_listed(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Registered unconditionally — a read tool, not chaos-gated."""
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/list"),
        headers={"Authorization": f"Bearer {token}"},
    )
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
    assert "get_cache_key_info" in tools
    # The namespace constraint is part of the advertised contract.
    assert "cache:" in json.dumps(tools["get_cache_key_info"]["inputSchema"])


# ---------------------------------------------------------------------------
# happy paths
# ---------------------------------------------------------------------------


async def test_reports_existing_string_key_with_ttl_type_and_size(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, redis_stub = mcp_client
    payload = json.dumps(["a", "b", "c"])
    await redis_stub.set("cache:jobs:worker-dispatcher:hot_set", payload, ex=600)

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(
            ac,
            token,
            "get_cache_key_info",
            {"key": "cache:jobs:worker-dispatcher:hot_set"},
        )
    )
    assert info["key"] == "cache:jobs:worker-dispatcher:hot_set"
    assert info["exists"] is True
    assert info["type"] == "string"
    assert info["ttl_seconds"] == 600
    assert info["size"] == len(payload.encode())


async def test_reports_missing_key_as_absent(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    # The tenant in the key is the caller's own. It used to be a hardcoded
    # nil UUID, which since R2-54 is a *cross-tenant* probe and refused —
    # a small illustration of how easy that key was to build by accident.
    info = _content(
        await _call(
            ac,
            token,
            "get_cache_key_info",
            {"key": f"cache:job:{default_tenant.id}:none"},
        )
    )
    assert info["exists"] is False
    assert info["type"] is None
    assert info["ttl_seconds"] is None
    assert info["size"] is None


async def test_collection_key_reports_element_count_and_no_expiry(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Non-string types report element count as `size`; a key without
    an expiry reports `ttl_seconds: null` while `exists` stays true."""
    ac, redis_stub = mcp_client
    redis_stub._store["read_model:sample"] = {"a", "b", "c", "d"}

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(ac, token, "get_cache_key_info", {"key": "read_model:sample"})
    )
    assert info["exists"] is True
    assert info["type"] == "set"
    assert info["ttl_seconds"] is None
    assert info["size"] == 4


# ---------------------------------------------------------------------------
# constraint: platform-owned cache namespaces only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "jobs:tenant:acme:status:running",  # CQRS read-model projection
        "job:progress:last:some-job",  # SSE snapshot (tenant data)
        "rate:1.2.3.4:60",  # rate-limit counter
        "priority_queue",  # queue ZSET
        "dag:paused:some-root",  # pause flag
    ],
)
async def test_refuses_keys_outside_cache_namespaces(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    forbidden_key: str,
) -> None:
    """Not an arbitrary-Redis-read primitive: everything outside the
    allowlisted cache namespaces is refused before any Redis call."""
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    body = await _call(ac, token, "get_cache_key_info", {"key": forbidden_key})
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "cache_key_forbidden"


async def test_wrong_scope_is_forbidden(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    body = await _call(
        ac, token, "get_cache_key_info", {"key": "cache:anything"}
    )
    assert body["error"]["code"] == protocol.MCP_FORBIDDEN


# ---------------------------------------------------------------------------
# acceptance: the chaos hook's write is observable through this tool
# ---------------------------------------------------------------------------


async def test_stale_cache_write_is_observable_and_invalidatable(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """The full observe → remediate → confirm loop across three scopes:

      1. `create_stale_cache` (chaos:invoke) writes the hot_set key.
      2. `get_cache_key_info` (telemetry:read) sees it — existence,
         string type, the hook's TTL, and the hook's exact byte size.
      3. `invalidate_cache_key` (actions:execute) deletes it.
      4. `get_cache_key_info` confirms it is gone.

    Step 2 is what this tool exists for: without it the hook's write
    was invisible to every read surface."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos_token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            read_token = await _token(
                db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
            )
            actions_token = await _token(
                db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
            )

            # 1. Chaos hook writes the key.
            created = _content(
                await _call(ac, chaos_token, "create_stale_cache", {})
            )
            assert created["accepted"] is True
            key = created["key"]

            # 2. The read tool observes exactly what the hook wrote.
            seen = _content(
                await _call(ac, read_token, "get_cache_key_info", {"key": key})
            )
            assert seen["exists"] is True
            assert seen["type"] == "string"
            assert seen["ttl_seconds"] == created["ttl_seconds"]
            assert seen["size"] == created["size_bytes"]

            # 3. Remediation deletes it.
            invalidated = _content(
                await _call(
                    ac,
                    actions_token,
                    "invalidate_cache_key",
                    {"key": key, "idempotency_key": "cache-info-round-trip-1"},
                )
            )
            assert invalidated["deleted"] is True

            # 4. The read tool confirms the remediation.
            gone = _content(
                await _call(ac, read_token, "get_cache_key_info", {"key": key})
            )
            assert gone["exists"] is False
            assert gone["ttl_seconds"] is None
            assert gone["size"] is None
    finally:
        teardown()


# ---------------------------------------------------------------------------
# the record check (WO-R3-267)
# ---------------------------------------------------------------------------
#
# The gap: `exists / type / ttl_seconds / size` read the same for a current
# hot-set entry and one that no longer matches the records it names — both
# are strings with a TTL, differing only in byte count. Two paid live runs
# of `remediate_stale_cache_success` split on that reading, one acting and
# one escalating, so the tests below are written as the discrimination the
# caller actually needs: same key, same tool, two worlds, two readings.


async def _seed_jobs(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    count: int,
) -> list[uuid.UUID]:
    """`count` ordinary job rows, returned by id."""
    jobs = [
        Job(
            tenant_id=tenant_id,
            user_id=user_id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.DEAD_LETTER.value,
            payload={},
            retry_count=3,
        )
        for _ in range(count)
    ]
    for job in jobs:
        db_session.add(job)
    # Ids are assigned on flush, not on construction — read them after.
    await db_session.flush()
    return [job.id for job in jobs]


async def test_entry_naming_records_the_platform_holds_finds_all_of_them(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The healthy reading: a hot-set entry listing three job ids the
    database has reports three referenced and three found."""
    ac, redis_stub = mcp_client
    job_ids = await _seed_jobs(db_session, default_tenant.id, test_user.id, 3)
    await redis_stub.set(
        "cache:jobs:worker-dispatcher:hot_set",
        json.dumps([str(i) for i in job_ids]),
        ex=24 * 3600,
    )

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(
            ac,
            token,
            "get_cache_key_info",
            {"key": "cache:jobs:worker-dispatcher:hot_set"},
        )
    )
    assert info["records_referenced"] == 3
    assert info["records_found"] == 3


async def test_entry_naming_records_the_platform_lacks_finds_none_of_them(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The stale reading, on the same key: the entry still names three
    records and the database holds none of them.

    Byte-for-byte this is the reading that used to be indistinguishable
    from the healthy one above — same key, same type, same kind of TTL,
    a handful of bytes apart."""
    ac, redis_stub = mcp_client
    await _seed_jobs(db_session, default_tenant.id, test_user.id, 3)
    await redis_stub.set(
        "cache:jobs:worker-dispatcher:hot_set",
        json.dumps([f"{uuid.uuid4()}" for _ in range(3)]),
        ex=600,
    )

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(
            ac,
            token,
            "get_cache_key_info",
            {"key": "cache:jobs:worker-dispatcher:hot_set"},
        )
    )
    assert info["records_referenced"] == 3
    assert info["records_found"] == 0


async def test_per_job_key_names_the_one_record_its_key_identifies(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """`cache:job:{tenant}:{job_id}` is answered from the key alone — the
    payload is tenant data and is never read."""
    ac, redis_stub = mcp_client
    (job_id,) = await _seed_jobs(db_session, default_tenant.id, test_user.id, 1)
    key = f"cache:job:{default_tenant.id}:{job_id}"
    await redis_stub.set(key, json.dumps({"id": str(job_id)}), ex=10)

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(await _call(ac, token, "get_cache_key_info", {"key": key}))
    assert info["records_referenced"] == 1
    assert info["records_found"] == 1


async def test_per_job_key_for_a_record_the_platform_no_longer_has(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """A cached copy of a job the database no longer holds: one
    reference, none found."""
    ac, redis_stub = mcp_client
    key = f"cache:job:{default_tenant.id}:{uuid.uuid4()}"
    await redis_stub.set(key, "{}", ex=10)

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(await _call(ac, token, "get_cache_key_info", {"key": key}))
    assert info["records_referenced"] == 1
    assert info["records_found"] == 0


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        (
            "kafka:consumer_lag:worker-dispatcher",
            "29",
            "a counter names no records",
        ),
        (
            "cache:jobs:worker-dispatcher:hot_set",
            "not json at all",
            "an unreadable value",
        ),
        (
            "cache:jobs:worker-dispatcher:hot_set",
            '{"members": 3}',
            "a JSON object is not a list of references",
        ),
        (
            "cache:jobs:worker-dispatcher:hot_set",
            "[1, 2, 3]",
            "non-string items are not record names",
        ),
    ],
)
async def test_unknowable_references_are_null_not_zero(
    mcp_client,  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    key: str,
    value: str,
    why: str,
) -> None:
    """Unknown is not a value. Where the platform cannot work out what an
    entry refers to it says so with a null, never with a 0 — a 0 would
    read as "names nothing", which is a finding rather than an absence
    of one."""
    ac, redis_stub = mcp_client
    await redis_stub.set(key, value, ex=600)

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(await _call(ac, token, "get_cache_key_info", {"key": key}))
    assert info["records_referenced"] is None, why
    assert info["records_found"] is None, why


async def test_collection_key_reports_null_references(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """A Redis collection type is sized, not dereferenced: the tool does
    not read members, so it has nothing to resolve."""
    ac, redis_stub = mcp_client
    redis_stub._store["read_model:sample"] = {"a", "b", "c", "d"}

    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(ac, token, "get_cache_key_info", {"key": "read_model:sample"})
    )
    assert info["size"] == 4
    assert info["records_referenced"] is None
    assert info["records_found"] is None


async def test_missing_key_reports_null_references(
    mcp_client, db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """Nothing there is nothing to check — and, since remediation ends
    with this call, the confirm-the-delete reading must not carry
    counts left over from the entry that was deleted."""
    ac, _ = mcp_client
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(
            ac,
            token,
            "get_cache_key_info",
            {"key": "cache:jobs:worker-dispatcher:hot_set"},
        )
    )
    assert info["exists"] is False
    assert info["records_referenced"] is None
    assert info["records_found"] is None


async def test_an_entry_naming_another_tenants_records_finds_none(
    mcp_client, db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The lookup is the caller's own tenant, like every other read on
    this surface. A platform-global key listing a sibling tenant's job
    ids resolves to nothing rather than confirming those rows exist —
    the safe direction, and the same answer a deleted row gives."""
    ac, redis_stub = mcp_client
    other = Tenant(name="other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    other_user = User(
        tenant_id=other.id,
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(other_user)
    await db_session.flush()
    foreign_ids = await _seed_jobs(db_session, other.id, other_user.id, 2)

    await redis_stub.set(
        "cache:jobs:worker-dispatcher:hot_set",
        json.dumps([str(i) for i in foreign_ids]),
        ex=600,
    )
    token = await _token(
        db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
    )
    info = _content(
        await _call(
            ac,
            token,
            "get_cache_key_info",
            {"key": "cache:jobs:worker-dispatcher:hot_set"},
        )
    )
    assert info["records_referenced"] == 2
    assert info["records_found"] == 0


async def test_the_hook_written_entry_reads_as_naming_records_nothing_holds(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """End to end, through the hook that writes the condition.

    Nothing marks the entry as stale and nothing new is written beside
    it: the hook puts the world in a state — a cached list of names the
    database has no records for — and the read tool measures that state
    at call time. The healthy entry on the same key, listing job ids the
    database does have, reads the other way."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos_token = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            read_token = await _token(
                db_session, default_tenant.id, [Scope.TELEMETRY_READ.value]
            )
            key = "cache:jobs:worker-dispatcher:hot_set"

            job_ids = await _seed_jobs(
                db_session, default_tenant.id, test_user.id, 3
            )
            await redis_stub.set(
                key, json.dumps([str(i) for i in job_ids]), ex=24 * 3600
            )
            healthy = _content(
                await _call(ac, read_token, "get_cache_key_info", {"key": key})
            )

            created = _content(
                await _call(ac, chaos_token, "create_stale_cache", {})
            )
            assert created["key"] == key
            stale = _content(
                await _call(ac, read_token, "get_cache_key_info", {"key": key})
            )

            assert (healthy["records_referenced"], healthy["records_found"]) == (
                3,
                3,
            )
            assert (stale["records_referenced"], stale["records_found"]) == (3, 0)
            # The four readings that used to be the whole answer still do
            # not separate the two worlds on their own.
            assert healthy["exists"] == stale["exists"] is True
            assert healthy["type"] == stale["type"] == "string"
    finally:
        teardown()
