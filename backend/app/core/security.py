"""Password hashing and the JWTs behind login, refresh and the SSE stream
token. Every token this file mints carries its own `type`, and decoding
refuses a token of the wrong one."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from app.config import get_settings
from app.core.exceptions import AuthenticationError
from jose import JWTError, jwt

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """True only when `plain` matches a well-formed bcrypt `hashed`.

    Fails closed on an unparseable hash instead of raising: the chaos-lab
    sentinel `!chaos-owner-no-login` made `checkpw` raise, and the 500 was an
    oracle for "this address is a chaos account" (D-12). Unparseable means 401.
    """
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_token(data: dict[str, Any], expires_delta: timedelta, token_type: str) -> str:
    settings = get_settings()
    payload = {
        **data,
        "exp": datetime.now(UTC) + expires_delta,
        "iat": datetime.now(UTC),
        "type": token_type,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(data: dict[str, Any]) -> str:
    settings = get_settings()
    return _make_token(
        data,
        timedelta(minutes=settings.access_token_expire_minutes),
        "access",
    )


def create_refresh_token(data: dict[str, Any]) -> str:
    settings = get_settings()
    return _make_token(
        data,
        timedelta(days=settings.refresh_token_expire_days),
        "refresh",
    )


# EventSource cannot set headers, so the SSE stream takes a URL token — never
# the access JWT. It authorizes ONE job, for this many seconds. ADR 0014.
STREAM_TOKEN_TTL_SECONDS = 60


def create_stream_token(job_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Mint a short-lived token authorizing the SSE stream for exactly one job.

    Subject is the JOB id. Authorize the job BEFORE minting: the route trusts only this.
    """
    return _make_token(
        {"sub": str(job_id), "tenant_id": str(tenant_id)},
        timedelta(seconds=STREAM_TOKEN_TTL_SECONDS),
        "stream",
    )


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    """Verify a token and return its claims, refusing one of the wrong kind —
    a refresh token may not be spent as an access token."""
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except JWTError as exc:
        raise AuthenticationError("Invalid or expired token") from exc

    if payload.get("type") != expected_type:
        raise AuthenticationError(f"Expected {expected_type} token, got {payload.get('type')}")

    return payload
