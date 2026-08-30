"""ADR 0006's topology must exist where topology is described (WO-R2-68).

ADR 0006 chose a standalone process for the MCP surface — "a process of its
own in every deployed environment" — and accepted "compose stanza, ECS
service, health check, alarms" as the cost. The compose stanza shipped. The
ECS half did not, for long enough that `infra/` (the only description of
production this repo has) said the agent-facing surface did not exist there
while the ADR and ARCHITECTURE.md both said it did.

Prose corrections decay; the repo already learned that once with the phantom
`msk.tf` (ADR 0018), which is asserted mechanically in CI for the same
reason. This is that assertion for ADR 0006.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _infra(name: str) -> str:
    return (_REPO_ROOT / "infra" / name).read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize(
    ("resource_type", "name", "file"),
    [
        ("aws_ecs_task_definition", "mcp", "ecs.tf"),
        ("aws_ecs_service", "mcp", "ecs.tf"),
        ("aws_lb_target_group", "mcp", "alb.tf"),
        ("aws_lb_listener_rule", "mcp", "alb.tf"),
        ("aws_security_group", "mcp", "networking.tf"),
        ("aws_cloudwatch_log_group", "mcp", "ecs.tf"),
        ("aws_cloudwatch_metric_alarm", "ecs_mcp_tasks", "cloudwatch.tf"),
    ],
)
def test_the_second_deployable_is_actually_declared(
    resource_type: str, name: str, file: str
) -> None:
    assert f'resource "{resource_type}" "{name}"' in _infra(file), (
        f"ADR 0006 accepts an ECS service with its own health check and "
        f"alarms as the cost of the standalone MCP process; "
        f'{resource_type}.{name} is missing from infra/{file}'
    )


def test_the_mcp_task_runs_the_standalone_entrypoint() -> None:
    """Same image, different command — the ADR's "no second build" claim."""
    ecs = _infra("ecs.tf")
    mcp_block = ecs.split('resource "aws_ecs_task_definition" "mcp"')[1]

    assert "app.mcp.standalone:app" in mcp_block
    assert "8001" in mcp_block
    # The backend's repository, not one of its own: two images could skew,
    # and a skew means the MCP surface fronting a different commit's service
    # layer than REST — the drift this topology was chosen to prevent.
    assert "aws_ecr_repository.backend.repository_url" in mcp_block


def test_the_mcp_task_does_not_carry_the_owner_database_credential() -> None:
    """It never migrates, so it has no business holding the credential that
    could. `scripts/entrypoint.sh` (which runs alembic) is bypassed by the
    command override."""
    mcp_block = _infra("ecs.tf").split('resource "aws_ecs_task_definition" "mcp"')[1]
    mcp_block = _uncommented(mcp_block.split('resource "aws_ecs_service"')[0])

    assert "database_url_owner" not in mcp_block
    assert "app_db_password" not in mcp_block
    # It does need the runtime role, the signing key and Redis.
    assert "aws_secretsmanager_secret.database_url.arn" in mcp_block
    assert "aws_secretsmanager_secret.secret_key.arn" in mcp_block


def test_the_mcp_target_group_probes_shallow_liveness() -> None:
    alb = _infra("alb.tf")
    mcp_tg = alb.split('resource "aws_lb_target_group" "mcp"')[1].split("resource ")[0]
    paths = re.findall(r'path\s*=\s*"([^"]+)"', _uncommented(mcp_tg))

    assert paths == ["/healthz"]


def test_the_mcp_endpoint_is_routed_and_served() -> None:
    """The listener rule's path and the app's route have to agree, or the
    agent gets a 404 from a service that is running perfectly."""
    from app.mcp.standalone import create_mcp_app

    rule = _infra("alb.tf").split('resource "aws_lb_listener_rule" "mcp"')[1]
    values = re.search(r"values\s*=\s*\[([^\]]+)\]", rule)
    assert values is not None
    assert '"/mcp"' in values.group(1)

    served = {getattr(route, "path", "") for route in create_mcp_app().routes}
    assert "/mcp" in served
    assert "/healthz" in served


def test_the_database_and_cache_admit_the_mcp_security_group() -> None:
    """A service on the ALB that cannot reach Postgres is a service that
    answers every tool call with a 500."""
    networking = _uncommented(_infra("networking.tf"))
    for sg in ("rds", "redis"):
        block = networking.split(f'resource "aws_security_group" "{sg}"')[1]
        block = block.split("resource ")[0]
        assert "aws_security_group.mcp.id" in block, (
            f"the {sg} security group does not admit the MCP tasks"
        )
