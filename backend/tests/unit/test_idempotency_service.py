"""Unit tests for IdempotencyService — canonical-JSON hashing +
hit/miss/mismatch semantics."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.dependencies import Principal
from app.models.idempotency import IdempotencyRecord
from app.services.idempotency import (
    IdempotencyKeyReusedError,
    IdempotencyService,
    _hash_arguments,
)


def _sa_principal(tenant_id: uuid.UUID | None = None) -> Principal:
    from app.models.service_account import ServiceAccount

    sa = ServiceAccount()
    sa.id = uuid.uuid4()
    sa.tenant_id = tenant_id or uuid.uuid4()
    sa.name = "probe"
    sa.scopes = []
    sa.is_active = True
    return Principal(
        kind="service_account",
        tenant_id=sa.tenant_id,
        service_account=sa,
        scopes=frozenset(),
    )


def _make_record(**over: object) -> IdempotencyRecord:
    r = IdempotencyRecord()
    r.id = uuid.uuid4()
    r.tenant_id = over.get("tenant_id", uuid.uuid4())  # type: ignore[assignment]
    r.principal_id = over.get("principal_id", uuid.uuid4())  # type: ignore[assignment]
    r.tool_name = over.get("tool_name", "restart_consumer_group")  # type: ignore[assignment]
    r.idempotency_key = over.get("idempotency_key", "k-1")  # type: ignore[assignment]
    r.arguments_hash = over.get("arguments_hash", "hash-1")  # type: ignore[assignment]
    r.response_json = over.get("response_json", {"ok": True})  # type: ignore[assignment]
    r.created_at = over.get("created_at", datetime.now(UTC))  # type: ignore[assignment]
    r.expires_at = over.get("expires_at")  # type: ignore[assignment]
    return r


def test_hash_is_stable_across_key_order() -> None:
    """Canonical JSON — dict order doesn't change the hash."""
    a = _hash_arguments({"a": 1, "b": 2, "c": [1, 2]})
    b = _hash_arguments({"c": [1, 2], "b": 2, "a": 1})
    assert a == b


def test_hash_changes_with_values() -> None:
    a = _hash_arguments({"a": 1})
    b = _hash_arguments({"a": 2})
    assert a != b


async def test_lookup_returns_none_when_no_record() -> None:
    repo = AsyncMock()
    repo.get_by_key.return_value = None
    svc = IdempotencyService(repo)
    hit = await svc.lookup(
        principal=_sa_principal(),
        tool_name="pause_dag",
        idempotency_key="k-1",
        arguments={"anything": True, "idempotency_key": "k-1"},
    )
    assert hit is None


async def test_lookup_hit_returns_cached_response() -> None:
    principal = _sa_principal()
    args = {"consumer_group": "worker-dispatcher", "idempotency_key": "k-hit"}
    record = _make_record(
        tenant_id=principal.tenant_id,
        principal_id=principal.id,
        tool_name="restart_consumer_group",
        idempotency_key="k-hit",
        arguments_hash=_hash_arguments(args),
        response_json={"consumer_group": "worker-dispatcher", "accepted": True},
    )
    repo = AsyncMock()
    repo.get_by_key.return_value = record

    hit = await IdempotencyService(repo).lookup(
        principal=principal,
        tool_name="restart_consumer_group",
        idempotency_key="k-hit",
        arguments=args,
    )
    assert hit is not None
    assert hit.response == {"consumer_group": "worker-dispatcher", "accepted": True}


async def test_lookup_hash_mismatch_raises() -> None:
    """Same key, different args → refuse. This is the load-bearing
    check that catches accidental key reuse."""
    principal = _sa_principal()
    orig_args = {"consumer_group": "wd", "idempotency_key": "k-1"}
    new_args = {"consumer_group": "OTHER", "idempotency_key": "k-1"}
    record = _make_record(
        tenant_id=principal.tenant_id,
        principal_id=principal.id,
        arguments_hash=_hash_arguments(orig_args),
    )
    repo = AsyncMock()
    repo.get_by_key.return_value = record
    with pytest.raises(IdempotencyKeyReusedError):
        await IdempotencyService(repo).lookup(
            principal=principal,
            tool_name=record.tool_name,
            idempotency_key="k-1",
            arguments=new_args,
        )


async def test_lookup_cross_tool_key_collision_raises() -> None:
    """Same key across tools → refuse (rare but real; the underlying
    UNIQUE constraint scopes by (tenant, principal, key) not tool)."""
    principal = _sa_principal()
    args = {"idempotency_key": "shared"}
    record = _make_record(
        tenant_id=principal.tenant_id,
        principal_id=principal.id,
        tool_name="restart_consumer_group",
        arguments_hash=_hash_arguments(args),
    )
    repo = AsyncMock()
    repo.get_by_key.return_value = record

    with pytest.raises(IdempotencyKeyReusedError, match="different tool"):
        await IdempotencyService(repo).lookup(
            principal=principal,
            tool_name="invalidate_cache_key",
            idempotency_key="shared",
            arguments=args,
        )


async def test_expired_record_returns_none() -> None:
    """Expired records don't count — the reaper cleans them lazily,
    the service treats them as absent so a fresh execution runs."""
    principal = _sa_principal()
    args = {"idempotency_key": "old"}
    record = _make_record(
        tenant_id=principal.tenant_id,
        principal_id=principal.id,
        arguments_hash=_hash_arguments(args),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    repo = AsyncMock()
    repo.get_by_key.return_value = record

    hit = await IdempotencyService(repo).lookup(
        principal=principal,
        tool_name=record.tool_name,
        idempotency_key="old",
        arguments=args,
    )
    assert hit is None


async def test_store_persists_hash_and_response() -> None:
    principal = _sa_principal()
    repo = AsyncMock()

    def _create(**kwargs):  # type: ignore[no-untyped-def]
        m = MagicMock()
        for k, v in kwargs.items():
            setattr(m, k, v)
        return m

    repo.create.side_effect = lambda **kw: _create(**kw)
    args = {"key": "cache:x", "idempotency_key": "k-new"}
    resp = {"key": "cache:x", "deleted": True}

    await IdempotencyService(repo).store(
        principal=principal,
        tool_name="invalidate_cache_key",
        idempotency_key="k-new",
        arguments=args,
        response=resp,
        ttl=timedelta(hours=1),
    )
    repo.create.assert_awaited_once()
    _, kwargs = repo.create.call_args
    assert kwargs["arguments_hash"] == _hash_arguments(args)
    assert kwargs["response_json"] == resp
    assert kwargs["expires_at"] is not None


# ---------------------------------------------------------------------------
# Reaper — ADR 0010 follow-up (v0.4.8)
# ---------------------------------------------------------------------------


async def test_delete_expired_removes_only_records_past_expires_at(
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """Reaper query bounds: rows with expires_at in the past go away;
    rows with expires_at in the future stay; rows with expires_at IS
    NULL (no TTL — legacy or hypothetical future) are never touched."""
    from app.repositories.idempotency import IdempotencyRepository

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()

    def _rec(*, key: str, expires_at: datetime | None) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            principal_id=principal_id,
            tool_name="restart_consumer_group",
            idempotency_key=key,
            arguments_hash=_hash_arguments({"consumer_group": key}),
            response_json={"ok": True},
            expires_at=expires_at,
        )

    fresh = _rec(key="fresh", expires_at=now + timedelta(hours=1))
    expired = _rec(key="expired", expires_at=now - timedelta(hours=1))
    no_ttl = _rec(key="no-ttl", expires_at=None)
    for row in (fresh, expired, no_ttl):
        db_session.add(row)
    await db_session.flush()

    reaped = await IdempotencyRepository(db_session).delete_expired(now=now)
    assert reaped == 1  # only `expired`

    from sqlalchemy import select as _select

    remaining_keys = {
        r.idempotency_key
        for r in (
            await db_session.execute(_select(IdempotencyRecord))
        ).scalars().all()
    }
    assert remaining_keys == {"fresh", "no-ttl"}


async def test_delete_expired_noop_when_nothing_expired(
    db_session,  # type: ignore[no-untyped-def]
) -> None:
    """No expired rows → no rows deleted → returns 0. Reaper loop
    skips its log line on this path so we don't spam operators with
    'reaped 0' every hour."""
    from app.repositories.idempotency import IdempotencyRepository

    now = datetime.now(UTC)
    db_session.add(
        IdempotencyRecord(
            id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            tool_name="pause_dag",
            idempotency_key="k1",
            arguments_hash="deadbeef",
            response_json={},
            expires_at=now + timedelta(hours=12),
        )
    )
    await db_session.flush()

    reaped = await IdempotencyRepository(db_session).delete_expired(now=now)
    assert reaped == 0
