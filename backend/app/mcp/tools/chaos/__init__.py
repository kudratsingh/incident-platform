"""Chaos tools, imported so the `@chaos_tool` decorators fire. The decorator is
the gate (`app/mcp/chaos.py`): a no-op when `CHAOS_ENABLED=false`.
"""

from app.mcp.tools.chaos import (  # noqa: F401
    bad_deploy,
    create_bad_data_job,
    create_mislabeled_dlq_job,
    create_stale_cache,
    create_stuck_dag,
    degrade_downstream,
    inject_latency,
    kill_consumer,
    pause_control_loop,
    pause_dag_chaos,
    poison_message,
    saturate_db_pool,
    saturate_redis,
    seed_dlq_messages,
    slow_query,
)

__all__: list[str] = []
