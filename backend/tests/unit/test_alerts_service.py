"""Unit tests for AlertService — persistence, HMAC signing, fail-open."""

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.models.alert import Alert
from app.services.alerts import (
    AlertService,
    AlertValidationError,
    sign_body,
)


def _make_repo() -> tuple[AlertService, AsyncMock]:
    repo = AsyncMock()
    svc = AlertService(repo)
    return svc, repo


def _fake_alert(**over: object) -> Alert:
    a = Alert()
    a.id = uuid.uuid4()
    a.tenant_id = over.get("tenant_id", uuid.uuid4())  # type: ignore[assignment]
    a.severity = over.get("severity", "warning")  # type: ignore[assignment]
    a.source = over.get("source", "slo:completion")  # type: ignore[assignment]
    a.title = over.get("title", "SLO burn")  # type: ignore[assignment]
    a.description = over.get("description")  # type: ignore[assignment]
    a.fired_at = None  # type: ignore[assignment]
    a.resolved_at = None
    a.extra_data = None
    return a


async def test_create_rejects_unknown_severity() -> None:
    svc, repo = _make_repo()
    with pytest.raises(AlertValidationError, match="Unknown severity"):
        await svc.create_alert(
            tenant_id=uuid.uuid4(),
            severity="urgent",  # not one of info/warning/critical
            source="whatever",
            title="hi",
        )
    repo.create.assert_not_called()


async def test_create_rejects_empty_title() -> None:
    svc, repo = _make_repo()
    with pytest.raises(AlertValidationError, match="title must not be empty"):
        await svc.create_alert(
            tenant_id=uuid.uuid4(),
            severity="info",
            source="whatever",
            title="",
        )
    repo.create.assert_not_called()


async def test_create_persists_row_and_skips_webhook_when_unset() -> None:
    svc, repo = _make_repo()
    repo.create.return_value = _fake_alert()
    with patch(
        "app.services.alerts.get_settings",
        return_value=Settings(alert_webhook_url=None, alert_webhook_secret=None),
    ):
        alert = await svc.create_alert(
            tenant_id=uuid.uuid4(),
            severity="warning",
            source="dlq:threshold",
            title="DLQ backlog growing",
        )
    assert alert is not None
    repo.create.assert_awaited_once()


async def test_hmac_signature_matches_expected() -> None:
    secret = "s3cr3t"
    body = json.dumps({"hello": "world"}, sort_keys=True).encode()
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    assert sign_body(secret, body) == expected


async def test_webhook_fails_open_on_network_error() -> None:
    """Delivery errors must not bubble — the persisted row is the source
    of truth; receivers catch up via list_active_alerts."""
    svc, repo = _make_repo()
    repo.create.return_value = _fake_alert()

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("connection reset")

        async def __aexit__(self, *args, **kwargs):
            return False

    with patch(
        "app.services.alerts.get_settings",
        return_value=Settings(
            alert_webhook_url="http://example.invalid/hook",
            alert_webhook_secret="s",
        ),
    ), patch("app.services.alerts.httpx.AsyncClient", return_value=_Boom()):
        # Must not raise
        await svc.create_alert(
            tenant_id=uuid.uuid4(),
            severity="critical",
            source="slo:completion",
            title="Boom",
        )


async def test_webhook_signs_and_posts_body() -> None:
    svc, repo = _make_repo()
    repo.create.return_value = _fake_alert()

    posted: dict[str, object] = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def post(self, url: str, content: bytes, headers: dict[str, str]):
            posted["url"] = url
            posted["body"] = content
            posted["sig"] = headers["X-Alert-Signature"]
            return MagicMock(status_code=200)

    with patch(
        "app.services.alerts.get_settings",
        return_value=Settings(
            alert_webhook_url="http://example.invalid/hook",
            alert_webhook_secret="s3cr3t",
        ),
    ), patch("app.services.alerts.httpx.AsyncClient", return_value=_Client()):
        await svc.create_alert(
            tenant_id=uuid.uuid4(),
            severity="info",
            source="test",
            title="Hello",
        )

    assert posted["url"] == "http://example.invalid/hook"
    # The signature we produced must verify with the same secret.
    assert posted["sig"] == sign_body("s3cr3t", posted["body"])  # type: ignore[arg-type]
