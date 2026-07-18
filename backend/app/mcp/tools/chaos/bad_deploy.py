"""
`bad_deploy` — simulate a bad deployment landing on the platform.

Effect today: fires a `critical` alert with source `chaos:bad_deploy`
and sets a Redis flag `chaos:bad_deploy` with a TTL. Downstream code
paths can gate on the flag if they want to fail in a bad-deploy-y
way; today no path does yet, so the observable signal is purely the
alert. The agent should see it via `list_incidents` and correlate
against `get_deploy_history`.

Faithful "actually roll a broken image" simulation is a follow-up —
it needs the deploy_markers table + a startup writer we haven't
built. The tool description tells the caller as much so the agent's
LLM doesn't over-index on this being a full outage.

Requires `chaos:invoke`. Registered only when `CHAOS_ENABLED=true`.
Blast radius: environment_wide (alerts propagate, flag is shared).
"""

from app.core.logging import get_logger
from app.mcp.chaos import BlastRadius, chaos_tool
from app.mcp.registry import ToolContext
from app.repositories.alert import AlertRepository
from app.services.alerts import AlertService
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)

BAD_DEPLOY_KEY = "chaos:bad_deploy"


class BadDeployInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        default="chaos:bad_deploy",
        min_length=1,
        max_length=64,
        description="Short label written on the alert + Redis flag. "
        "Lets multiple concurrent chaos runs distinguish their "
        "signals.",
    )
    ttl_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description="How long the Redis flag stays active. Default 10 "
        "minutes.",
    )
    note: str | None = Field(
        default=None,
        max_length=512,
        description="Optional free-form context recorded on the alert.",
    )


class BadDeployOutput(BaseModel):
    label: str
    flag_key: str
    alert_id: str
    ttl_seconds: int


@chaos_tool(
    "bad_deploy",
    description=(
        "Simulate a bad deploy landing: fires a `critical` alert with "
        "source `chaos:bad_deploy` and sets Redis flag "
        "`chaos:bad_deploy` for `ttl_seconds`. Today the observable "
        "signal is the alert; wiring the flag into a real bad-behaviour "
        "path is a follow-up that needs deploy_markers."
    ),
    input_model=BadDeployInput,
    output_model=BadDeployOutput,
    blast_radius=BlastRadius.ENVIRONMENT_WIDE,
)
async def bad_deploy(inp: BadDeployInput, ctx: ToolContext) -> BadDeployOutput:
    # Redis flag first — cheap, and if it fails we still want the alert.
    await ctx.redis.set(BAD_DEPLOY_KEY, inp.label, ex=inp.ttl_seconds)

    service = AlertService(AlertRepository(ctx.db))
    extra = {"label": inp.label, "ttl_seconds": inp.ttl_seconds}
    if inp.note:
        extra["note"] = inp.note
    alert = await service.create_alert(
        tenant_id=ctx.principal.tenant_id,
        severity="critical",
        source="chaos:bad_deploy",
        title="Simulated bad deploy",
        description=inp.note,
        extra_data=extra,
    )

    logger.warning(
        "chaos bad_deploy fired",
        extra={
            "label": inp.label,
            "alert_id": str(alert.id),
            "ttl_seconds": inp.ttl_seconds,
        },
    )
    return BadDeployOutput(
        label=inp.label,
        flag_key=BAD_DEPLOY_KEY,
        alert_id=str(alert.id),
        ttl_seconds=inp.ttl_seconds,
    )
