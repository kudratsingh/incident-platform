"""Unit tests for AuthService — repositories are mocked, no DB needed."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.exceptions import AuthenticationError, ConflictError
from app.core.security import hash_password
from app.models.enums import UserRole
from app.models.user import User
from app.services.auth import AuthService


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
    svc = AuthService(user_repo, audit_repo)
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
