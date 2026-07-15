"""
Service-account and token lifecycle: create, mint, revoke, verify.

The plaintext of a bearer token is generated here, returned to the caller
exactly once, and never persisted. What we store is the SHA-256 hex hash;
verification hashes the presented token and looks it up.

Token format is `sa_` + 43 URL-safe base64 chars (256 bits of entropy from
`secrets.token_urlsafe(32)`). The `sa_` prefix is how the auth dependency
distinguishes machine tokens from human JWTs without decoding.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.core.logging import get_logger, request_id_var
from app.core.scopes import ALL_SCOPES, validate_scopes
from app.models.service_account import ServiceAccount, ServiceAccountToken
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)

logger = get_logger(__name__)


TOKEN_PREFIX = "sa_"
DEFAULT_TOKEN_TTL = timedelta(days=90)


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def looks_like_service_account_token(value: str) -> bool:
    """Cheap prefix check used by the auth dependency to route between JWT
    verification and DB lookup. Does not validate the token — that's the
    verifier's job."""
    return value.startswith(TOKEN_PREFIX)


class ServiceAccountService:
    def __init__(
        self,
        sa_repo: ServiceAccountRepository,
        token_repo: ServiceAccountTokenRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self.sa_repo = sa_repo
        self.token_repo = token_repo
        self.audit_repo = audit_repo

    # ------------------------------------------------------------------
    # Service accounts
    # ------------------------------------------------------------------

    async def create_service_account(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str,
        scopes: list[str],
        created_by_user_id: uuid.UUID | None,
    ) -> ServiceAccount:
        try:
            validate_scopes(scopes)
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc

        existing = await self.sa_repo.get_by_name(tenant_id, name)
        if existing is not None:
            raise ConflictError(f"Service account already exists: {name}")

        sa = await self.sa_repo.create(
            tenant_id=tenant_id,
            name=name,
            scopes=scopes,
            is_active=True,
            created_by_user_id=created_by_user_id,
        )
        await self.audit_repo.log(
            "service_account.created",
            user_id=created_by_user_id,
            tenant_id=tenant_id,
            resource_type="service_account",
            resource_id=str(sa.id),
            request_id=request_id_var.get("") or None,
            extra_data={"name": name, "scopes": scopes},
        )
        logger.info(
            "service account created",
            extra={
                "sa_id": str(sa.id),
                "sa_name": name,
                "tenant_id": str(tenant_id),
                "scopes": scopes,
            },
        )
        return sa

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    async def mint_token(
        self,
        *,
        service_account: ServiceAccount,
        scopes: list[str] | None,
        ttl: timedelta | None,
        minted_by_user_id: uuid.UUID | None,
    ) -> tuple[ServiceAccountToken, str]:
        """Mint a new bearer token. Returns the persisted row + the plaintext
        (shown to the operator once)."""
        if not service_account.is_active:
            raise AuthorizationError("Service account is disabled")

        requested = scopes if scopes is not None else list(service_account.scopes)
        try:
            validate_scopes(requested)
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc

        # The token's scopes must be a subset of the account's — you can't
        # narrow *up* by minting a broader token than the account carries.
        allowed = set(service_account.scopes)
        extra = [s for s in requested if s not in allowed]
        if extra:
            raise AuthorizationError(
                f"Requested scopes exceed account scopes: {extra}"
            )

        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_hash = _hash_token(plaintext)
        effective_ttl = ttl if ttl is not None else DEFAULT_TOKEN_TTL
        expires_at = datetime.now(UTC) + effective_ttl

        row = await self.token_repo.create(
            service_account_id=service_account.id,
            token_hash=token_hash,
            scopes=requested,
            expires_at=expires_at,
        )
        await self.audit_repo.log(
            "service_account.token_minted",
            user_id=minted_by_user_id,
            tenant_id=service_account.tenant_id,
            resource_type="service_account_token",
            resource_id=str(row.id),
            request_id=request_id_var.get("") or None,
            extra_data={
                "service_account_id": str(service_account.id),
                "scopes": requested,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
        )
        logger.info(
            "service account token minted",
            extra={
                "sa_id": str(service_account.id),
                "token_id": str(row.id),
                "scopes": requested,
            },
        )
        return row, plaintext

    async def revoke_token(
        self,
        *,
        service_account: ServiceAccount,
        token_id: uuid.UUID,
        revoked_by_user_id: uuid.UUID | None,
    ) -> ServiceAccountToken:
        token = await self.token_repo.get_by_id(token_id)
        if token is None or token.service_account_id != service_account.id:
            raise NotFoundError(f"Token not found: {token_id}")
        if token.revoked_at is not None:
            # Idempotent: already revoked → no-op, no audit noise
            return token

        token.revoked_at = datetime.now(UTC)
        await self.token_repo.session.flush()
        await self.audit_repo.log(
            "service_account.token_revoked",
            user_id=revoked_by_user_id,
            tenant_id=service_account.tenant_id,
            resource_type="service_account_token",
            resource_id=str(token.id),
            request_id=request_id_var.get("") or None,
            extra_data={"service_account_id": str(service_account.id)},
        )
        logger.info(
            "service account token revoked",
            extra={"sa_id": str(service_account.id), "token_id": str(token.id)},
        )
        return token

    # ------------------------------------------------------------------
    # Verification (called from the auth dependency)
    # ------------------------------------------------------------------

    async def verify_token(
        self, plaintext: str
    ) -> tuple[ServiceAccount, ServiceAccountToken]:
        """Look up a presented bearer token; raise AuthenticationError on any
        failure mode. Success returns (service_account, token) — both loaded
        and validated (active account, not revoked, not expired)."""
        if not looks_like_service_account_token(plaintext):
            raise AuthenticationError("Not a service-account token")

        token_hash = _hash_token(plaintext)
        token = await self.token_repo.get_by_hash(token_hash)
        if token is None:
            raise AuthenticationError("Invalid token")
        if token.revoked_at is not None:
            raise AuthenticationError("Token has been revoked")
        # DateTime(timezone=True) round-trips as aware on Postgres but naive on
        # SQLite (which has no tz support). Coerce naive to UTC so the
        # comparison below never raises TypeError on the tests.
        if token.expires_at is not None:
            expires_at = token.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < datetime.now(UTC):
                raise AuthenticationError("Token has expired")

        sa = await self.sa_repo.get_by_id(token.service_account_id)
        if sa is None:
            # Cascade should prevent this; defensive.
            raise AuthenticationError("Service account not found")
        if not sa.is_active:
            raise AuthenticationError("Service account is disabled")

        # Best-effort last-used timestamp. Not on the critical path — a race
        # is fine; we just want operators to spot dead tokens.
        token.last_used_at = datetime.now(UTC)
        return sa, token


__all__ = [
    "ALL_SCOPES",
    "DEFAULT_TOKEN_TTL",
    "TOKEN_PREFIX",
    "ServiceAccountService",
    "looks_like_service_account_token",
]
