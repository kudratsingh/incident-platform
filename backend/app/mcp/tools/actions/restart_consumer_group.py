"""
`restart_consumer_group` — the compensating counterpart to BOTH
consumer-affecting chaos hooks: `kill_consumer` (Wave 1 PR B) and
`inject_latency`.

Clears the Redis kill flag so a stopped consumer's next supervisor
restart succeeds, AND deletes any injected-latency flag so the
restarted consumer runs at full speed. A real task recycle would drop
the injected slowness too; before v0.4.5 the chaos help text promised
latency clearing this tool didn't perform, leaving `inject_latency`
with no Tier-1 remediation at all. In a real deploy this would also send a
kubectl/ECS API call to force a task recycle; here we do the honest
demoable thing: delete the kill key, let the worker's supervisor
loop pick the consumer back up. Same shape as the chaos side —
observable via `get_consumer_lag` recovery.

`actions:execute` + idempotent.
"""

from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.workers.kafka_consumer import kill_key_for, latency_key_for
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


class RestartConsumerGroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumer_group: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(
        min_length=8,
        max_length=255,
        description="Caller-supplied token. Repeat calls with the same "
        "value + same arguments return the cached result WITHOUT "
        "re-executing — including `accepted: true`. Reusing a key from "
        "an earlier incident therefore looks like a success and does "
        "nothing. Use a fresh key per distinct intent; only reuse one "
        "to retry the very same call after a transport failure.",
    )


class RestartConsumerGroupOutput(BaseModel):
    consumer_group: str
    # v0.4.9: the `kill_key` / `latency_key` string fields are gone.
    # They spelled out `chaos:kill:*` / `chaos:latency:*` in the
    # response of a tool that only requires `actions:execute` — so an
    # agent with no chaos scope still learned the test rig existed, and
    # at least one investigation chased the harness instead of the
    # fault. The booleans carry the whole operational outcome; the key
    # names were never actionable, only revealing.
    kill_key_cleared: bool
    latency_key_cleared: bool
    accepted: bool


@tool(
    "restart_consumer_group",
    description=(
        "Restart a stalled Kafka consumer group and clear any "
        "throttling applied to it, so its supervisor brings it back at "
        "full speed. `kill_key_cleared` / `latency_key_cleared` report "
        "whether each condition was actually present. Idempotent — "
        "repeat calls with the same idempotency_key are safe.\n"
        "VERIFYING: a real restart brings the consumer back within a "
        "few seconds (measured ~2s). Confirm via group membership and "
        "draining lag — not via get_consumer_lag alone, whose cached "
        "value keeps reading the pre-restart high for ~30s after a "
        "genuine recovery. `kill_key_cleared: false` with an unchanged "
        "consumer is the signature of a reused idempotency_key "
        "replaying an old success rather than acting."
    ),
    input_model=RestartConsumerGroupInput,
    output_model=RestartConsumerGroupOutput,
    required_scope=Scope.ACTIONS_EXECUTE,
    is_idempotent=True,
)
async def restart_consumer_group(
    inp: RestartConsumerGroupInput, ctx: ToolContext
) -> RestartConsumerGroupOutput:
    kill_key = kill_key_for(inp.consumer_group)
    latency_key = latency_key_for(inp.consumer_group)
    kill_cleared = bool(await ctx.redis.delete(kill_key))
    latency_cleared = bool(await ctx.redis.delete(latency_key))
    logger.warning(
        "action restart_consumer_group",
        extra={
            "group": inp.consumer_group,
            "kill_cleared": kill_cleared,
            "latency_cleared": latency_cleared,
        },
    )
    return RestartConsumerGroupOutput(
        consumer_group=inp.consumer_group,
        kill_key_cleared=kill_cleared,
        latency_key_cleared=latency_cleared,
        accepted=True,
    )
