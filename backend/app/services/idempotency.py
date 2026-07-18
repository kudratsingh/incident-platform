"""
Idempotency policy for Tier 1 actions.

Two operations on a key:
  - `lookup(principal, tool_name, key, arguments)` → returns a
    `CacheHit` when a matching record exists (same key, same
    arguments_hash), raises `IdempotencyKeyReusedError` when the
    hash differs (Stripe-shape "key reused with different body"),
    returns `None` when there's no record yet.
  - `store(principal, tool_name, key, arguments, response, ttl)` —
    persist the tool's response under the key so the next lookup
    hits.

Argument hashing uses canonical JSON (sorted keys, tight separators)
so the same dict serializes identically across calls.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.exceptions import AppError
from app.dependencies import Principal
from app.models.idempotency import IdempotencyRecord
from app.repositories.idempotency import IdempotencyRepository


class IdempotencyKeyReusedError(AppError):
    """Same key + different arguments than the original call. The
    caller should either send a fresh key or send the exact same
    arguments as before."""

    status_code = 409
    error_code = "idempotency_key_reused"


@dataclass(frozen=True)
class CacheHit:
    response: dict[str, Any]
    stored_at: datetime


def _hash_arguments(arguments: dict[str, Any]) -> str:
    """Canonical-JSON SHA-256. Sorted keys + tight separators so the
    same dict hashes the same regardless of insertion order or
    whitespace."""
    body = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(body).hexdigest()


class IdempotencyService:
    def __init__(self, repo: IdempotencyRepository) -> None:
        self.repo = repo

    async def lookup(
        self,
        *,
        principal: Principal,
        tool_name: str,
        idempotency_key: str,
        arguments: dict[str, Any],
    ) -> CacheHit | None:
        record = await self.repo.get_by_key(
            tenant_id=principal.tenant_id,
            principal_id=principal.id,
            idempotency_key=idempotency_key,
        )
        if record is None:
            return None
        # Expired records are treated as absent — the reaper cleans
        # them up later; the caller re-executes and stores fresh.
        if record.expires_at is not None:
            expires_at = record.expires_at
            # SQLite round-trips datetimes as naive; normalize before compare.
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < datetime.now(UTC):
                return None

        arguments_hash = _hash_arguments(arguments)
        if record.arguments_hash != arguments_hash:
            raise IdempotencyKeyReusedError(
                f"Idempotency key {idempotency_key!r} was previously used for "
                f"tool {record.tool_name!r} with different arguments. Pick a "
                "fresh key or send the exact same arguments."
            )
        if record.tool_name != tool_name:
            # Cross-tool key collision — refuse the same way.
            raise IdempotencyKeyReusedError(
                f"Idempotency key {idempotency_key!r} was previously used for "
                f"a different tool ({record.tool_name!r})."
            )
        return CacheHit(
            response=dict(record.response_json or {}),
            stored_at=record.created_at,
        )

    async def store(
        self,
        *,
        principal: Principal,
        tool_name: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        response: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> IdempotencyRecord:
        expires_at: datetime | None = None
        if ttl is not None:
            expires_at = datetime.now(UTC) + ttl
        return await self.repo.create(
            id=uuid.uuid4(),
            tenant_id=principal.tenant_id,
            principal_id=principal.id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            arguments_hash=_hash_arguments(arguments),
            response_json=response,
            expires_at=expires_at,
        )


__all__ = [
    "CacheHit",
    "IdempotencyKeyReusedError",
    "IdempotencyService",
    "_hash_arguments",
]
