"""Every path the Locust load suite requests must resolve against the app.

The load suite had no test that ran it, and every path in it omitted the
`/api/v1` prefix all routers are mounted under. Against the documented
`--host http://localhost:8000` that means every simulated request 404s:
the run completes, the numbers look plausible, and what was measured is
the 404 handler. Nothing caught it because nothing invoked the suite.

This is that invocation. `locustfile.ROUTES` is the single place the
tasks get their paths from, and each entry is sent at the real app here —
so a dropped prefix, a renamed route, or a router remounted somewhere
else fails this test instead of silently voiding a load run.

Asserting "not 404/405" rather than a specific status on purpose: these
requests are unauthenticated, so what comes back is a 401/403/422 that
will change as the auth surface changes. The property under test is that
the *route exists and accepts that method* — which is exactly what the
bug broke.

The locustfile is read in a child interpreter, for the reason spelled out
in test_locustfile_job_types.py: importing `locust` runs gevent's
`monkey.patch_all()`, which would irreversibly patch socket/ssl/threading
in this pytest process and destabilise the asyncio API tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

_LOCUSTFILE = Path(__file__).resolve().parents[1] / "load" / "locustfile.py"

_LOADER = """\
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("locustfile_under_test", sys.argv[1])
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps({
    "prefix": module.API_PREFIX,
    "routes": {name: list(pair) for name, pair in module.ROUTES.items()},
    # The URLs the tasks actually build, not just the templates they
    # were declared with — so a `url()` that dropped the prefix on the
    # way out would still be caught.
    "urls": {name: module.url(name, job_id="00000000-0000-0000-0000-000000000000")
             for name in module.ROUTES},
}))
"""


@pytest.fixture(scope="module")
def locust_routes() -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", _LOADER, str(_LOCUSTFILE)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_locust_paths_carry_the_api_prefix(locust_routes: dict) -> None:
    """The prefix is on every built URL, not merely defined somewhere."""
    prefix = locust_routes["prefix"]
    assert prefix == "/api/v1"
    unprefixed = sorted(
        name for name, built in locust_routes["urls"].items()
        if not built.startswith(prefix)
    )
    assert not unprefixed, f"routes built without {prefix}: {unprefixed}"


async def test_every_locust_path_resolves_against_the_real_app(
    client: AsyncClient,
    locust_routes: dict,
) -> None:
    missing = []
    for name, (method, template) in locust_routes["routes"].items():
        path = locust_routes["prefix"] + template.format(job_id=uuid.uuid4())
        resp = await client.request(method, path)
        if resp.status_code in (404, 405):
            missing.append(f"{name}: {method} {path} -> {resp.status_code}")
    assert not missing, (
        "the load suite requests paths the API does not serve; every "
        "simulated request against them would 404:\n  " + "\n  ".join(missing)
    )
