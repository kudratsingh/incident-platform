"""R2-51 — the caller does not get to choose what lands in `audit_logs`.

`X-Request-ID` is copied onto every audit row this request writes. The
column is `String(255)`, and every audit writer on the MCP path is
savepoint-wrapped and silent on failure, so a header the column cannot
hold does not fail the request — it deletes the record of it. That makes
the header an audit-suppression switch for anyone who can reach the load
balancer.

These tests pin the two halves of the fix: the middleware refuses to put
an unusable id into circulation, and the bound it enforces is provably
inside the column it has to fit.
"""

import re
import uuid

import pytest
from app.core.middleware import (
    CORRELATION_ID_MAX_LENGTH,
    RequestContextMiddleware,
    sanitise_correlation_id,
)
from app.models.audit import REQUEST_ID_MAX_LENGTH, AuditLog
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

_UUID_RE = re.compile(r"\A[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")


def _is_generated(value: str) -> bool:
    """A substituted id is a fresh UUID — not a trimmed version of what
    the caller sent, which would still be attacker-chosen."""
    return bool(_UUID_RE.fullmatch(value))


@pytest.mark.parametrize(
    "supplied",
    [
        "550e8400-e29b-41d4-a716-446655440000",  # UUID
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # traceparent
        "1-5759e988-bd862e3fe1be46a994272793",  # X-Ray
        "abc123",
        "a" * CORRELATION_ID_MAX_LENGTH,  # exactly at the bound
    ],
)
def test_usable_ids_are_passed_through_unchanged(supplied: str) -> None:
    assert sanitise_correlation_id(supplied, header="X-Request-ID") == supplied


@pytest.mark.parametrize(
    ("supplied", "why"),
    [
        ("x" * 4096, "4KB header — the R2-51 trigger"),
        ("x" * (CORRELATION_ID_MAX_LENGTH + 1), "one over the bound"),
        ("x" * (REQUEST_ID_MAX_LENGTH + 1), "one over the column"),
        ("abc\ndef", "newline — log injection"),
        ("abc\x00def", "NUL byte"),
        ("abc\tdef", "tab"),
        ("id with spaces", "space"),
        ("id;DROP TABLE audit_logs", "punctuation outside the charset"),
        ("", "empty"),
        (None, "absent"),
    ],
)
def test_unusable_ids_are_replaced_with_a_fresh_uuid(
    supplied: str | None, why: str
) -> None:
    got = sanitise_correlation_id(supplied, header="X-Request-ID")
    assert _is_generated(got), f"{why}: expected a generated id, got {got!r}"
    assert got != supplied


def test_over_long_ids_are_not_truncated_into_a_shared_prefix() -> None:
    """Truncation would let one caller mint any number of distinct
    headers that all collapse onto the same `request_id`, so their
    actions would share an audit correlation id. Substitution keeps every
    call distinguishable."""
    a = sanitise_correlation_id("z" * 4096, header="X-Request-ID")
    b = sanitise_correlation_id("z" * 4096 + "different", header="X-Request-ID")
    assert a != b


def test_correlation_id_bound_fits_the_audit_column() -> None:
    """The tripwire. The middleware's bound is only meaningful while it is
    inside the column the value has to reach; narrowing
    `audit_logs.request_id` without revisiting the middleware would
    silently re-open the suppression path."""
    column_length = AuditLog.__table__.c.request_id.type.length
    assert column_length == REQUEST_ID_MAX_LENGTH
    assert CORRELATION_ID_MAX_LENGTH <= REQUEST_ID_MAX_LENGTH


def _app() -> Starlette:
    async def _echo(request):  # type: ignore[no-untyped-def]
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/echo", _echo)])
    app.add_middleware(RequestContextMiddleware)
    return app


async def _get(headers: dict[str, str]):  # type: ignore[no-untyped-def]
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as ac:
        return await ac.get("/echo", headers=headers)


async def test_middleware_echoes_the_id_actually_in_force() -> None:
    """A caller whose header was refused has to be able to see it: the
    response carries the id the request was really recorded under."""
    resp = await _get({"X-Request-ID": "x" * 4096})
    assert resp.status_code == 200
    assert _is_generated(resp.headers["X-Request-ID"])
    # Trace mirrors the request id when the caller sent no trace header.
    assert resp.headers["X-Trace-ID"] == resp.headers["X-Request-ID"]


async def test_middleware_keeps_a_usable_id() -> None:
    supplied = str(uuid.uuid4())
    resp = await _get({"X-Request-ID": supplied})
    assert resp.headers["X-Request-ID"] == supplied


async def test_middleware_validates_the_trace_header_on_its_own_terms() -> None:
    supplied = str(uuid.uuid4())
    resp = await _get({"X-Request-ID": supplied, "X-Trace-ID": "bad\nvalue"})
    assert resp.headers["X-Request-ID"] == supplied
    assert _is_generated(resp.headers["X-Trace-ID"])
