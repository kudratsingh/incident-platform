"""Transaction-envelope contract for `tools/call` (WO-R2-06)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.registry import (
    ToolContext,
    _restore_for_tests,
    _snapshot_for_tests,
    tool,
)
from app.mcp.standalone import create_mcp_app
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.idempotency import IdempotencyRecord
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PROBE_SIDE_EFFECT_ACTION = "probe.tool_side_effect"


class _RedisStub:
    async def get(self, key: str) -> bytes | str | None:
        return None

    async def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        return True

    async def delete(self, *keys: str) -> int:
        return 0


# ---------------------------------------------------------------------------


class _CrashIn(BaseModel):
    pass


class _CrashOut(BaseModel):
    ok: bool


class _ActionIn(BaseModel):
    idempotency_key: str


class _ActionOut(BaseModel):
    ok: bool
    marker: str


def _register_probe_tools() -> None:
    @tool(
        "envelope_probe_crash",
        description="Writes a row, then raises. Test-only.",
        input_model=_CrashIn,
        output_model=_CrashOut,
        required_scope=Scope.ACTIONS_EXECUTE,
    )
    async def _crash(inp: _CrashIn, ctx: ToolContext) -> _CrashOut:
        # A write the tool stages before it dies. It must not survive.
        await AuditRepository(ctx.db).log(
            PROBE_SIDE_EFFECT_ACTION,
            tenant_id=ctx.principal.tenant_id,
            resource_type="mcp_tool",
        )
        raise RuntimeError("simulated tool crash")

    @tool(
        "envelope_probe_action",
        description="Tier-1 shaped idempotent action. Test-only.",
        input_model=_ActionIn,
        output_model=_ActionOut,
        required_scope=Scope.ACTIONS_EXECUTE,
        is_idempotent=True,
    )
    async def _action(inp: _ActionIn, ctx: ToolContext) -> _ActionOut:
        return _ActionOut(ok=True, marker="fresh-execution")


# ---------------------------------------------------------------------------


class _Env:
    def __init__(
        self,
        client: AsyncClient,
        token: str,
        factory: Any,
        principal_id: uuid.UUID,
    ) -> None:
        self.client = client
        self.token = token
        self.factory = factory
        self.principal_id = principal_id
        self.tenant_id = DEFAULT_TENANT_ID

    async def call(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        return await self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def audit_rows(self, action: str) -> list[AuditLog]:
        """Read committed rows through a brand-new session, so nothing the request left
        pending in its own session can fake a pass."""
        async with self.factory() as session:
            result = await session.execute(
                select(AuditLog).where(AuditLog.action == action)
            )
            return list(result.scalars().all())


@pytest_asyncio.fixture
async def env():  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    snapshot = _snapshot_for_tests()
    _register_probe_tools()

    # Seed + commit: the request sessions below are separate transactions
    # and can only see committed data.
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    slug="default",
                    name="Default Tenant",
                    is_active=True,
                )
            )
            await session.flush()
            svc = ServiceAccountService(
                ServiceAccountRepository(session),
                ServiceAccountTokenRepository(session),
                AuditRepository(session),
            )
            sa = await svc.create_service_account(
                tenant_id=DEFAULT_TENANT_ID,
                name=f"probe-{uuid.uuid4().hex[:8]}",
                scopes=[Scope.ACTIONS_EXECUTE.value],
                created_by_user_id=None,
            )
            _, token = await svc.mint_token(
                service_account=sa,
                scopes=None,
                ttl=None,
                minted_by_user_id=None,
            )
            principal_id = sa.id

    app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        # Byte-for-byte the shape of app.dependencies.get_db.
        async with factory() as session:
            async with session.begin():
                yield session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield _RedisStub()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        yield _Env(client, token, factory, principal_id)

    _restore_for_tests(snapshot)
    await engine.dispose()


def _envelope(resp: Any) -> dict[str, Any]:
    """Assert the response is a JSON-RPC envelope and return it."""
    body = resp.text
    try:
        parsed = json.loads(body)
    except ValueError:  # pragma: no cover - only on the red path
        raise AssertionError(
            f"not a JSON-RPC envelope: HTTP {resp.status_code} "
            f"content-type={resp.headers.get('content-type')!r} body={body!r}"
        ) from None
    assert parsed.get("jsonrpc") == "2.0", parsed
    assert "result" in parsed or "error" in parsed, parsed
    return parsed


# ---------------------------------------------------------------------------


async def test_crashing_tool_returns_envelope_and_persists_error_audit(
    env: _Env,
) -> None:
    resp = await env.call("envelope_probe_crash", {})
    body = _envelope(resp)

    assert body["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
    assert body["id"] == "1"

    # The row `evals/guards.py` grades on. Before the fix the rollback at
    # write could never be issued and the crashed call read as clean.
    rows = await env.audit_rows("agent.tool_invoked")
    assert len(rows) == 1, "crashed tool left no audit row"
    assert rows[0].extra_data is not None
    assert rows[0].extra_data["outcome"] == "error"
    assert rows[0].extra_data["tool_name"] == "envelope_probe_crash"


async def test_crashed_tool_writes_roll_back_but_audit_row_survives(
    env: _Env,
) -> None:
    """The savepoint boundary: the tool's own staged write dies with it, the audit row for
    the attempt does not."""
    await env.call("envelope_probe_crash", {})

    assert await env.audit_rows(PROBE_SIDE_EFFECT_ACTION) == [], (
        "the crashed tool's own write was committed"
    )
    assert len(await env.audit_rows("agent.tool_invoked")) == 1


# ---------------------------------------------------------------------------


async def _insert_record(
    env: _Env,
    *,
    key: str,
    arguments: dict[str, Any],
    response: dict[str, Any],
    expires_at: datetime | None,
) -> None:
    from app.services.idempotency import _hash_arguments

    async with env.factory() as session:
        async with session.begin():
            session.add(
                IdempotencyRecord(
                    id=uuid.uuid4(),
                    tenant_id=env.tenant_id,
                    principal_id=env.principal_id,
                    tool_name="envelope_probe_action",
                    idempotency_key=key,
                    arguments_hash=_hash_arguments(arguments),
                    response_json=response,
                    expires_at=expires_at,
                )
            )


async def test_expired_key_re_executes_and_returns_an_envelope(
    env: _Env,
) -> None:
    """An expired record must not stop the tool from running, and must not blow up the call
    either."""
    args = {"idempotency_key": "expired-k-1"}
    await _insert_record(
        env,
        key="expired-k-1",
        arguments=args,
        response={"ok": True, "marker": "stale"},
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    resp = await env.call("envelope_probe_action", args)
    body = _envelope(resp)

    assert "error" not in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["marker"] == "fresh-execution"

    rows = await env.audit_rows("agent.tool_invoked")
    assert len(rows) == 1, "the executed action left no audit row"
    assert rows[0].extra_data is not None
    assert rows[0].extra_data["outcome"] == "success"


async def test_duplicate_call_returns_the_recorded_outcome(
    env: _Env,
) -> None:
    """One answer per key: a duplicate call returns the recorded response rather than its
    own freshly computed one."""
    args = {"idempotency_key": "inflight-k-1"}
    await _insert_record(
        env,
        key="inflight-k-1",
        arguments=args,
        response={"ok": True, "marker": "winner-response"},
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )

    resp = await env.call("envelope_probe_action", args)
    body = _envelope(resp)

    assert "error" not in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["marker"] == "winner-response", (
        "duplicate call returned its own result instead of the recorded one"
    )

    rows = await env.audit_rows("agent.tool_invoked")
    assert len(rows) == 1
    assert rows[0].extra_data is not None
    assert rows[0].extra_data["outcome"] == "success"


# ---------------------------------------------------------------------------


async def test_unhandled_dispatch_error_still_returns_envelope(
    env: _Env, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    from app.mcp import handlers

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("escaped the dispatch layer entirely")

    monkeypatch.setattr(handlers, "dispatch", _boom)

    resp = await env.call("envelope_probe_action", {"idempotency_key": "k"})
    body = _envelope(resp)
    assert body["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR
    # JSON-RPC wants an unknowable id said out loud, not left out.
    assert "id" in body and body["id"] is None
