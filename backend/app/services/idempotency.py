"""
Idempotency policy for Tier 1 actions.

The key is *claimed before the action runs*, not recorded after it
(R2-27). The old shape looked the key up and inserted it after
execution, both in the same READ COMMITTED transaction with nothing in
between, so two concurrent calls on one key both missed the cache and
both executed; the loser then died on `uq_idempotency_scope` with its
side effect already landed. Claiming first closes that window instead of
repairing it afterwards.

Three operations:
  - `acquire(principal, tool_name, key, arguments, ttl)` → an
    `Acquired` (this caller owns the key and must execute), a
    `Replay` (someone already answered for this key — send theirs), or
    `IdempotencyKeyReusedError` when the key is held for different
    arguments or a different tool.
  - `complete(claim, response)` — attach this call's response to the
    claim, making it replayable.
  - `release(claim)` — drop an unfinished claim so a retry can
    re-execute. Every path that does not complete must release: the MCP
    envelope deliberately commits the request transaction on a tool
    error so the audit row survives, and a claim left behind would
    commit with it and wedge the key for its whole TTL.

`lookup` remains for reading a key without taking it.

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
    """The key is held by a claim that has no response yet: another call
    is executing it right now.

    Reachable only if a claim outlived the transaction that took it —
    a release that could not be written, say — because claim and response
    otherwise commit together. Retryable, unlike
    `IdempotencyKeyReusedError`, so it gets its own code rather than
    borrowing that one's."""

    status_code = 409
    error_code = "idempotency_key_in_flight"


def _hash_arguments(arguments: dict[str, Any]) -> str:
    """Canonical-JSON SHA-256. Sorted keys + tight separators so the
    same dict hashes the same regardless of insertion order or
    whitespace.

    Published cross-repo contract — the commander's contract snapshot
    matrix pins these bytes. Any change to what this function hashes
    (input dict shape) or how (sort_keys, separators, default=,
    algorithm) is a coordinated version-sync, not a refactor. Full
    normalization table + coordination rule in
    docs/ADR/0010-idempotency-record-lifecycle.md § "Arguments-hash
    contract (cross-repo)".
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
        if _is_expired(record):
            # Evict rather than read past it. Treating the row as absent
            # while the UNIQUE index went on holding it was the whole of
            # finding #2: the caller re-executed and its insert then
            # collided with a record the lookup had just told it was not
            # there — after the action had taken effect.
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

        Returns a `Claim` when this caller won and must execute, or a
        `Replay` carrying the answer already recorded for the key.
        Raises `IdempotencyKeyReusedError` when the key is held for
        different arguments or a different tool, and
        `IdempotencyKeyInFlightError` when it is held by a claim with no
        response yet.

        At most one retry. An expired holder is evicted and the claim
        re-attempted; if that second attempt also loses, another caller
        took the key in between and owns it, so we defer to them rather
        than spinning.
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
        """Attach this call's response to its claim, making it
        replayable. An UPDATE by id on a row we own, so unlike the
        insert-after-execution it replaces, it cannot lose a race."""
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
