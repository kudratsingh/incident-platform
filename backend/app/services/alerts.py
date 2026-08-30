"""
Alerts — create, commit, then push via HMAC-signed webhook.

**Delivery happens after the commit (WO-R2-70).** The POST used to be
awaited from inside the caller's still-open transaction, which made the
webhook an announcement about a row nobody else could see yet: any
rollback afterwards — a later statement failing, a request erroring out,
the dedup `IntegrityError` landing on a sibling write — erased the alert
while the commander had already been told it existed and had already
started acting on an `alert_id` that would never resolve. The emission is
therefore registered on the session's post-commit queue
(`utils/post_commit.py`, the same mechanism cache invalidation uses) and
runs only once the transaction is durable. On rollback the queue dies
with the session and nothing is delivered, which is exactly right.

Whoever owns `session.begin()` owns the drain: `get_db` does it for the
API and MCP surfaces, and the SLO evaluation loop does it for itself.

Signing:
  - Body is `json.dumps(payload, sort_keys=True, separators=(",", ":"))`
    so both sides produce byte-identical strings.
  - `X-Alert-Timestamp: <unix ms>` and `X-Alert-Nonce: <hex>` identify
    the delivery.
  - The signed material is **`{timestamp}.{nonce}.{body}`**, not the body
    alone, and `X-Alert-Signature: sha256=<hex hmac>` carries the result.

That composition is the fix for the second half of WO-R2-70. Signing only
the body left the timestamp unauthenticated, so the replay rejection this
docstring promised consumers was defeated by editing one header: capture a
delivery, restamp `X-Alert-Timestamp` to now, and the signature still
verifies because it never covered the header. A receiver's skew check is
only as good as what the signature binds. The nonce gives the receiver a
second, stronger option than skew alone — remembering nonces for the
length of its accepted window makes a replay detectable even inside it.

Receivers should: recompute the HMAC over `{timestamp}.{nonce}.{body}`,
compare in constant time, reject a timestamp outside their skew window,
and reject a nonce they have already seen inside it.

Fail-open policy: webhook errors are logged but never bubble. The
persisted row is the source of truth — a receiver missing a delivery
can catch up by polling `list_active_alerts`.
"""

import hashlib
import hmac
import json
import time
import uuid
from functools import partial
from typing import Any

import httpx
from app.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger, request_id_var
from app.models.alert import ALLOWED_SEVERITIES, Alert
from app.repositories.alert import AlertRepository
from app.utils.post_commit import register_post_commit

logger = get_logger(__name__)


class AlertValidationError(AppError):
    status_code = 400
    error_code = "alert_invalid"


class AlertService:
    def __init__(self, alert_repo: AlertRepository) -> None:
        self.alert_repo = alert_repo

    async def create_alert(
        self,
        *,
        tenant_id: uuid.UUID,
        severity: str,
        source: str,
        title: str,
        description: str | None = None,
        extra_data: dict[str, Any] | None = None,
        dedup_key: str | None = None,
    ) -> Alert:
        """Persist an alert and push it to the webhook.

        `dedup_key` is optional and, when given, is enforced by the unique
        constraint on `(tenant_id, dedup_key)`: a second alert with the same
        key raises `IntegrityError` out of the flush inside `create` — before
        the webhook fires, which is the ordering that matters, since a
        suppressed alert must not still be delivered.

        The conflict is deliberately raised rather than swallowed here. A
        producer that de-duplicates has to decide what a conflict *means* to
        it (for the SLO loop it means "this window is already alerted, carry
        on"), and a service that quietly returned someone else's row would
        make that decision invisible at the call site. Callers that pass a
        key are expected to catch it; callers that pass none cannot hit it,
        because NULL keys do not collide.
        """
        if severity not in ALLOWED_SEVERITIES:
            raise AlertValidationError(
                f"Unknown severity {severity!r}; allowed: {sorted(ALLOWED_SEVERITIES)}"
            )
        if not title:
            raise AlertValidationError("Alert title must not be empty")

        alert = await self.alert_repo.create(
            tenant_id=tenant_id,
            severity=severity,
            source=source,
            title=title,
            description=description,
            extra_data=extra_data,
            request_id=request_id_var.get("") or None,
            dedup_key=dedup_key,
        )
        logger.info(
            "alert created",
            extra={
                "alert_id": str(alert.id),
                "tenant_id": str(tenant_id),
                "severity": severity,
                "source": source,
            },
        )
        # Deliver after the commit, never before it (see the module
        # docstring). The payload is snapshotted here, while the row's
        # attributes are loaded and the transaction is still open, so the
        # hook closes over plain data and never touches an ORM instance on
        # a session that may since have been closed or expired.
        payload = _webhook_payload(alert)
        queued = register_post_commit(
            self.alert_repo.session, partial(deliver_webhook, payload)
        )
        if not queued:
            # No post-commit queue on this session — the session stand-ins
            # the unit suites pass to services. Emitting inline preserves
            # the old behaviour for them rather than silently dropping the
            # delivery, which would make an absent webhook look like a
            # passing test. A real AsyncSession always has one.
            logger.debug(
                "alert webhook emitted inline — session has no post-commit queue",
                extra={"alert_id": str(alert.id)},
            )
            await deliver_webhook(payload)
        return alert


def _webhook_payload(alert: Alert) -> dict[str, Any]:
    """The delivery body, as data, decoupled from the ORM instance."""
    return {
        "alert_id": str(alert.id),
        "tenant_id": str(alert.tenant_id),
        "severity": alert.severity,
        "source": alert.source,
        "title": alert.title,
        "description": alert.description,
        "fired_at": alert.fired_at.isoformat() if alert.fired_at else None,
        "extra_data": alert.extra_data or {},
    }


def signed_material(timestamp: str, nonce: str, body: bytes) -> bytes:
    """The exact bytes the signature covers: `{timestamp}.{nonce}.{body}`.

    One function so the sender, the tests and the receiving side cannot
    each compose it slightly differently — a signature scheme where the
    two ends disagree about what is being signed is a signature scheme
    that verifies nothing. The separators are unambiguous: both prefixes
    are fixed-alphabet (digits, hex) and contain no `.` themselves.
    """
    return f"{timestamp}.{nonce}.".encode() + body


def sign_delivery(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """HMAC-SHA256 over the timestamp, nonce and body we POST.

    Replaces the old `sign_body`, which covered the body alone and left
    `X-Alert-Timestamp` outside the signature — so the replay window the
    module promises was defeated by restamping one header (WO-R2-70).
    Exposed for tests and for the agent-side verifier.
    """
    digest = hmac.new(
        secret.encode(), signed_material(timestamp, nonce, body), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


async def deliver_webhook(payload: dict[str, Any]) -> None:
    """Push one alert to the configured webhook, if any. Never raises —
    delivery failures are the receiver's problem (they can poll).

    Runs from the post-commit queue, which swallows and logs exceptions;
    raising here would be caught there anyway, and the alert row is
    already durable by the time this is reached.
    """
    settings = get_settings()
    url = settings.alert_webhook_url
    secret = settings.alert_webhook_secret
    if not url or not secret:
        return

    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    timestamp = str(int(time.time() * 1000))
    # Per delivery, not per alert: a retry of the same alert is a distinct
    # delivery, and the receiver's replay cache is keyed on this.
    nonce = uuid.uuid4().hex

    headers = {
        "Content-Type": "application/json",
        "X-Alert-Signature": sign_delivery(secret, timestamp, nonce, body),
        "X-Alert-Timestamp": timestamp,
        "X-Alert-Nonce": nonce,
    }
    alert_id = payload.get("alert_id")

    try:
        async with httpx.AsyncClient(
            timeout=settings.alert_webhook_timeout_seconds
        ) as client:
            resp = await client.post(url, content=body, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "alert webhook non-2xx",
                extra={
                    "alert_id": alert_id,
                    "status": resp.status_code,
                    "url": url,
                },
            )
    except Exception as exc:
        # Fail open — receiver can catch up via list_active_alerts poll.
        logger.warning(
            "alert webhook delivery failed",
            extra={"alert_id": alert_id, "error": str(exc)},
        )


__all__ = [
    "AlertService",
    "AlertValidationError",
    "deliver_webhook",
    "sign_delivery",
    "signed_material",
]
