"""Side effects that must not fire until the transaction commits.

A cache invalidation issued from inside a transaction is an announcement
about a change nobody else can see yet. The row it invalidates is still
the pre-change row for every other connection, so a reader that misses on
the hole we just punched reads the *old* value from Postgres and puts it
straight back — and the commit that follows lands behind a cache entry
that now contradicts it (R2-23).

The transaction boundary is the only place that knows the write is real,
and in this codebase that boundary belongs to whoever opened
`session.begin()` — never to the service. So services register their
post-commit work here and the session owner drains it:

    # service
    register_post_commit(session, partial(JobCache.invalidate, redis, ...))

    # session owner
    async with session.begin():
        ...
    await run_post_commit(session)

Rollback needs no handling. The `session.begin()` block raises on the way
out, the drain never runs, and the queue dies with the session — no
commit, no side effect, which is exactly right.

Hooks must not raise, and this module holds them to it. By the time one
runs its transaction is durable and there is nothing left to roll back;
letting a Redis blip out of here would turn a committed replay into a
500 the caller reads as "it didn't happen". A failed invalidation costs
one TTL of staleness, and the TTL is ten seconds.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

PostCommitHook = Callable[[], Awaitable[None]]

# Namespaced because `Session.info` is a shared per-session scratchpad.
_INFO_KEY = "app.post_commit_hooks"


def _queue(session: AsyncSession) -> dict[Any, Any] | None:
    """`session.info`, if this session actually has one.

    `AsyncSession.info` is always a real dict, so in production this never
    returns None. It exists for the session stand-ins the unit suites pass
    to services — for those the honest answer is "this object has no
    post-commit queue", and both entry points below then do nothing rather
    than growing mock attributes that look like registered work.
    """
    info = getattr(session, "info", None)
    return info if isinstance(info, dict) else None


def register_post_commit(session: AsyncSession, hook: PostCommitHook) -> None:
    """Queue `hook` to run once `session`'s current transaction commits.

    Ordering is registration order. Registering the same effect twice runs
    it twice — the hooks here are idempotent, so that is a cost rather
    than a bug, and de-duplication would need an identity for a closure.
    """
    info = _queue(session)
    if info is None:
        return
    hooks: list[PostCommitHook] = info.setdefault(_INFO_KEY, [])
    hooks.append(hook)


async def run_post_commit(session: AsyncSession) -> None:
    """Drain and run the queue. Call only after the commit has landed.

    The queue is emptied before anything runs, so a caller that reuses the
    session for a second transaction starts clean even if a hook raises.
    """
    info = _queue(session)
    if info is None:
        return
    hooks: list[PostCommitHook] = info.pop(_INFO_KEY, [])
    for hook in hooks:
        try:
            await hook()
        except Exception:
            # Deliberately swallowed — see the module docstring. Logged at
            # warning with the hook's name so a systematic failure (Redis
            # unreachable, say) is still visible in the log stream rather
            # than only in the staleness it causes.
            logger.warning(
                "post_commit_hook_failed",
                extra={"hook": getattr(hook, "__name__", repr(hook))},
                exc_info=True,
            )
