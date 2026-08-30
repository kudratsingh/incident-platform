"""
`get_deploy_history` — what's deployed on this environment.

Preferred source: the `deploy_markers` table (populated by the release
pipeline in the future, and by `scripts/seed_eval_fixtures.py` today).
Returns the most recent N rows, newest first, optionally scoped to
one environment.

Fallback: if `deploy_markers` is empty **or the query itself errors
(missing table, connection issue, etc.)** the tool returns a single
synthetic "current" entry read from env vars (`APP_VERSION`,
`APP_REVISION`, `BACKEND_IMAGE_TAG`).

The DB error path is what makes this tool safe to expose during
staged rollouts — earlier versions crashed with HTTP 500 when
`deploy_markers` didn't exist yet (typical on a stack running an
older image against a fresher DB, or before migrations settled).

Requires `telemetry:read`.
"""

import os
from datetime import UTC, datetime

from app.core.db_degrade import degrade_on_db_error
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.repositories.deploy_marker import DeployMarkerRepository
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

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
        "snapshot when the table is empty. Useful for correlating "
        "incidents with a recent deploy — some deploys carry a "
        "`notes` annotation useful for that correlation.\n"
        "FRESHNESS: live read from Postgres, no cache. Deploy markers "
        "are written by the release pipeline, so a very recent deploy "
        "may not have a row yet — absence of a marker is weak evidence "
        "that no deploy happened."
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

    # Try the DB path first. Any error — missing table (staged rollout,
    # migration not yet applied), transient connection issue, transaction
    # already in a failed state — falls through to the env-based synthetic
    # entry rather than surfacing HTTP 500 to the agent. Logged as a
    # warning so operators know they're on the fallback path.
    from app.models.deploy_marker import DeployMarker

    rows: list[DeployMarker] = []
    total = 0
    db_error_note: str | None = None
    # SAVEPOINT, not a bare `try`. The failure this fallback exists for —
    # `deploy_markers` absent on a staged rollout — aborts the whole
    # Postgres transaction, so before R2-59 the fallback returned happily
    # and the envelope's audit write for this very call was then dropped
    # on the floor. Rolling back to the savepoint keeps the session usable
    # for everything that runs after the tool returns.
    async with degrade_on_db_error(ctx.db, what="deploy_markers") as probe:
        repo = DeployMarkerRepository(ctx.db)
        rows, total = await repo.list_recent(
            limit=inp.limit, environment=inp.environment
        )
    if probe.failed:
        db_error_note = f"deploy_markers query failed: {probe.error_type}"

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

    # Empty table (or DB error) → env-based single-entry synthetic.
    # Behaviour matches the pre-table version of this tool so unseeded
    # envs (fresh clones, CI) still get *something* useful.
    app_version = os.getenv("APP_VERSION")
    image_tag = os.getenv("BACKEND_IMAGE_TAG")
    revision = os.getenv("APP_REVISION")

    if app_version:
        version = app_version
    elif image_tag:
        version = image_tag
    else:
        version = "unknown"

    fallback_note = "synthetic entry from env vars (deploy_markers empty)"
    if db_error_note is not None:
        fallback_note = f"synthetic entry from env vars ({db_error_note})"

    synthetic = DeployEntry(
        version=version,
        revision=revision,
        image_tag=image_tag,
        deployed_at=_STARTED_AT,
        environment=inp.environment or settings.environment,
        notes=fallback_note,
    )
    return GetDeployHistoryOutput(total=1, entries=[synthetic], source="env")
