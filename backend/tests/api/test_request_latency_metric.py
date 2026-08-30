"""What `RequestContextMiddleware` puts in the `RequestLatency` Path dimension.

The dimension used to be `request.url.path` — the raw URL, job/tenant/user
UUIDs and all. CloudWatch bills per distinct dimension *combination*, so every
resource id minted a new custom metric that would be paid for and then never
read. Because `BaseHTTPMiddleware` wraps the router, unmatched URLs were
measured too, which put the cardinality under the control of anyone who could
send the service a request.

These tests pin the shape of the dimension, not the value of the latency.
"""

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
    """Let any fire-and-forget emit task run before assertions.

    Harmless once emission is a synchronous enqueue; required while it is a
    `create_task`, so the same test body is meaningful before and after.
    """
    for _ in range(3):
        await asyncio.sleep(0)


def _paths(emitted: list[dict[str, Any]]) -> list[str]:
    return [
        c["dimensions"].get("Path")
        for c in emitted
        if c["name"] == "RequestLatency"
    ]


# ---------------------------------------------------------------------------
# The core cardinality claim
# ---------------------------------------------------------------------------


async def test_two_job_ids_produce_one_path_dimension(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Two GETs for different job ids must collapse to a single dimension value.

    This is the whole finding. With the raw path it is two distinct billable
    metrics, and with a million jobs it is a million.
    """
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
    # flattening them, so the declared path of a route mounted under
    # `include_router(..., prefix="/api/v1")` is `/jobs/{job_id}`. See
    # `route_label`'s docstring; `test_route_labels_are_unique_per_route`
    # pins the property that makes the short form safe to use as an id.
    assert set(paths) == {"/jobs/{job_id}"}, (
        f"two job ids produced {len(set(paths))} distinct Path dimensions: "
        f"{sorted(set(paths))}"
    )


async def test_no_uuid_ever_reaches_the_path_dimension(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Stronger and route-agnostic: the id in the URL must not appear anywhere
    in the emitted dimensions. Catches a partial fix that templates one route
    and forgets its siblings."""
    job_id = "2f1c8a90-0000-4000-8000-0000000000ab"
    await client.get(f"/api/v1/jobs/{job_id}", headers=auth_headers)
    await _settle()

    for call in emitted:
        for value in call["dimensions"].values():
            assert job_id not in value, f"resource id leaked into a dimension: {call}"


# ---------------------------------------------------------------------------
# Unmatched URLs — the attacker-controlled half
# ---------------------------------------------------------------------------


async def test_unmatched_path_emits_the_constant_not_the_url(
    client: AsyncClient, emitted: list[dict[str, Any]]
) -> None:
    """A 404 on a URL that matches no route emits `unmatched`.

    `BaseHTTPMiddleware` wraps the router, so a scanner walking random URLs
    reached this code with a fresh path every time. Nothing rate-limits
    404s, so this was the cheapest way to run up a CloudWatch bill.
    """
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
# The dimension that is supposed to vary still varies
# ---------------------------------------------------------------------------


async def test_status_code_dimension_is_still_per_status(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Collapsing Path must not collapse StatusCode — a bounded, useful
    dimension. Without this, "emit a constant for everything" would pass
    every other test in this file."""
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
# The allow-list actually covers what the app serves
# ---------------------------------------------------------------------------
#
# The guarantee that emission cannot block the request is pinned one layer
# down, in tests/unit/test_metrics_queue.py: `emit_gauge` does no I/O at all,
# so there is no delivery path for the request to wait on. Asserting it here
# by monkeypatching `emit_gauge` itself would only test the mock.


async def test_every_served_route_is_in_the_allow_list(
    client: AsyncClient, auth_headers: dict[str, str], emitted: list[dict[str, Any]]
) -> None:
    """Real traffic must never be bucketed as `other`.

    The tripwire for FastAPI changing its route-tree shape. `collect_route_labels`
    has to recurse into `original_router` to see the API at all; if a future
    version nests differently and the traversal silently finds nothing, the
    allow-list stops matching what `route_label` reports at request time and
    *every* request is billed as `other`. That is a total loss of the metric
    with no error anywhere, so it gets an explicit test.
    """
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
    """No two distinct URLs may collapse to the same Path label.

    `route_label` uses the route's *declared* path, which for anything mounted
    via `include_router(prefix=...)` is router-relative (`/jobs/{job_id}`, not
    `/api/v1/jobs/{job_id}`). That is only safe as an identifier while it stays
    unique across the whole app; two routers each declaring `/items/{id}` under
    different prefixes would silently merge into one metric.

    Reconstructing the full path in production would mean reading FastAPI's
    private `_IncludedRouter.include_context.prefix`, which we deliberately do
    not depend on. Instead the risk is pinned here: this test may use private
    attributes, because if FastAPI changes them the test fails loudly at CI
    rather than production quietly mismeasuring.
    """
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
# A failing emit must not reach the client
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
