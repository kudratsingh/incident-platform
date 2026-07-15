"""Unit tests for ServiceAccountService — verifies scope subset validation,
token minting shape, and the four verify_token failure modes (bad prefix,
unknown hash, revoked, expired)."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
)
from app.core.scopes import Scope
from app.models.service_account import ServiceAccount, ServiceAccountToken
from app.services.service_account import (
    TOKEN_PREFIX,
    ServiceAccountService,
)


def _make_sa(**kwargs: object) -> ServiceAccount:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "name": "test-sa",
        "scopes": [Scope.TELEMETRY_READ.value, Scope.INCIDENTS_READ.value],
        "is_active": True,
        "created_by_user_id": None,
    }
    defaults.update(kwargs)
    sa = MagicMock(spec=ServiceAccount)
    for k, v in defaults.items():
        setattr(sa, k, v)
    return sa  # type: ignore[return-value]


def _make_token(**kwargs: object) -> ServiceAccountToken:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "service_account_id": uuid.uuid4(),
        "token_hash": "deadbeef" * 8,
        "scopes": [Scope.TELEMETRY_READ.value],
        "expires_at": datetime.now(UTC) + timedelta(days=30),
        "revoked_at": None,
        "last_used_at": None,
    }
    defaults.update(kwargs)
    token = MagicMock(spec=ServiceAccountToken)
    for k, v in defaults.items():
        setattr(token, k, v)
    return token  # type: ignore[return-value]


def _make_service() -> tuple[ServiceAccountService, AsyncMock, AsyncMock, AsyncMock]:
    sa_repo = AsyncMock()
    token_repo = AsyncMock()
    # token_repo.session.flush is called by revoke_token
    token_repo.session = MagicMock()
    token_repo.session.flush = AsyncMock()
    audit_repo = AsyncMock()
    svc = ServiceAccountService(sa_repo, token_repo, audit_repo)
    return svc, sa_repo, token_repo, audit_repo


# ---------------------------------------------------------------------------
# create_service_account
# ---------------------------------------------------------------------------


async def test_create_rejects_unknown_scope() -> None:
    svc, sa_repo, _, _ = _make_service()
    sa_repo.get_by_name.return_value = None
    with pytest.raises(AuthorizationError):
        await svc.create_service_account(
            tenant_id=uuid.uuid4(),
            name="bad",
            scopes=["telemetry:read", "not:a:scope"],
            created_by_user_id=None,
        )


async def test_create_rejects_duplicate_name_in_tenant() -> None:
    svc, sa_repo, _, _ = _make_service()
    sa_repo.get_by_name.return_value = _make_sa()
    with pytest.raises(ConflictError):
        await svc.create_service_account(
            tenant_id=uuid.uuid4(),
            name="dup",
            scopes=[Scope.TELEMETRY_READ.value],
            created_by_user_id=None,
        )


async def test_create_writes_audit_row() -> None:
    svc, sa_repo, _, audit_repo = _make_service()
    sa_repo.get_by_name.return_value = None
    sa_repo.create.return_value = _make_sa(name="ok")
    await svc.create_service_account(
        tenant_id=uuid.uuid4(),
        name="ok",
        scopes=[Scope.TELEMETRY_READ.value],
        created_by_user_id=uuid.uuid4(),
    )
    audit_repo.log.assert_awaited_once()
    args, kwargs = audit_repo.log.call_args
    assert args[0] == "service_account.created"


# ---------------------------------------------------------------------------
# mint_token
# ---------------------------------------------------------------------------


async def test_mint_returns_plaintext_with_expected_prefix() -> None:
    svc, _, token_repo, _ = _make_service()
    sa = _make_sa()
    token_repo.create.return_value = _make_token(service_account_id=sa.id)
    _, plaintext = await svc.mint_token(
        service_account=sa,
        scopes=None,
        ttl=None,
        minted_by_user_id=None,
    )
    assert plaintext.startswith(TOKEN_PREFIX)
    assert len(plaintext) > 40  # 256 bits of entropy urlsafe-encoded


async def test_mint_hashes_the_persisted_token() -> None:
    svc, _, token_repo, _ = _make_service()
    sa = _make_sa()
    token_repo.create.return_value = _make_token(service_account_id=sa.id)
    _, plaintext = await svc.mint_token(
        service_account=sa,
        scopes=None,
        ttl=None,
        minted_by_user_id=None,
    )
    _, kwargs = token_repo.create.call_args
    assert kwargs["token_hash"] == hashlib.sha256(plaintext.encode()).hexdigest()


async def test_mint_rejects_scope_not_on_account() -> None:
    svc, _, _, _ = _make_service()
    sa = _make_sa(scopes=[Scope.TELEMETRY_READ.value])
    with pytest.raises(AuthorizationError) as ei:
        await svc.mint_token(
            service_account=sa,
            scopes=[Scope.TELEMETRY_READ.value, Scope.ACTIONS_EXECUTE.value],
            ttl=None,
            minted_by_user_id=None,
        )
    assert "exceed account scopes" in str(ei.value.message).lower() or \
        "exceed" in str(ei.value)


async def test_mint_rejects_inactive_account() -> None:
    svc, _, _, _ = _make_service()
    sa = _make_sa(is_active=False)
    with pytest.raises(AuthorizationError):
        await svc.mint_token(
            service_account=sa,
            scopes=None,
            ttl=None,
            minted_by_user_id=None,
        )


async def test_mint_respects_custom_ttl() -> None:
    svc, _, token_repo, _ = _make_service()
    sa = _make_sa()
    token_repo.create.return_value = _make_token(service_account_id=sa.id)
    await svc.mint_token(
        service_account=sa,
        scopes=None,
        ttl=timedelta(days=7),
        minted_by_user_id=None,
    )
    _, kwargs = token_repo.create.call_args
    delta = kwargs["expires_at"] - datetime.now(UTC)
    # Allow a couple seconds of jitter
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7, minutes=1)


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


async def test_verify_rejects_non_sa_prefix() -> None:
    svc, _, _, _ = _make_service()
    with pytest.raises(AuthenticationError):
        await svc.verify_token("eyJhbGciOiJIUzI1NiJ9.notajwt")


async def test_verify_rejects_unknown_hash() -> None:
    svc, _, token_repo, _ = _make_service()
    token_repo.get_by_hash.return_value = None
    with pytest.raises(AuthenticationError):
        await svc.verify_token(TOKEN_PREFIX + "nope")


async def test_verify_rejects_revoked_token() -> None:
    svc, sa_repo, token_repo, _ = _make_service()
    token_repo.get_by_hash.return_value = _make_token(
        revoked_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    sa_repo.get_by_id.return_value = _make_sa()
    with pytest.raises(AuthenticationError) as ei:
        await svc.verify_token(TOKEN_PREFIX + "whatever")
    assert "revoked" in str(ei.value.message).lower()


async def test_verify_rejects_expired_token() -> None:
    svc, sa_repo, token_repo, _ = _make_service()
    token_repo.get_by_hash.return_value = _make_token(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    sa_repo.get_by_id.return_value = _make_sa()
    with pytest.raises(AuthenticationError) as ei:
        await svc.verify_token(TOKEN_PREFIX + "whatever")
    assert "expired" in str(ei.value.message).lower()


async def test_verify_rejects_inactive_service_account() -> None:
    svc, sa_repo, token_repo, _ = _make_service()
    token_repo.get_by_hash.return_value = _make_token()
    sa_repo.get_by_id.return_value = _make_sa(is_active=False)
    with pytest.raises(AuthenticationError) as ei:
        await svc.verify_token(TOKEN_PREFIX + "whatever")
    assert "disabled" in str(ei.value.message).lower()


async def test_verify_success_updates_last_used_at() -> None:
    svc, sa_repo, token_repo, _ = _make_service()
    token = _make_token(last_used_at=None)
    token_repo.get_by_hash.return_value = token
    sa_repo.get_by_id.return_value = _make_sa()
    sa, returned = await svc.verify_token(TOKEN_PREFIX + "whatever")
    assert returned is token
    assert token.last_used_at is not None


# ---------------------------------------------------------------------------
# revoke_token
# ---------------------------------------------------------------------------


async def test_revoke_is_idempotent() -> None:
    svc, _, token_repo, audit_repo = _make_service()
    already = _make_token(revoked_at=datetime.now(UTC) - timedelta(days=1))
    token_repo.get_by_id.return_value = already
    sa = _make_sa(id=already.service_account_id)
    await svc.revoke_token(
        service_account=sa,
        token_id=already.id,
        revoked_by_user_id=None,
    )
    # Already-revoked → no audit noise
    audit_repo.log.assert_not_called()


async def test_revoke_stamps_revoked_at() -> None:
    svc, _, token_repo, audit_repo = _make_service()
    token = _make_token(revoked_at=None)
    token_repo.get_by_id.return_value = token
    sa = _make_sa(id=token.service_account_id)
    await svc.revoke_token(
        service_account=sa,
        token_id=token.id,
        revoked_by_user_id=uuid.uuid4(),
    )
    assert token.revoked_at is not None
    audit_repo.log.assert_awaited_once()
    args, _ = audit_repo.log.call_args
    assert args[0] == "service_account.token_revoked"
