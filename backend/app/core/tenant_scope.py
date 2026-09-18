"""Explicit cross-tenant scope for the sessions that legitimately need it.

ADR 0026, WO-R2-129: every `tenant_isolation` policy used to open with the ADR
0003 bootstrap branch, so a statement that never set `app.tenant_id` satisfied it
*unconditionally* on all eleven tenant tables — fail-open, and plat #192 proved it
live. The policies are strict now, so the paths that genuinely span tenants say so
instead: one GUC, `app.tenant_scope` = `'platform'`, declared in five audited
places, and a path that forgets is refused rather than leaking. `set_config(...,
true)` is **transaction-local** because requests and worker loops share one pool
(ADR 0015). A `BYPASSRLS` role is not an option: the RDS master is no superuser.
"""

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

# The GUC `tenant_isolation` consults; set only here at runtime.
SCOPE_GUC = "app.tenant_scope"
PLATFORM_SCOPE = "platform"

_DECLARE_PLATFORM_SCOPE = f"SELECT set_config('{SCOPE_GUC}', '{PLATFORM_SCOPE}', true)"


class PlatformScopedSession(Session):
    """Sync session class carrying the `after_begin` hook below.

    The request path's stock `Session` must never get platform scope.
    """


@event.listens_for(PlatformScopedSession, "after_begin")
def _declare_platform_scope(
    session: Session, transaction: object, connection: object
) -> None:
    """Declare platform scope at the top of every transaction.

    `after_begin` fires before any caller statement, so the intent rides the
    factory the worker loops were handed rather than each query. No-op on SQLite.
    """
    if connection.dialect.name != "postgresql":  # type: ignore[attr-defined]
        return
    connection.exec_driver_sql(_DECLARE_PLATFORM_SCOPE)  # type: ignore[attr-defined]


def platform_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """A session factory whose every transaction spans all tenants (same engine/pool)."""
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        sync_session_class=PlatformScopedSession,
    )


async def declare_tenant_scope(session: AsyncSession, tenant_id: object) -> None:
    """Point this transaction's RLS context at one tenant.

    The `set_config('app.tenant_id', …, true)` the auth dependencies issue, for the
    paths that skip `get_current_user`: `POST /auth/register`, `POST /auth/login`
    and the `?token=` job stream. No-op on SQLite.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


__all__ = [
    "PLATFORM_SCOPE",
    "SCOPE_GUC",
    "PlatformScopedSession",
    "declare_tenant_scope",
    "platform_session_factory",
]
