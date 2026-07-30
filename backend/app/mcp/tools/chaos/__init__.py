"""
Chaos tools — imported unconditionally so the `@chaos_tool` decorators
fire. The decorator itself is the gate: when `CHAOS_ENABLED=false`,
each `@chaos_tool` invocation is a no-op and the tool never enters the
registry (see `app/mcp/chaos.py`).

Add new chaos tools by importing their module below.
"""

from app.mcp.tools.chaos import (  # noqa: F401
    bad_deploy,
    create_bad_data_job,
    inject_latency,
    kill_consumer,
    poison_message,
    saturate_redis,
)

__all__: list[str] = []
