"""
Alerts — create + optionally push via HMAC-signed webhook.

Signing:
  - Body is `json.dumps(payload, sort_keys=True, separators=(",", ":"))`
    so both sides produce byte-identical strings.
  - `X-Alert-Signature: sha256=<hex hmac>` header carries the signature.
  - `X-Alert-Timestamp: <unix ms>` optionally lets consumers reject
    old replayed deliveries (they do the check themselves; we just
    include it).

Fail-open policy: webhook errors are logged but never bubble. The
persisted row is the source of truth — a receiver missing a delivery
can catch up by polling `list_active_alerts`.
"""

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
from app.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger, request_id_var
from app.models.alert import ALLOWED_SEVERITIES, Alert
from app.repositories.alert import AlertRepository

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
        # Fire-and-log webhook. Persistent record already committed above.
        await _maybe_emit_webhook(alert)
        return alert


def sign_body(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex signature over the exact bytes we POST. Exposed
    for tests + for the agent-side verifier."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _maybe_emit_webhook(alert: Alert) -> None:
    """Push the alert to the configured webhook, if any. Never raises —
    delivery failures are the receiver's problem (they can poll)."""
    settings = get_settings()
    url = settings.alert_webhook_url
    secret = settings.alert_webhook_secret
    if not url or not secret:
        return

    payload = {
        "alert_id": str(alert.id),
        "tenant_id": str(alert.tenant_id),
        "severity": alert.severity,
        "source": alert.source,
        "title": alert.title,
        "description": alert.description,
        "fired_at": alert.fired_at.isoformat() if alert.fired_at else None,
        "extra_data": alert.extra_data or {},
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    headers = {
        "Content-Type": "application/json",
        "X-Alert-Signature": sign_body(secret, body),
        "X-Alert-Timestamp": str(int(time.time() * 1000)),
    }

    try:
        async with httpx.AsyncClient(
            timeout=settings.alert_webhook_timeout_seconds
        ) as client:
            resp = await client.post(url, content=body, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "alert webhook non-2xx",
                extra={
                    "alert_id": str(alert.id),
                    "status": resp.status_code,
                    "url": url,
                },
            )
    except Exception as exc:
        # Fail open — receiver can catch up via list_active_alerts poll.
        logger.warning(
            "alert webhook delivery failed",
            extra={"alert_id": str(alert.id), "error": str(exc)},
        )


__all__ = [
    "AlertService",
    "AlertValidationError",
    "sign_body",
]
