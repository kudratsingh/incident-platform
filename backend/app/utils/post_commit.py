"""Side effects that must not fire until the transaction commits.

An invalidation issued inside a transaction announces a change nobody can see
yet, so a reader refills the hole with the old row and the commit lands behind a
contradicting cache entry (R2-23). Services `register_post_commit(...)` and the
owner of `session.begin()` calls `run_post_commit(session)` after it. Rollback
drops the queue twice over: the drain never runs, and a rollback listener clears
it, because `session.info` survives a rollback and the worker loops reuse
sessions. **Hooks must not raise** — a Redis blip would turn a committed replay
into a 500; a failed invalidation costs one ten-second TTL of staleness.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = get_logger(__name__)

PostCommitHook = Callable[[], Awaitable[None]]

# Namespaced because `Session.info` is a shared per-session scratchpad.
_INFO_KEY = "app.post_commit_hooks"
_GUARD_KEY = "app.post_commit_rollback_guard"


def _queue(session: AsyncSession) -> dict[Any, Any] | None:
    """`session.info`, if this session actually has one.

    Never None in production; it exists for the unit suites' session stand-ins,
    which have no queue, so the entry points below do nothing for them.
    """
    info = getattr(session, "info", None)
    return info if isinstance(info, dict) else None


def register_post_commit(session: AsyncSession, hook: PostCommitHook) -> bool:
    """Queue `hook` to run once `session`'s current transaction commits.

    Registration order; registering the same effect twice runs it twice. Returns
    whether it was queued — False means no queue (the `_queue` stand-ins), and the
    caller decides whether to skip or run inline.
    """
    info = _queue(session)
    if info is None:
        return False
    _install_rollback_guard(session, info)
    hooks: list[PostCommitHook] = info.setdefault(_INFO_KEY, [])
    hooks.append(hook)
    return True


def _install_rollback_guard(session: AsyncSession, info: dict[Any, Any]) -> None:
    """Drop queued hooks when the session rolls back. Installed once.

    `session.info` outlives a rollback, so a session reused for a second
    transaction — the worker loops do — would otherwise drain the failed
    transaction's hooks and announce a rolled-back row (WO-R2-70).
    """
    if info.get(_GUARD_KEY):
        return
    sync_session = getattr(session, "sync_session", None)
    # Same stance as `_queue`: a stand-in gets no machinery.
    if not isinstance(sync_session, Session):
        return

    def _discard(*_args: Any) -> None:
        dropped = info.pop(_INFO_KEY, [])
        if dropped:
            logger.debug(
                "post_commit_hooks_discarded_on_rollback",
                extra={"count": len(dropped)},
            )

    event.listen(sync_session, "after_soft_rollback", _discard)
    info[_GUARD_KEY] = True


async def run_post_commit(session: AsyncSession) -> None:
    """Drain and run the queue, emptied first. Call only after the commit has landed."""
    info = _queue(session)
    if info is None:
        return
    hooks: list[PostCommitHook] = info.pop(_INFO_KEY, [])
    for hook in hooks:
        try:
            await hook()
        except Exception:
            # Deliberately swallowed — see the module docstring; named in the log.
            logger.warning(
                "post_commit_hook_failed",
                extra={"hook": getattr(hook, "__name__", repr(hook))},
                exc_info=True,
            )
