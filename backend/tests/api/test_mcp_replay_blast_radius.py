"""`replay_dlq_messages` is the coarse, blind-batch replay tool — these
pin the blast radius the DLQ safety scenarios assume it has (R2-22).

`replay_dlq_by_category` refuses `human_required` outright, and
`mark_dlq_permanent` exists precisely to *put* a job in that category so
automatic replay stops touching it. But `replay_dlq_messages` filtered
on status and `job_type` only, so the one tool an agent reaches for when
it wants "replay the DLQ" swept the fenced entries back onto
`job.submitted` — where they re-fail, because `human_required` means a
persistent bug, not a transient one.

`replay_dlq_by_ids` has the same gap and is left alone deliberately:
there the caller names each id, which is a defensible way to say "yes,
this one". The blind batch is the one that needs the default.

The seeded eval world models exactly this: `scripts/seed_eval_fixtures.py`
seeds four DLQ rows, three replayable and one `human_required`
(`stable("dlq-job-csv-parse")`), and a scenario that fires a blind bulk
replay must leave that fourth row alone.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest_asyncio
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import registry
from app.mcp.standalone import create_mcp_app
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
    """KV surface the replay tools touch (idempotency records)."""

    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self,
        key: str,
        value: bytes | str,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def mget(self, keys: list[str]) -> list[bytes | str | None]:
        return [self._store.get(k) for k in keys]

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                removed += 1
        return removed


@pytest_asyncio.fixture
async def mcp_client(  # type: ignore[no-untyped-def]
    db_session: AsyncSession,
    default_tenant,
):
    app = create_mcp_app()
    redis_stub = _RedisStub()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
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
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


async def _call(
    ac: AsyncClient, token: str, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    body: dict[str, Any] = resp.json()
    return body


def _content(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


async def _seed_dlq(
    db_session: AsyncSession,
    default_tenant: Any,
    test_user: Any,
    hints: tuple[str | None, ...],
) -> list[uuid.UUID]:
    """One dead-lettered job per entry in `hints`. `None` models an
    organically dead-lettered job that triage never categorised."""
    ids: list[uuid.UUID] = []
    for hint in hints:
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
        ids.append(job.id)
    return ids


async def _status(db_session: AsyncSession, job_id: uuid.UUID) -> str:
    job = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    return str(job.status)


# --------------------------------------------------------------------------- #
# The fence                                                                    #
# --------------------------------------------------------------------------- #


async def test_blind_bulk_replay_skips_human_required_entries(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """RED before: `requested == 3, replayed == 3` — the fenced job went
    straight back onto `job.submitted` alongside the replayable ones."""
    safe_id, wait_id, fenced_id = await _seed_dlq(
        db_session,
        default_tenant,
        test_user,
        (
            RemediationHint.REPLAY_SAFE.value,
            RemediationHint.WAIT_AND_REPLAY.value,
            RemediationHint.HUMAN_REQUIRED.value,
        ),
    )
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_messages",
            {"idempotency_key": "blast-radius-1", "limit": 10},
        )
    )

    assert payload["replayed"] == 2
    replayed_ids = {j["id"] for j in payload["jobs"]}
    assert replayed_ids == {str(safe_id), str(wait_id)}
    assert str(fenced_id) not in replayed_ids

    # Reported, not silently dropped — the agent has to be able to see
    # that there is remaining DLQ work it is not allowed to automate.
    assert payload["skipped_human_required"] == 1
    assert [j["id"] for j in payload["skipped_jobs"]] == [str(fenced_id)]

    assert await _status(db_session, fenced_id) == JobStatus.DEAD_LETTER
    assert await _status(db_session, safe_id) == JobStatus.PENDING


async def test_blind_bulk_replay_still_takes_uncategorised_entries(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The exclusion is `human_required`, not "has a hint".

    A NULL hint means triage has not classified the entry yet, and the
    blind batch has always been the tool that sweeps those. Excluding
    them too would be a much larger behaviour change than the finding
    asks for — and `NOT IN ('human_required')` is NULL for a NULL
    column, so getting this wrong is the easy way to write the fix.
    """
    plain_id, fenced_id = await _seed_dlq(
        db_session,
        default_tenant,
        test_user,
        (None, RemediationHint.HUMAN_REQUIRED.value),
    )
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_messages",
            {"idempotency_key": "blast-radius-2", "limit": 10},
        )
    )

    assert [j["id"] for j in payload["jobs"]] == [str(plain_id)]
    assert payload["skipped_human_required"] == 1
    assert await _status(db_session, fenced_id) == JobStatus.DEAD_LETTER


async def test_blind_bulk_replay_takes_the_fenced_entry_on_explicit_opt_in(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """An operator who has reviewed the bug and shipped the fix must
    still have a bulk path — the default is a default, not a wall."""
    safe_id, fenced_id = await _seed_dlq(
        db_session,
        default_tenant,
        test_user,
        (
            RemediationHint.REPLAY_SAFE.value,
            RemediationHint.HUMAN_REQUIRED.value,
        ),
    )
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_messages",
            {
                "idempotency_key": "blast-radius-3",
                "limit": 10,
                "include_human_required": True,
            },
        )
    )

    assert payload["replayed"] == 2
    assert {j["id"] for j in payload["jobs"]} == {str(safe_id), str(fenced_id)}
    assert payload["skipped_human_required"] == 0
    assert payload["skipped_jobs"] == []
    assert await _status(db_session, fenced_id) == JobStatus.PENDING


async def test_job_type_narrowing_still_applies_to_the_skip_report(
    mcp_client, db_session, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The skip count is scoped by the same filters as the replay, so it
    can't report work the caller did not ask about."""
    (fenced_id,) = await _seed_dlq(
        db_session,
        default_tenant,
        test_user,
        (RemediationHint.HUMAN_REQUIRED.value,),
    )
    token = await _token(
        db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
    )

    payload = _content(
        await _call(
            mcp_client,
            token,
            "replay_dlq_messages",
            {
                "idempotency_key": "blast-radius-4",
                "limit": 10,
                "job_type": JobType.REPORT_GEN.value,
            },
        )
    )

    assert payload["requested"] == 0
    assert payload["skipped_human_required"] == 0
    assert await _status(db_session, fenced_id) == JobStatus.DEAD_LETTER


def test_the_tool_description_states_the_fence() -> None:
    """The agent reads the description and nothing else — a guard it
    cannot see is a guard it will fight. `replay_dlq_by_category` spells
    its refusal out; the bulk tool has to as well, including the name of
    the opt-in so the agent knows an escape hatch exists.
    """
    spec = registry.get_tool("replay_dlq_messages")
    assert spec is not None
    description = spec.description
    assert "human_required" in description
    assert "include_human_required" in description
