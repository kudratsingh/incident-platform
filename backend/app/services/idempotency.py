"""
Idempotency policy for Tier 1 actions.

The key is *claimed before the action runs*, not recorded after it (R2-27) — the old
lookup-then-insert let two concurrent calls both execute. `acquire` returns a `Claim` or a
`Replay`, `complete` makes the claim replayable, `release` drops an unfinished one; every
path that does not complete MUST release, or the key wedges for its whole TTL.
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
    """Same key, different arguments or tool than the original call."""

    status_code = 409
    error_code = "idempotency_key_reused"


@dataclass(frozen=True)
class CacheHit:
    """The answer a previous call already gave for this key."""

    response: dict[str, Any]
    stored_at: datetime


@dataclass(frozen=True)
class Claim:
    """A key this caller owns and has not yet answered for."""

    record_id: uuid.UUID


@dataclass(frozen=True)
class Replay:
    """Someone else already answered for this key. Their answer is the
    answer — ours, if we were to produce one, would not be."""

    hit: CacheHit


class IdempotencyKeyInFlightError(AppError):
    """The key is held by a claim with no response yet — another call is executing it.

    Retryable, unlike `IdempotencyKeyReusedError`, hence its own code."""

    status_code = 409
    error_code = "idempotency_key_in_flight"


def _hash_arguments(arguments: dict[str, Any]) -> str:
    """Canonical-JSON SHA-256: sorted keys + tight separators, so order cannot change it.

    Cross-repo contract — the commander's snapshot pins these bytes, so changing the input
    shape, `sort_keys`, `separators`, `default=` or the algorithm is a version-sync (ADR 0010).
    """
    body = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _is_expired(record: IdempotencyRecord) -> bool:
    if record.expires_at is None:
        return False
    expires_at = record.expires_at
    # SQLite round-trips datetimes as naive; normalize before compare.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at < datetime.now(UTC)


class IdempotencyService:
    """Decides whether a Tier-1 call may run, or must return an earlier
    answer."""

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
        """Read a key without taking it: the stored answer, or None if there is
        none. Refuses the same ways `acquire` does."""
        record = await self.repo.get_by_key(
            tenant_id=principal.tenant_id,
            principal_id=principal.id,
            idempotency_key=idempotency_key,
        )
        if record is None:
            return None
        if _is_expired(record):
            # Evict rather than read past it — treating the row as absent while
            # the UNIQUE index held it was finding #2 (collide after effect).
            await self.repo.delete_by_id(record_id=record.id)
            return None

        # Same refusals `acquire` gives, from one implementation.
        self._assert_same_call(
            record=record,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            arguments_hash=_hash_arguments(arguments),
        )
        if record.response_json is None:
            raise IdempotencyKeyInFlightError(
                f"Idempotency key {idempotency_key!r} is currently being "
                "executed by another call. Retry shortly."
            )
        return CacheHit(
            response=dict(record.response_json),
            stored_at=record.created_at,
        )

    async def acquire(
        self,
        *,
        principal: Principal,
        tool_name: str,
        idempotency_key: str,
        arguments: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> Claim | Replay:
        """Take the key, or find out who already has it.

        A `Claim` means this caller won and must execute; a `Replay` carries the answer
        already recorded. Raises `IdempotencyKeyReusedError` (different arguments/tool) or
        `IdempotencyKeyInFlightError` (claim, no response). At most one retry, then defer.
        """
        expires_at = (
            datetime.now(UTC) + ttl if ttl is not None else None
        )
        arguments_hash = _hash_arguments(arguments)

        for attempt in (1, 2):
            record_id = await self.repo.insert_claim(
                tenant_id=principal.tenant_id,
                principal_id=principal.id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                arguments_hash=arguments_hash,
                expires_at=expires_at,
            )
            if record_id is not None:
                return Claim(record_id=record_id)

            holder = await self.repo.get_by_key(
                tenant_id=principal.tenant_id,
                principal_id=principal.id,
                idempotency_key=idempotency_key,
            )
            if holder is None:
                # Raced with a delete between the insert and this read.
                # The key is free again; the loop retries once.
                continue
            if attempt == 1 and _is_expired(holder):
                await self.repo.delete_by_id(record_id=holder.id)
                continue

            self._assert_same_call(
                record=holder,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                arguments_hash=arguments_hash,
            )
            if holder.response_json is None:
                raise IdempotencyKeyInFlightError(
                    f"Idempotency key {idempotency_key!r} is currently being "
                    "executed by another call. Retry shortly."
                )
            return Replay(
                hit=CacheHit(
                    response=dict(holder.response_json),
                    stored_at=holder.created_at,
                )
            )

        raise IdempotencyKeyInFlightError(
            f"Idempotency key {idempotency_key!r} could not be claimed; "
            "another call is contending for it. Retry shortly."
        )

    async def complete(
        self,
        claim: Claim,
        *,
        response: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> None:
        """Attach this call's response to its claim, making it replayable.

        An UPDATE by id on a row we own, so it cannot lose a race."""
        expires_at = (
            datetime.now(UTC) + ttl if ttl is not None else None
        )
        await self.repo.complete_claim(
            record_id=claim.record_id,
            response_json=response,
            expires_at=expires_at,
        )

    async def release(self, claim: Claim) -> None:
        """Drop an unfinished claim so a retry can re-execute."""
        await self.repo.delete_by_id(record_id=claim.record_id)

    def _assert_same_call(
        self,
        *,
        record: IdempotencyRecord,
        tool_name: str,
        idempotency_key: str,
        arguments_hash: str,
    ) -> None:
        if record.arguments_hash != arguments_hash:
            raise IdempotencyKeyReusedError(
                f"Idempotency key {idempotency_key!r} was previously used for "
                f"tool {record.tool_name!r} with different arguments. Pick a "
                "fresh key or send the exact same arguments."
            )
        if record.tool_name != tool_name:
            raise IdempotencyKeyReusedError(
                f"Idempotency key {idempotency_key!r} was previously used for "
                f"a different tool ({record.tool_name!r})."
            )


__all__ = [
    "CacheHit",
    "Claim",
    "IdempotencyKeyInFlightError",
    "IdempotencyKeyReusedError",
    "IdempotencyService",
    "Replay",
    "_hash_arguments",
]
