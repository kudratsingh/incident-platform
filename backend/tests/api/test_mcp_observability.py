"""WO-R2-60 — the agent-facing process has to emit the evidence."""

from __future__ import annotations

import importlib
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.core import observability
from app.core.logging import JSONFormatter
from app.core.observability import (
    MCP_SERVICE_NAME,
    bootstrap_process_observability,
    instrumented_libraries,
)
from app.dependencies import get_db, get_redis
from app.mcp import standalone
from app.mcp.standalone import create_mcp_app
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    async def get(self, key: str) -> bytes | str | None:
        return None


@pytest.fixture
def fresh_bootstrap(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Let the bootstrap run again in this test."""
    original_handlers = list(logging.root.handlers)
    original_level = logging.root.level
    monkeypatch.setattr(observability, "_bootstrapped", False)
    yield
    logging.root.handlers = original_handlers
    logging.root.setLevel(original_level)


def test_bootstrap_installs_the_json_formatter_and_lets_info_through(
    fresh_bootstrap: None,
) -> None:
    """Both halves of the logging finding: the formatter, and the level."""
    logging.root.handlers = []
    logging.root.setLevel(logging.CRITICAL)

    bootstrap_process_observability(service_name=MCP_SERVICE_NAME)

    assert logging.root.handlers, "no handler installed"
    assert all(
        isinstance(h.formatter, JSONFormatter) for h in logging.root.handlers
    ), "the MCP process would emit unstructured logs"
    assert logging.root.isEnabledFor(logging.INFO), (
        "INFO is dropped — the level was never set from LOG_LEVEL"
    )


def test_bootstrap_emits_a_structured_info_line(
    fresh_bootstrap: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read it off the stream rather than through `caplog`: the bootstrap replaces the root
    handlers, which removes pytest's capture handler — and going to the real stream is
    what proves the line is JSON at all."""
    bootstrap_process_observability(service_name=MCP_SERVICE_NAME)

    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{")
    ]
    emitted = [
        entry for entry in lines if entry.get("message") == "observability bootstrapped"
    ]
    assert emitted, "the bootstrap emitted no INFO line"
    assert emitted[0]["level"] == "INFO"
    assert emitted[0]["service_name"] == MCP_SERVICE_NAME


def test_bootstrap_installs_a_tracer_provider(fresh_bootstrap: None) -> None:
    bootstrap_process_observability(service_name=MCP_SERVICE_NAME)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "no real TracerProvider installed — every span is a no-op"
    )


def test_the_standalone_entrypoint_runs_the_bootstrap_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding itself, stated structurally."""
    calls: list[str] = []

    def _spy(*, service_name: str, settings: Any = None) -> None:
        calls.append(service_name)

    monkeypatch.setattr(
        observability, "bootstrap_process_observability", _spy
    )
    importlib.reload(standalone)

    assert calls == [MCP_SERVICE_NAME], (
        "the MCP entrypoint did not bootstrap observability"
    )

    # Leave the module as the rest of the suite expects to find it.
    monkeypatch.undo()
    importlib.reload(standalone)


def test_the_mcp_process_instruments_all_three_libraries() -> None:
    """The check that would have caught this: FastAPI, Redis and SQLAlchemy all
    instrumented in *this* process, not just in the API's."""
    app = create_mcp_app()
    live = instrumented_libraries(app)
    assert live == {"fastapi": True, "redis": True, "sqlalchemy": True}, live


async def test_one_mcp_request_produces_a_span(
    db_session: AsyncSession, default_tenant: Any
) -> None:
    """End to end: a real `POST /mcp` yields a server span on the exporter."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        pytest.fail(
            "no real TracerProvider in this process — the bootstrap did not run"
        )

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield _RedisStub()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        resp = await ac.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "initialize",
                "params": {},
            },
        )
    assert resp.status_code == 200

    spans = exporter.get_finished_spans()
    assert spans, "the MCP surface exported no spans for a served request"
    assert any("/mcp" in (s.name or "") for s in spans), (
        f"no span for the /mcp route; got {[s.name for s in spans]}"
    )
