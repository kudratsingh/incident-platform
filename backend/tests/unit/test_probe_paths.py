"""The deployment probes must point at routes that exist, and at the right ones.

Three probes, three different questions (WO-R2-65):

  * ALB target group  -> `/healthz`        "can this task serve HTTP?"
  * ECS container     -> `/healthz/worker` "is this task worth keeping?"
  * operators         -> `/api/v1/health`  everything, and nothing automatic
                                            acts on it

Both infrastructure probes used to curl the deep check, which made a Redis
outage deregister every target and recycle every task. The paths live in
Terraform and the routes live in FastAPI, so nothing but a test keeps the two
halves agreeing — a renamed route would otherwise fail every health check in
production and pass every test here.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from app.main import create_app

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

LIVENESS_PATH = "/healthz"
WORKER_PROBE_PATH = "/healthz/worker"
DEEP_CHECK_PATH = "/api/v1/health"


def _infra(name: str) -> str:
    """The file's configuration, with `#` comment lines stripped.

    The comments explain at length which endpoint each probe must *not* use,
    so a naive substring search over the raw text would match the reasoning
    rather than the setting."""
    text = (_REPO_ROOT / "infra" / name).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _app_paths() -> set[str]:
    return {getattr(route, "path", "") for route in create_app().routes}


@pytest.mark.parametrize(
    "path", [LIVENESS_PATH, WORKER_PROBE_PATH, DEEP_CHECK_PATH]
)
def test_every_probed_path_is_a_real_route(path: str) -> None:
    assert path in _app_paths(), f"{path} is probed by infra but not served"


def test_alb_target_group_probes_the_shallow_liveness_endpoint() -> None:
    """A target group decides who gets traffic. It may not depend on Redis:
    every backend target shares one, so failing them all converts a degraded
    API into an unreachable one."""
    backend_tg = _infra("alb.tf").split('resource "aws_lb_target_group" "frontend"')[0]
    paths = re.findall(r'path\s*=\s*"([^"]+)"', backend_tg)

    assert paths == [LIVENESS_PATH], (
        f"backend target group probes {paths}, expected [{LIVENESS_PATH!r}]"
    )


def test_ecs_container_check_probes_the_worker_liveness_endpoint() -> None:
    """The container check is the one with restart authority, so it must see
    a dead worker (ADR 0009) and must not see a dependency outage."""
    command = re.search(
        r'healthCheck\s*=\s*\{\s*command\s*=\s*\[([^\]]+)\]', _infra("ecs.tf")
    )
    assert command is not None, "no ECS healthCheck command found"

    assert WORKER_PROBE_PATH in command.group(1)
    assert DEEP_CHECK_PATH not in command.group(1), (
        "the ECS container check is back on the deep check — a Redis outage "
        "will recycle every task mid-job"
    )


def test_no_automatic_probe_reads_the_deep_check() -> None:
    """The property, stated once: the deep check is for humans."""
    for name in ("alb.tf", "ecs.tf"):
        assert DEEP_CHECK_PATH not in _infra(name), (
            f"infra/{name} probes {DEEP_CHECK_PATH}; that endpoint reports "
            "dependency health and returns 503 for conditions no restart or "
            "deregistration can fix"
        )
