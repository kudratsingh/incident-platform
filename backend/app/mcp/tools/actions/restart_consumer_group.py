"""
`restart_consumer_group` — the compensator for `kill_consumer` and `inject_latency`.

Deletes the kill flag so the supervisor's next restart succeeds, and the
injected-latency flag so the consumer returns at full speed — before v0.4.5 it
skipped the second, leaving `inject_latency` with no Tier-1 remediation. Recovery
shows up in `get_consumer_lag`. `actions:execute` + idempotent.
"""

from app.config import get_settings
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.registry import ToolContext, tool
from app.mcp.tools.consumer_lag import SEEDED_CONSUMER_GROUPS
from app.workers.kafka_consumer import kill_key_for, latency_key_for
from pydantic import BaseModel, ConfigDict, Field

logger = get_logger(__name__)


def _known_groups() -> frozenset[str]:
    """Every consumer-group name the platform can speak about.

    The union of the groups settings actually runs and the ones `get_consumer_lag`
    advertises — a scenario can restart either. Read at call time so a settings
    override is reflected without a reload.
    """
    settings = get_settings()
    from_settings = {
        value
        for name, value in vars(settings).items()
        if name.startswith("kafka_consumer_group_") and isinstance(value, str)
    }
    return frozenset(from_settings | set(SEEDED_CONSUMER_GROUPS))


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
    # v0.4.9: the `kill_key` / `latency_key` fields are gone — they spelled out
    # `chaos:*` key names in a tool needing only `actions:execute`. The booleans
    # carry the whole outcome.
    kill_key_cleared: bool
    latency_key_cleared: bool
    group_recognized: bool = Field(
        description="Whether `consumer_group` matches a group the "
        "platform knows about. False means the name reached no real "
        "consumer — almost always a typo, since `accepted` is true "
        "either way and both `*_cleared` flags will be false. NOT a "
        "refusal: an unrecognised name is still executed, so a group "
        "added after this build is never blocked."
    )
    accepted: bool = Field(
        description="The flag-clearing ran. It does NOT assert that a "
        "consumer restarted or that the group exists — the platform "
        "cannot observe either. Check `group_recognized` and the two "
        "`*_cleared` flags for what actually happened, and confirm "
        "recovery through group membership."
    )


@tool(
    "restart_consumer_group",
    description=(
        "Restart a stalled Kafka consumer group and clear any "
        "throttling applied to it, so its supervisor brings it back at "
        "full speed. `kill_key_cleared` / `latency_key_cleared` report "
        "whether each condition was actually present. Idempotent — "
        "repeat calls with the same idempotency_key are safe.\n"
        "WHAT `accepted` MEANS: the flag-clearing ran. It is true for "
        "any group name, including one that matches no consumer, and it "
        "does not assert that anything restarted. `group_recognized` "
        "reports whether the name matches a group the platform knows "
        "about; false there, with both `*_cleared` false, is a typo'd "
        "group name — the stalled consumer is still down. The name is "
        "never refused, so a group added after this build still works.\n"
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
    # Reported, never enforced (R2-17): a whitelist would refuse a new group.
    recognized = inp.consumer_group in _known_groups()
    logger.warning(
        "action restart_consumer_group",
        extra={
            "group": inp.consumer_group,
            "kill_cleared": kill_cleared,
            "latency_cleared": latency_cleared,
            "group_recognized": recognized,
        },
    )
    return RestartConsumerGroupOutput(
        consumer_group=inp.consumer_group,
        kill_key_cleared=kill_cleared,
        latency_key_cleared=latency_cleared,
        group_recognized=recognized,
        accepted=True,
    )
