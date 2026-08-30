"""Unit tests for AlertService — persistence, HMAC signing, fail-open."""

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from app.config import Settings
from app.models.alert import Alert
from app.models.base import Base
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.repositories.alert import AlertRepository
from app.services.alerts import (
    AlertService,
    AlertValidationError,
    sign_delivery,
    signed_material,
)
from app.utils.post_commit import run_post_commit
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


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


async def test_hmac_signature_covers_timestamp_nonce_and_body() -> None:
    secret = "s3cr3t"
    body = json.dumps({"hello": "world"}, sort_keys=True).encode()
    expected = "sha256=" + hmac.new(
        secret.encode(), b"1730000000000.abc123." + body, hashlib.sha256
    ).hexdigest()
    assert sign_delivery(secret, "1730000000000", "abc123", body) == expected
    assert signed_material("1730000000000", "abc123", body) == (
        b"1730000000000.abc123." + body
    )


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
            posted["ts"] = headers["X-Alert-Timestamp"]
            posted["nonce"] = headers["X-Alert-Nonce"]
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
    # The signature we produced must verify with the same secret, over the
    # timestamp and nonce as well as the body.
    assert posted["sig"] == sign_delivery(
        "s3cr3t",
        posted["ts"],  # type: ignore[arg-type]
        posted["nonce"],  # type: ignore[arg-type]
        posted["body"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Commit before delivery (WO-R2-70)
#
# The POST used to be awaited from inside the caller's still-open
# transaction, so the commander was told an alert existed while the row was
# invisible to every other connection — and any rollback afterwards erased
# it, leaving the agent acting on an alert_id that would never resolve.
# Delivery is now queued on the session's post-commit hook and runs only once
# the transaction is durable.
#
# These use a real AsyncSession because the mechanism under test *is* the
# session: `session.info` is where the queue lives, and a mock has no queue.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def alert_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    slug="default",
                    name="Default Tenant",
                    is_active=True,
                )
            )
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


class _Recorder:
    """Records POSTs. One instance per test, patched over httpx.AsyncClient."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def __call__(self, *_args: Any, **_kwargs: Any) -> "_Recorder":
        return self

    async def __aenter__(self) -> "_Recorder":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def post(self, url: str, content: bytes, headers: dict[str, str]) -> Any:
        self.posts.append({"url": url, "body": content, "headers": headers})
        return MagicMock(status_code=200)


def _webhook_on() -> Settings:
    return Settings(
        alert_webhook_url="http://receiver.invalid/hook",
        alert_webhook_secret="s3cr3t",
    )


async def test_webhook_is_not_delivered_from_inside_the_transaction(
    alert_session: AsyncSession,
) -> None:
    """The core of the finding: nothing may reach the receiver while the row
    it describes is still uncommitted."""
    recorder = _Recorder()
    svc = AlertService(AlertRepository(alert_session))

    with patch("app.services.alerts.get_settings", return_value=_webhook_on()), patch(
        "app.services.alerts.httpx.AsyncClient", recorder
    ):
        async with alert_session.begin():
            await svc.create_alert(
                tenant_id=DEFAULT_TENANT_ID,
                severity="critical",
                source="slo:job_completion_rate",
                title="SLO fast burn",
            )
            # Inside the transaction — at HEAD this list already held one POST.
            assert recorder.posts == []

        # Committed, but the session owner has not drained yet.
        assert recorder.posts == []
        await run_post_commit(alert_session)

    assert len(recorder.posts) == 1


async def test_rolled_back_alert_is_never_delivered(
    alert_session: AsyncSession,
) -> None:
    """A later failure inside the same transaction erases the row. At HEAD the
    commander had already been told about it."""
    recorder = _Recorder()
    svc = AlertService(AlertRepository(alert_session))

    with patch("app.services.alerts.get_settings", return_value=_webhook_on()), patch(
        "app.services.alerts.httpx.AsyncClient", recorder
    ):
        with pytest.raises(RuntimeError):
            # The shape `get_db` gives every request: drain only if the
            # begin-block exits cleanly.
            async with alert_session.begin():
                await svc.create_alert(
                    tenant_id=DEFAULT_TENANT_ID,
                    severity="critical",
                    source="slo:job_completion_rate",
                    title="SLO fast burn",
                )
                raise RuntimeError("a later statement fails")
            await run_post_commit(alert_session)  # pragma: no cover

        # Belt and braces: even a session owner that drains anyway delivers
        # nothing, because the rollback cleared the queue.
        await run_post_commit(alert_session)

    assert recorder.posts == []
    remaining = (await alert_session.execute(select(Alert))).scalars().all()
    assert list(remaining) == []


async def test_replaying_a_delivery_with_a_new_timestamp_fails_verification(
    alert_session: AsyncSession,
) -> None:
    """The replay defence the module docstring promises, actually bound.

    At HEAD only the body was signed, so an attacker restamped
    `X-Alert-Timestamp` to now and the signature still verified — the skew
    check the receiver performs was checking an unauthenticated header.
    """
    recorder = _Recorder()
    svc = AlertService(AlertRepository(alert_session))

    with patch("app.services.alerts.get_settings", return_value=_webhook_on()), patch(
        "app.services.alerts.httpx.AsyncClient", recorder
    ):
        async with alert_session.begin():
            await svc.create_alert(
                tenant_id=DEFAULT_TENANT_ID,
                severity="warning",
                source="dlq:threshold",
                title="DLQ backlog growing",
            )
        await run_post_commit(alert_session)

    delivered = recorder.posts[0]
    headers, body = delivered["headers"], delivered["body"]
    # As delivered, it verifies.
    assert headers["X-Alert-Signature"] == sign_delivery(
        "s3cr3t", headers["X-Alert-Timestamp"], headers["X-Alert-Nonce"], body
    )
    # Replayed an hour later with a fresh timestamp, it does not.
    replayed_ts = str(int(headers["X-Alert-Timestamp"]) + 3_600_000)
    assert headers["X-Alert-Signature"] != sign_delivery(
        "s3cr3t", replayed_ts, headers["X-Alert-Nonce"], body
    )


async def test_each_delivery_carries_a_fresh_nonce(
    alert_session: AsyncSession,
) -> None:
    """The receiver's replay cache is keyed on this, so it has to be unique
    per delivery rather than per alert."""
    recorder = _Recorder()
    svc = AlertService(AlertRepository(alert_session))

    with patch("app.services.alerts.get_settings", return_value=_webhook_on()), patch(
        "app.services.alerts.httpx.AsyncClient", recorder
    ):
        for i in range(2):
            async with alert_session.begin():
                await svc.create_alert(
                    tenant_id=DEFAULT_TENANT_ID,
                    severity="info",
                    source="test",
                    title=f"Alert {i}",
                )
            await run_post_commit(alert_session)

    nonces = {post["headers"]["X-Alert-Nonce"] for post in recorder.posts}
    assert len(nonces) == 2
