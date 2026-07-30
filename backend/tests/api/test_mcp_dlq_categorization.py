"""End-to-end tests for the DLQ categorization surface added in v0.4.0.

Covers:
  - list_dlq_messages exposes remediation_hint and filters by it
  - replay_dlq_by_ids replays targeted set + surfaces per-id errors
  - replay_dlq_by_category walks the safe categories
  - replay_dlq_by_category REFUSES human_required
  - mark_dlq_permanent sets the hint + writes an audit row
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.standalone import create_mcp_app
from app.models.audit import AuditLog
from app.models.enums import JobStatus, JobType, RemediationHint
from app.models.job import Job
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None
    ) -> bool:
        self._store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed


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
        yield ac


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


async def _seed_categorized_dlq(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
) -> dict[str, uuid.UUID]:
    """One job per category. Returns a hint→job_id map."""
    ids: dict[str, uuid.UUID] = {}
    for hint in (
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.HUMAN_REQUIRED.value,
    ):
        job = Job(
            tenant_id=default_tenant.id,
            user_id=test_user.id,
            type=JobType.CSV_UPLOAD.value,
            status=JobStatus.DEAD_LETTER.value,
            error_message=f"synthetic {hint}",
            retry_count=3,
            remediation_hint=hint,
        )
        db_session.add(job)
        await db_session.flush()
        ids[hint] = job.id
    return ids


# ---------------------------------------------------------------------------
# list_dlq_messages — categorization surface
# ---------------------------------------------------------------------------


async def test_list_dlq_exposes_remediation_hint(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(mcp_client, token, "list_dlq_messages", {})
    )
    hints = {item["remediation_hint"] for item in payload["items"]}
    assert hints == {
        RemediationHint.REPLAY_SAFE.value,
        RemediationHint.WAIT_AND_REPLAY.value,
        RemediationHint.HUMAN_REQUIRED.value,
    }


async def test_list_dlq_filters_by_category(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "list_dlq_messages",
            {"remediation_hint": RemediationHint.REPLAY_SAFE.value},
        )
    )
    assert payload["total"] == 1
    assert all(
        item["remediation_hint"] == RemediationHint.REPLAY_SAFE.value
        for item in payload["items"]
    )


# ---------------------------------------------------------------------------
# replay_dlq_by_ids
# ---------------------------------------------------------------------------


async def test_replay_dlq_by_ids_replays_targeted_set(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [
                    str(ids[RemediationHint.REPLAY_SAFE.value]),
                    str(ids[RemediationHint.WAIT_AND_REPLAY.value]),
                ],
                "idempotency_key": "replay-ids-0001",
            },
        )
    )
    assert payload["requested"] == 2
    assert payload["replayed"] == 2
    assert payload["failed"] == 0


async def test_replay_dlq_by_ids_reports_per_id_failures(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A mix of real + missing IDs — the good ones replay, the
    missing one shows up as `ok: false` with the not-found message."""
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    bogus = str(uuid.uuid4())
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_ids",
            {
                "job_ids": [
                    str(ids[RemediationHint.REPLAY_SAFE.value]),
                    bogus,
                ],
                "idempotency_key": "replay-mixed-0001",
            },
        )
    )
    assert payload["replayed"] == 1
    assert payload["failed"] == 1
    bad = next(r for r in payload["results"] if r["id"] == bogus)
    assert bad["ok"] is False
    assert bad["error"]


# ---------------------------------------------------------------------------
# replay_dlq_by_category
# ---------------------------------------------------------------------------


async def test_replay_dlq_by_category_replays_safe(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_by_category",
            {
                "category": RemediationHint.REPLAY_SAFE.value,
                "idempotency_key": "cat-safe-0001",
            },
        )
    )
    assert payload["category"] == RemediationHint.REPLAY_SAFE.value
    assert payload["matched"] == 1
    assert payload["replayed"] == 1


async def test_replay_dlq_by_category_refuses_human_required(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The whole point of the category: auto-replay is wrong for
    human_required. Refusal is enforced BEFORE any job is touched."""
    await _seed_categorized_dlq(db_session, default_tenant, test_user)
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        mcp_client,
        token,
        "replay_dlq_by_category",
        {
            "category": RemediationHint.HUMAN_REQUIRED.value,
            "idempotency_key": "cat-refused-0001",
        },
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "dlq_category_refused"


# ---------------------------------------------------------------------------
# mark_dlq_permanent
# ---------------------------------------------------------------------------


async def test_mark_dlq_permanent_sets_hint(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    replay_safe_id = ids[RemediationHint.REPLAY_SAFE.value]

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "mark_dlq_permanent",
            {
                "job_id": str(replay_safe_id),
                "reason": "Analysis shows this hits the same bug every time.",
                "idempotency_key": "mark-0001",
            },
        )
    )
    assert payload["remediation_hint"] == RemediationHint.HUMAN_REQUIRED.value
    assert payload["already_marked"] is False
    assert payload["previous_hint"] == RemediationHint.REPLAY_SAFE.value

    # And an audit row was written
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.marked_permanent")
        )
    ).scalars().all()
    assert list(rows), "expected a job.marked_permanent audit row"
    row = rows[-1]
    assert row.extra_data is not None
    assert "same bug" in row.extra_data["reason"]


async def test_mark_dlq_permanent_idempotent(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    ids = await _seed_categorized_dlq(db_session, default_tenant, test_user)
    already_marked_id = ids[RemediationHint.HUMAN_REQUIRED.value]

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    payload = _content(
        await _call(
            mcp_client,
            token,
            "mark_dlq_permanent",
            {
                "job_id": str(already_marked_id),
                "reason": "no-op, already marked",
                "idempotency_key": "mark-idem-0001",
            },
        )
    )
    assert payload["already_marked"] is True


async def test_mark_dlq_permanent_refuses_non_dlq(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A non-DLQ job can't be marked — refuses with not-found shape."""
    job = Job(
        tenant_id=default_tenant.id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD.value,
        status=JobStatus.COMPLETED.value,
    )
    db_session.add(job)
    await db_session.flush()

    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )
    body = await _call(
        mcp_client,
        token,
        "mark_dlq_permanent",
        {
            "job_id": str(job.id),
            "reason": "should refuse — not in DLQ",
            "idempotency_key": "mark-refuse-0001",
        },
    )
    assert body["error"]["code"] == protocol.MCP_TOOL_ERROR
    assert body["error"]["data"]["error_code"] == "not_found"
