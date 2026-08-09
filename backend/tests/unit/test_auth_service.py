"""Unit tests for AuthService — repositories are mocked, no DB needed."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import hash_password, verify_password
from app.models.enums import UserRole
from app.models.user import User
from app.services.auth import AuthService

# The unusable-password sentinel chaos-created owners get
# (app/mcp/tools/chaos/create_bad_data_job.py). Not a bcrypt string.
CHAOS_OWNER_SENTINEL_HASH = "!chaos-owner-no-login"


def _make_user(**kwargs: object) -> User:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "email": "test@example.com",
        "hashed_password": hash_password("password123"),
        "role": UserRole.USER,
        "is_active": True,
    }
    defaults.update(kwargs)
    user = MagicMock(spec=User)
    for k, v in defaults.items():
        setattr(user, k, v)
    return user  # type: ignore[return-value]


def _make_service() -> tuple[AuthService, AsyncMock, AsyncMock]:
    user_repo = AsyncMock()
    audit_repo = AsyncMock()
    tenant_repo = AsyncMock()
    # Default to a valid tenant so `register` finds one.
    fake_tenant = MagicMock()
    fake_tenant.id = uuid.uuid4()
    fake_tenant.is_active = True
    tenant_repo.get_by_slug.return_value = fake_tenant
    svc = AuthService(user_repo, audit_repo, tenant_repo)
    return svc, user_repo, audit_repo


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


async def test_register_success() -> None:
    svc, user_repo, audit_repo = _make_service()
    user_repo.get_by_email.return_value = None
    new_user = _make_user(email="new@example.com")
    user_repo.create.return_value = new_user

    result = await svc.register("new@example.com", "password123")

    user_repo.create.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is new_user


async def test_register_duplicate_email_raises() -> None:
    svc, user_repo, _ = _make_service()
    user_repo.get_by_email.return_value = _make_user()

    with pytest.raises(ConflictError, match="already registered"):
        await svc.register("test@example.com", "password123")


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


async def test_login_success_returns_tokens() -> None:
    svc, user_repo, _ = _make_service()
    user_repo.get_by_email.return_value = _make_user()

    access, refresh = await svc.login("test@example.com", "password123")

    assert access
    assert refresh
    assert access != refresh


async def test_login_wrong_password_raises() -> None:
    svc, user_repo, _ = _make_service()
    user_repo.get_by_email.return_value = _make_user()

    with pytest.raises(AuthenticationError):
        await svc.login("test@example.com", "wrongpassword")


async def test_login_unknown_email_raises() -> None:
    svc, user_repo, _ = _make_service()
    user_repo.get_by_email.return_value = None

    with pytest.raises(AuthenticationError):
        await svc.login("nobody@example.com", "password123")


async def test_login_inactive_user_raises() -> None:
    svc, user_repo, _ = _make_service()
    user_repo.get_by_email.return_value = _make_user(is_active=False)

    with pytest.raises(AuthenticationError, match="disabled"):
        await svc.login("test@example.com", "password123")


async def test_login_against_unusable_password_hash_raises_auth_error() -> None:
    """Chaos-lab owner accounts carry a sentinel hash bcrypt cannot parse.

    `bcrypt.checkpw` raises ValueError('Invalid salt') on it, which used to
    escape login() as a 500 — a 500-vs-401 oracle that fingerprints chaos
    accounts (D-12). Verification must fail closed so the caller gets the
    same 401 as any other bad credential.

    Both `is_active` values are covered on purpose: the real chaos owner is
    inactive, but login() verifies the password BEFORE the is_active check,
    so an active account with an unparseable hash must fail cleanly too —
    reordering the checks would not be a fix.
    """
    for is_active in (True, False):
        svc, user_repo, _ = _make_service()
        user_repo.get_by_email.return_value = _make_user(
            email="chaos-owner+t@chaos.local",
            hashed_password=CHAOS_OWNER_SENTINEL_HASH,
            is_active=is_active,
        )

        with pytest.raises(AuthenticationError, match="Invalid email or password"):
            await svc.login("chaos-owner+t@chaos.local", "anything")


async def test_verify_password_rejects_non_bcrypt_hash() -> None:
    """The same guard at its source: any hash bcrypt can't parse is False,
    never an exception."""
    assert verify_password("anything", CHAOS_OWNER_SENTINEL_HASH) is False
    assert verify_password("anything", "") is False
