from app.core.exceptions import AuthenticationError, ConflictError
from app.core.logging import get_logger, request_id_var, user_id_var
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.repositories.audit import AuditRepository
from app.repositories.user import UserRepository

logger = get_logger(__name__)


class AuthService:
    def __init__(
        self, user_repo: UserRepository, audit_repo: AuditRepository
    ) -> None:
        self.user_repo = user_repo
        self.audit_repo = audit_repo

    async def register(
        self,
        email: str,
        password: str,
        role: str = "user",
        ip_address: str | None = None,
    ) -> User:
        existing = await self.user_repo.get_by_email(email)
        if existing:
            raise ConflictError(f"Email already registered: {email}")

        user = await self.user_repo.create(
            email=email,
            hashed_password=hash_password(password),
            role=role,
        )
        await self.audit_repo.log(
            "user.registered",
            user_id=user.id,
            resource_type="user",
            resource_id=str(user.id),
            request_id=request_id_var.get("") or None,
            ip_address=ip_address,
        )
        logger.info("user registered", extra={"email": email, "role": role})
        return user

    async def login(
        self, email: str, password: str, ip_address: str | None = None
    ) -> tuple[str, str]:
        user = await self.user_repo.get_by_email(email)
        if not user or not verify_password(password, user.hashed_password):
            raise AuthenticationError("Invalid email or password")
        if not user.is_active:
            raise AuthenticationError("Account is disabled")

        token_data = {"sub": str(user.id), "role": user.role, "email": user.email}
        access_token = create_access_token(token_data)
        refresh_token = create_refresh_token(token_data)

        user_id_var.set(str(user.id))
        await self.audit_repo.log(
            "user.login",
            user_id=user.id,
            resource_type="user",
            resource_id=str(user.id),
            request_id=request_id_var.get("") or None,
            ip_address=ip_address,
        )
        logger.info("user login", extra={"email": email})
        return access_token, refresh_token

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        payload = decode_token(refresh_token, expected_type="refresh")
        user_id = payload["sub"]
        import uuid
        user = await self.user_repo.get_by_id(uuid.UUID(user_id))
        if not user or not user.is_active:
            raise AuthenticationError("User not found or disabled")

        token_data = {"sub": str(user.id), "role": user.role, "email": user.email}
        return create_access_token(token_data), create_refresh_token(token_data)
