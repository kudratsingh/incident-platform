"""
Alerts — create, commit, then push via HMAC-signed webhook.

**Delivery happens after the commit (WO-R2-70)**, on the session's post-commit queue, so a
rollback delivers nothing and whoever owns `session.begin()` owns the drain. The signature
covers `{timestamp}.{nonce}.{body}`, not the body alone, which left `X-Alert-Timestamp`
forgeable. Webhook errors never bubble — the row is the truth, pollable by
`list_active_alerts`.
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
    """The alert was rejected before it was written — bad severity, no title."""

    status_code = 400
    error_code = "alert_invalid"


class AlertService:
    """Records alerts and, once the transaction commits, pushes them to the
    webhook."""

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

        `dedup_key`, when given, is enforced by the unique constraint on `(tenant_id,
        dedup_key)`: a second alert raises `IntegrityError` out of the flush, before the
        webhook fires. Raised, not swallowed — the producer decides what a conflict means.
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
        # Deliver after the commit (see the module docstring). The payload is
        # snapshotted now so the hook closes over data, not an ORM instance.
        payload = _webhook_payload(alert)
        queued = register_post_commit(
            self.alert_repo.session, partial(deliver_webhook, payload)
        )
        if not queued:
            # No post-commit queue — a unit-suite session stand-in. Emit inline
            # rather than drop it, or an absent webhook looks like a pass.
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

    One function so sender, tests and receiver cannot compose it differently; both
    prefixes are fixed-alphabet and contain no `.`.
    """
    return f"{timestamp}.{nonce}.".encode() + body


def sign_delivery(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """HMAC-SHA256 over the timestamp, nonce and body we POST.

    Replaces `sign_body`, which covered the body alone and left `X-Alert-Timestamp`
    restampable (WO-R2-70). Exposed for tests and the agent-side verifier.
    """
    digest = hmac.new(
        secret.encode(), signed_material(timestamp, nonce, body), hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


async def deliver_webhook(payload: dict[str, Any]) -> None:
    """Push one alert to the configured webhook, if any. Never raises.

    Runs from the post-commit queue, which swallows and logs; the row is already durable.
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
