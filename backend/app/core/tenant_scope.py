"""Explicit cross-tenant scope for the sessions that legitimately need it.

Background (ADR 0026, WO-R2-129). Every `tenant_isolation` policy used to
open with the ADR 0003 bootstrap branch::

    current_setting('app.tenant_id', true) IS NULL
    OR current_setting('app.tenant_id', true) = ''
    OR tenant_id = current_setting('app.tenant_id', true)::uuid

so a statement that had not set `app.tenant_id` satisfied the policy
*unconditionally*, on all eleven tenant tables. The default was
**fail-open**: forgetting the setting bought full cross-tenant read and
write, silently. That is the finding plat #192 proved live — an unscoped
connection wrote and read another tenant's digests with nothing raised
and nothing logged.

The policies are now strict: a row matches only when `app.tenant_id`
names its tenant. Since forgetting is no longer a way *in*, the paths
that genuinely span tenants need a way to *say so*, and this module is
it — one GUC, `app.tenant_scope`, set to `'platform'`.

Why a declared scope rather than the absence of one
---------------------------------------------------
The mechanism looks similar — a session variable the policy consults —
but the direction is reversed, and the direction is the whole point:

* **Before:** the escape was the default. Every new code path got
  cross-tenant access by not doing anything, and the failure was silent.
  A pooled connection whose GUC had reset to `''` got it too.
* **Now:** the escape is a statement of intent, made in one of five
  audited places. A path that forgets is *refused* — a visible error,
  not a silent leak. "Which code may cross tenants" is answerable by
  grepping for this module.

`set_config(..., true)` is **transaction-local**, deliberately: the
runtime shares one connection pool between requests and worker loops
(ADR 0015), so a session-level `SET` would leak platform scope onto
whichever request checked the connection out next. Scoping it to the
transaction makes that impossible by construction.

Why not a dedicated Postgres role
---------------------------------
The obvious alternative — workers connect as a role holding `BYPASSRLS`
— is unavailable and unattractive here:

* `BYPASSRLS` can only be granted by a superuser. Production's owner is
  the RDS master, which is **not** a superuser and has no `BYPASSRLS`
  (ADR 0015, "Why FORCE alone already enforces in production"), so the
  migration chain cannot create such a role on RDS at all.
* The variant that avoids `BYPASSRLS` — a role-membership predicate in
  the policy — still requires the workers to *connect* as a different
  role, i.e. a second engine and a second pool against a db.t3.micro.
  ADR 0015 considered and rejected exactly that.

See ADR 0026 for the full comparison.
"""

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

# The GUC the `tenant_isolation` policies consult for cross-tenant intent,
# and the single value that grants it. Both are also spelled out in the
# migration that creates the policies; this module is the only thing that
# sets them at runtime.
SCOPE_GUC = "app.tenant_scope"
PLATFORM_SCOPE = "platform"

_DECLARE_PLATFORM_SCOPE = f"SELECT set_config('{SCOPE_GUC}', '{PLATFORM_SCOPE}', true)"


class PlatformScopedSession(Session):
    """Sync session class carrying the `after_begin` hook below.

    A distinct class so the listener binds to *these* sessions only — the
    request-path sessions built by `dependencies._async_session` use the
    stock `Session` and must never acquire platform scope.
    """


@event.listens_for(PlatformScopedSession, "after_begin")
def _declare_platform_scope(
    session: Session, transaction: object, connection: object
) -> None:
    """Declare platform scope at the top of every transaction.

    `after_begin` fires once per transaction, before any of the caller's
    statements, which is what makes this reliable for worker loops that
    open ~30 different sessions across the dispatcher alone: the intent
    is attached to the factory they were handed, not to each query.

    No-op on SQLite (the unit/API suite): it has no `set_config` and no
    row-level security to satisfy.
    """
    if connection.dialect.name != "postgresql":  # type: ignore[attr-defined]
        return
    connection.exec_driver_sql(_DECLARE_PLATFORM_SCOPE)  # type: ignore[attr-defined]


def platform_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """A session factory whose every transaction spans all tenants.

    Same engine and therefore the same pool as the request path — the
    scope is a per-transaction declaration, not a second connection.
    """
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        sync_session_class=PlatformScopedSession,
    )


async def declare_tenant_scope(session: AsyncSession, tenant_id: object) -> None:
    """Point this transaction's RLS context at one tenant.

    The same `set_config('app.tenant_id', …, true)` the auth dependencies
    issue, for the request paths that authenticate *themselves* and so
    never pass through `get_current_user`: `POST /auth/register`,
    `POST /auth/login` (both write `audit_logs`) and the `?token=` job
    stream (which reads `jobs`).

    Those three used to run with no tenant context at all and were
    carried by the bootstrap branch — meaning the audit write for every
    login in the system, and the stream's job lookup, had no RLS backstop
    whatever. They each already know their tenant; now they say it. No-op
    on SQLite.
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
