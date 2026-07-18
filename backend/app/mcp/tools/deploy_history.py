"""
`get_deploy_history` — what's running right now.

Minimal shape: a single "current" entry read from environment
variables the release workflow (or ECS task definition) is expected
to set. Multi-entry history is a follow-up that needs a
`deploy_markers` table + a startup write hook — deferred until a
scenario actually needs it.

The env vars we honor:
  - `APP_VERSION` (typical: the git tag, e.g. `v0.4.0`)
  - `APP_REVISION` (git SHA)
  - `BACKEND_IMAGE_TAG` (fallback for APP_VERSION when unset)

`APP_STARTED_AT` is captured at module import so multiple invocations
return the same value for the same process. Requires `telemetry:read`.
"""

import os
from datetime import UTC, datetime

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from pydantic import BaseModel, ConfigDict, Field

# Captured at module import — deterministic across calls in the same
# process. On restart it resets, which is exactly what "when did this
# deploy start" means anyway.
_STARTED_AT = datetime.now(UTC)


class _EmptyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeployEntry(BaseModel):
    version: str = Field(
        description="Best-effort version label — the git tag if set via "
        "APP_VERSION, else the image tag, else 'unknown'."
    )
    revision: str | None = Field(
        default=None, description="Git SHA if APP_REVISION is set."
    )
    image_tag: str | None = None
    started_at: datetime
    env: str
    notes: str = Field(
        default="",
        description="Free-form; today mentions the source of the "
        "version/revision labels so operators can trust them.",
    )


class GetDeployHistoryOutput(BaseModel):
    total: int
    entries: list[DeployEntry]


@tool(
    "get_deploy_history",
    description=(
        "Return what version + revision is running right now (a single "
        "'current' entry). Deploy history over time is a planned "
        "follow-up that needs a `deploy_markers` table. Today the "
        "agent gets the source of truth for what's live."
    ),
    input_model=_EmptyIn,
    output_model=GetDeployHistoryOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_deploy_history(
    _inp: _EmptyIn, ctx: ToolContext
) -> GetDeployHistoryOutput:
    from app.config import get_settings

    settings = get_settings()

    app_version = os.getenv("APP_VERSION")
    image_tag = os.getenv("BACKEND_IMAGE_TAG")
    revision = os.getenv("APP_REVISION")

    if app_version:
        version = app_version
        source = "APP_VERSION"
    elif image_tag:
        version = image_tag
        source = "BACKEND_IMAGE_TAG"
    else:
        version = "unknown"
        source = "(no env set)"

    entry = DeployEntry(
        version=version,
        revision=revision,
        image_tag=image_tag,
        started_at=_STARTED_AT,
        env=settings.environment,
        notes=f"version from {source}",
    )
    return GetDeployHistoryOutput(total=1, entries=[entry])
