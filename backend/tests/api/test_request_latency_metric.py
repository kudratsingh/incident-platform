"""What `RequestContextMiddleware` puts in the `RequestLatency` Path dimension."""

import asyncio
from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core import metrics, middleware
from app.main import create_app
from httpx import AsyncClient


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every gauge the middleware emits, as {name, value, dimensions}."""
    calls: list[dict[str, Any]] = []

    async def _capture(
        metric_name: str,
        value: float,
        unit: str = "Count",
        dimensions: dict[str, str] | None = None,
    ) -> None:
        calls.append(
            {"name": metric_name, "value": value, "dimensions": dict(dimensions or {})}
        )

    monkeypatch.setattr(middleware.metrics, "emit_gauge", _capture)
    return calls


async def _settle() -> None:
    """Let any fire-and-forget emit task run before assertions."""
    for _ in range(3):
        await asyncio.sleep(0)


def _paths(emitted: list[dict[str, Any]]) -> list[str]:
    return [
        c["dimensions"].get("Path")
        for c in emitted
        if c["name"] == "RequestLatency"
    ]


# ---------------------------------------------------------------------------


async def test_two_job_ids_produce_one_path_dimension(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Two GETs for different job ids must collapse to a single dimension value."""
    await client.get(
        "/api/v1/jobs/2f1c8a90-0000-4000-8000-000000000001", headers=auth_headers
    )
    await client.get(
        "/api/v1/jobs/2f1c8a90-0000-4000-8000-000000000002", headers=auth_headers
    )
    await _settle()

    paths = _paths(emitted)
    assert len(paths) == 2, f"expected two emissions, got {paths}"
    # Router-relative by design — FastAPI nests included routers rather than
    assert set(paths) == {"/jobs/{job_id}"}, (
        f"two job ids produced {len(set(paths))} distinct Path dimensions: "
        f"{sorted(set(paths))}"
    )


async def test_no_uuid_ever_reaches_the_path_dimension(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Stronger and route-agnostic: the id in the URL must not appear anywhere in the
    emitted dimensions."""
    job_id = "2f1c8a90-0000-4000-8000-0000000000ab"
    await client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    await _settle()

    for call in emitted:
        for value in call["dimensions"].values():
            assert job_id not in value, f"resource id leaked into a dimension: {call}"


# ---------------------------------------------------------------------------


async def test_unmatched_path_emits_the_constant_not_the_url(
    client: AsyncClient, emitted: list[dict[str, Any]]
) -> None:
    """A 404 on a URL that matches no route emits `unmatched`."""
    resp = await client.get("/api/v1/no-such-route/aaaa-bbbb-cccc")
    assert resp.status_code == 404

    await _settle()
    assert _paths(emitted) == ["unmatched"]


async def test_scanner_traffic_collapses_to_a_single_dimension(
    client: AsyncClient, emitted: list[dict[str, Any]]
) -> None:
    """Twenty distinct junk URLs, one dimension value."""
    for i in range(20):
        await client.get(f"/api/v1/{i}/{i * 7}/scan-{i}")
    await _settle()

    paths = _paths(emitted)
    assert len(paths) == 20
    assert set(paths) == {"unmatched"}


# ---------------------------------------------------------------------------


async def test_status_code_dimension_is_still_per_status(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Collapsing Path must not collapse StatusCode — a bounded, useful dimension."""
    await client.get(
        "/api/v1/jobs/00000000-0000-0000-0000-000000000000", headers=auth_headers
    )  # 404 from the handler, but a matched route
    await client.get("/api/v1/jobs", headers=auth_headers)  # 200

    await _settle()
    statuses = {
        c["dimensions"].get("StatusCode")
        for c in emitted
        if c["name"] == "RequestLatency"
    }
    assert statuses == {"404", "200"}


# ---------------------------------------------------------------------------


async def test_every_served_route_is_in_the_allow_list(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Real traffic must never be bucketed as `other`."""
    middleware.register_route_dimension(create_app())

    for path in ("/api/v1/jobs", "/api/v1/health", "/healthz"):
        await client.get(path, headers=auth_headers)
    await client.get(
        "/api/v1/jobs/2f1c8a90-0000-4000-8000-000000000003", headers=auth_headers
    )
    await _settle()

    paths = _paths(emitted)
    assert paths, "no RequestLatency emitted"
    for path in paths:
        assert path is not None
        survived = metrics._sanitise_dimensions({"Path": path})["Path"]
        assert survived == path, (
            f"served route {path!r} is not in the allow-list — it would be "
            f"reported as {survived!r}. Route traversal is probably broken."
        )


def test_route_labels_are_unique_per_route() -> None:
    """No two distinct URLs may collapse to the same Path label."""
    app = create_app()
    by_label: dict[str, set[str]] = {}

    def walk(routes: Sequence[Any], prefix: str = "", depth: int = 0) -> None:
        if depth > 8:
            return
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                context = getattr(route, "include_context", None)
                sub_prefix = getattr(context, "prefix", "") if context else ""
                walk(nested.routes, prefix + sub_prefix, depth + 1)
                continue
            path_format = getattr(route, "path_format", None)
            if isinstance(path_format, str) and path_format:
                by_label.setdefault(path_format, set()).add(prefix + path_format)
            sub_routes = getattr(route, "routes", None)
            if sub_routes:
                walk(sub_routes, prefix, depth + 1)

    walk(app.routes)
    assert by_label, "route traversal found nothing — FastAPI internals moved"

    collisions = {
        label: sorted(full) for label, full in by_label.items() if len(full) > 1
    }
    assert not collisions, (
        "these route labels are ambiguous — distinct URLs would share one "
        f"metric dimension: {collisions}"
    )


# ---------------------------------------------------------------------------


async def test_middleware_survives_a_failing_emit(
    client: AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising metrics path must not turn into a 500 on the request."""
    monkeypatch.setattr(
        middleware.metrics,
        "emit_gauge",
        AsyncMock(side_effect=RuntimeError("cloudwatch unreachable")),
    )
    resp = await client.get("/api/v1/jobs", headers=auth_headers)
    assert resp.status_code == 200
