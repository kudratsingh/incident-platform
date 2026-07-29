"""
`get_deploy_history` — what's deployed on this environment.

Preferred source: the `deploy_markers` table (populated by the release
pipeline in the future, and by `scripts/seed_eval_fixtures.py` today).
Returns the most recent N rows, newest first, optionally scoped to
one environment.

Fallback: if `deploy_markers` is empty, return a single synthetic
"current" entry read from env vars (`APP_VERSION`, `APP_REVISION`,
`BACKEND_IMAGE_TAG`). Keeps behaviour graceful on unseeded envs
(fresh clones, CI test runs) without failing the tool call.

Requires `telemetry:read`.
"""

import os
from datetime import UTC, datetime

from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.deploy_marker import DeployMarkerRepository
from pydantic import BaseModel, ConfigDict, Field

# Captured at module import — deterministic across calls in the same
# process. Used only by the env-fallback path.
_STARTED_AT = datetime.now(UTC)


class GetDeployHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str | None = Field(
        default=None,
        description="Filter to one environment (e.g. `prod`, `staging`). "
        "Omit for cross-env history.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of deploy markers to return.",
    )


class DeployEntry(BaseModel):
    version: str = Field(
        description="Version label — a git tag, image tag, or opaque "
        "identifier the release pipeline set."
    )
    revision: str | None = Field(
        default=None, description="Git SHA if known."
    )
    image_tag: str | None = None
    deployed_at: datetime
    environment: str
    notes: str | None = Field(
        default=None,
        description="Free-form annotation attached to the deploy — "
        "correlation hints, feature flags, etc.",
    )


class GetDeployHistoryOutput(BaseModel):
    total: int
    entries: list[DeployEntry]
    source: str = Field(
        description="Where the data came from: `deploy_markers` when "
        "the table is populated, `env` for the single-entry env fallback."
    )


@tool(
    "get_deploy_history",
    description=(
        "Return recent deploys, newest first. Prefers the "
        "`deploy_markers` table; falls back to a single-entry env "
        "snapshot when unseeded. Useful for correlating incidents "
        "with a recent deploy — the seed script annotates one row "
        "with a `notes` string for exactly this hypothesis-testing "
        "use case."
    ),
    input_model=GetDeployHistoryInput,
    output_model=GetDeployHistoryOutput,
    required_scope=Scope.TELEMETRY_READ,
)
async def get_deploy_history(
    inp: GetDeployHistoryInput, ctx: ToolContext
) -> GetDeployHistoryOutput:
    from app.config import get_settings

    settings = get_settings()
    repo = DeployMarkerRepository(ctx.db)
    rows, total = await repo.list_recent(
        limit=inp.limit, environment=inp.environment
    )

    if rows:
        return GetDeployHistoryOutput(
            total=total,
            entries=[
                DeployEntry(
                    version=r.version,
                    revision=r.revision,
                    image_tag=r.image_tag,
                    deployed_at=r.deployed_at,
                    environment=r.environment,
                    notes=r.notes,
                )
                for r in rows
            ],
            source="deploy_markers",
        )

    # Empty table → env-based single-entry synthetic. Behaviour matches
    # the pre-table version of this tool so unseeded envs (fresh clones,
    # CI) still get *something* useful.
    app_version = os.getenv("APP_VERSION")
    image_tag = os.getenv("BACKEND_IMAGE_TAG")
    revision = os.getenv("APP_REVISION")

    if app_version:
        version = app_version
    elif image_tag:
        version = image_tag
    else:
        version = "unknown"

    synthetic = DeployEntry(
        version=version,
        revision=revision,
        image_tag=image_tag,
        deployed_at=_STARTED_AT,
        environment=inp.environment or settings.environment,
        notes="synthetic entry from env vars (deploy_markers empty)",
    )
    return GetDeployHistoryOutput(total=1, entries=[synthetic], source="env")
