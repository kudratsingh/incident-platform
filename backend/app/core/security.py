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

    Fails closed on any hash bcrypt cannot parse instead of raising.
    Chaos-lab owner accounts are stored with the unusable sentinel
    `!chaos-owner-no-login` (app/mcp/tools/chaos/create_bad_data_job.py);
    `checkpw` raises ValueError('Invalid salt') on it, which escaped
    `AuthService.login` as a 500 and turned the status code into an oracle
    for "this address is a chaos account" (D-12). A hash that cannot be
    parsed is simply a credential nothing can match, so the caller gets the
    same 401 as any other bad password.
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


# Native EventSource cannot set an Authorization header, so the SSE stream
# authenticates with a token in the URL. That token must never be the primary
# access JWT — URLs end up in access logs, proxies, and browser history.
# Instead we mint a single-purpose token that only authorizes streaming ONE
# job and dies after this many seconds. See ADR 0014.
STREAM_TOKEN_TTL_SECONDS = 60


def create_stream_token(job_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """Mint a short-lived token authorizing the SSE stream for exactly one job.

    The subject is the JOB id, not a user id: the stream route compares it to
    its path parameter, so a token minted for job X can never open job Y's
    stream. Callers must authorize the job (tenant scope + ownership) BEFORE
    minting — the stream route trusts the token alone.
    """
    return _make_token(
        {"sub": str(job_id), "tenant_id": str(tenant_id)},
        timedelta(seconds=STREAM_TOKEN_TTL_SECONDS),
        "stream",
    )


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
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
