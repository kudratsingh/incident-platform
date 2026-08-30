"""The audit row for an MCP tool call is not optional.

Two findings, one property. `agent.tool_invoked` is the only record that
an action ran — `evals/guards.py` grades the agent on it, and the Audit
tab is where an operator reconstructs what a machine principal did. Both
of these let it go missing while the call returned 200:

  - R2-51: `X-Request-ID` is copied onto the row and the column is
    `String(255)`. A caller-supplied header longer than that makes the
    *insert* fail. The insert is savepoint-wrapped and silent, so the
    tool ran, the transaction committed, and nothing was recorded.
  - R2-59: `get_deploy_history` and `get_postgres_health` swallowed a DB
    error without rolling back, leaving the transaction aborted — so on
    exactly the failure their fallback exists for, the envelope's audit
    write afterwards was dropped.

The R2-59 tests run against `AbortingSession`, which gives SQLite
Postgres' aborted-transaction rule; without it neither finding is
reproducible off a real Postgres.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from app.core.middleware import CORRELATION_ID_MAX_LENGTH
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.models.audit import AuditLog
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.operator_audit import TOOL_INVOKED_ACTION
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import AbortingSession


class _RedisStub:
    def __init__(self, values: dict[str, bytes | str] | None = None) -> None:
        self._store: dict[str, bytes | str] = dict(values or {})

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)


async def _mint_token(
    db_session: AsyncSession,
    default_tenant: Any,
    scopes: list[str],
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=default_tenant.id,
        name=f"audit-probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


def _rpc(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def _client_for(session: Any) -> AsyncClient:
    """An MCP client whose `get_db` yields `session` — a real one, or a
    proxy that behaves like Postgres under failure."""
    app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield _RedisStub()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def token(db_session: AsyncSession, default_tenant) -> str:  # type: ignore[no-untyped-def]
    return await _mint_token(
        db_session, default_tenant, [Scope.TELEMETRY_READ.value]
    )


async def _tool_rows(db_session: AsyncSession, tool_name: str) -> list[AuditLog]:
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == TOOL_INVOKED_ACTION,
                    AuditLog.resource_id == tool_name,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# R2-51 — a caller-supplied header cannot suppress the row
# ---------------------------------------------------------------------------


async def test_oversized_request_id_still_writes_the_audit_row(
    db_session: AsyncSession, token: str
) -> None:
    """The finding's sharp end: a 4KB header used to leave the tool run
    and committed with no audit row. The row must exist, and carry the
    sanitised id rather than the caller's."""
    async with _client_for(db_session) as ac:
        resp = await ac.post(
            "/mcp",
            json=_rpc("get_consumer_lag"),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "x" * 4096,
            },
        )

    assert resp.status_code == 200
    assert "result" in resp.json()

    rows = await _tool_rows(db_session, "get_consumer_lag")
    assert len(rows) == 1, "the tool ran; the audit row has to be there"
    recorded = rows[0].request_id
    assert recorded is not None
    assert len(recorded) <= CORRELATION_ID_MAX_LENGTH
    assert not recorded.startswith("xxxx"), (
        "the caller's value reached the audit row — it must be replaced, "
        "not carried or truncated"
    )
    # And it is the id the caller was told about.
    assert recorded == resp.headers["X-Request-ID"]


async def test_control_characters_in_the_request_id_are_replaced(
    db_session: AsyncSession, token: str
) -> None:
    async with _client_for(db_session) as ac:
        resp = await ac.post(
            "/mcp",
            json=_rpc("get_consumer_lag"),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "tenant-a\r\nfake: injected",
            },
        )

    assert resp.status_code == 200
    rows = await _tool_rows(db_session, "get_consumer_lag")
    assert len(rows) == 1
    assert rows[0].request_id is not None
    assert "\n" not in rows[0].request_id
    assert "injected" not in rows[0].request_id


async def test_a_usable_request_id_is_recorded_verbatim(
    db_session: AsyncSession, token: str
) -> None:
    """The correlation the header exists for still works — this fix must
    not cost the agent its ability to tie a call to its own trace."""
    supplied = f"commander-{uuid.uuid4().hex}"
    async with _client_for(db_session) as ac:
        await ac.post(
            "/mcp",
            json=_rpc("get_consumer_lag"),
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": supplied,
            },
        )

    rows = await _tool_rows(db_session, "get_consumer_lag")
    assert [r.request_id for r in rows] == [supplied]


async def test_an_unwritable_audit_row_fails_the_call(
    db_session: AsyncSession, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The general rule behind R2-51: if the row cannot be written, the
    request fails. A 200 for an action nobody recorded is the outcome
    being removed."""
    from app.repositories import audit as audit_repo_module

    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(audit_repo_module.AuditRepository, "log", _explode)

    async with _client_for(db_session) as ac:
        resp = await ac.post(
            "/mcp",
            json=_rpc("get_consumer_lag"),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 500
    body = resp.json()
    assert "result" not in body or body.get("result") is None
    assert body["error"]["code"] == protocol.JSONRPC_INTERNAL_ERROR


# ---------------------------------------------------------------------------
# R2-59 — a degraded query cannot take the audit row down with it
# ---------------------------------------------------------------------------


async def test_deploy_history_fallback_still_writes_its_audit_row(
    db_session: AsyncSession, token: str
) -> None:
    """`deploy_markers` absent — the exact condition the env fallback
    exists for. The tool must degrade *and* stay audited."""
    session = AbortingSession(db_session, fail_on="deploy_markers")

    async with _client_for(session) as ac:
        resp = await ac.post(
            "/mcp",
            json=_rpc("get_deploy_history", {"limit": 5}),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["source"] != "deploy_markers", "expected the fallback"

    rows = await _tool_rows(db_session, "get_deploy_history")
    assert len(rows) == 1, (
        "the fallback ran and the call returned success, so the audit row "
        "must exist — this is the row R2-59 dropped"
    )
    assert rows[0].extra_data["outcome"] == "success"


async def test_postgres_health_failure_still_writes_its_audit_row(
    db_session: AsyncSession, token: str
) -> None:
    session = AbortingSession(
        db_session, fail_on="SELECT 1", error="server closed the connection"
    )

    async with _client_for(session) as ac:
        resp = await ac.post(
            "/mcp",
            json=_rpc("get_postgres_health"),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["ok"] is False, "expected the unhealthy report"

    rows = await _tool_rows(db_session, "get_postgres_health")
    assert len(rows) == 1, (
        "an ok=false health answer is a report, not a reason to lose the "
        "record of the call"
    )
