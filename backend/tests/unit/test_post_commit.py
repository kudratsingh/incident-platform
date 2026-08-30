"""Unit tests for the post-commit hook queue (R2-23)."""

from typing import Any
from unittest.mock import AsyncMock

from app.utils.post_commit import register_post_commit, run_post_commit


class _Session:
    """The only surface the queue uses: a real `info` dict, which is what
    `AsyncSession` gives it."""

    def __init__(self) -> None:
        self.info: dict[Any, Any] = {}


async def test_hooks_run_in_registration_order() -> None:
    session = _Session()
    order: list[str] = []

    async def _first() -> None:
        order.append("first")

    async def _second() -> None:
        order.append("second")

    register_post_commit(session, _first)  # type: ignore[arg-type]
    register_post_commit(session, _second)  # type: ignore[arg-type]

    assert order == [], "registration must not run anything"

    await run_post_commit(session)  # type: ignore[arg-type]
    assert order == ["first", "second"]


async def test_the_queue_is_drained_once() -> None:
    """A session outlives its transaction — `get_db` holds one across the
    whole request. A hook left in the queue would fire again behind the
    next commit, invalidating on a transaction that never touched the
    job."""
    session = _Session()
    hook = AsyncMock()
    register_post_commit(session, hook)

    await run_post_commit(session)  # type: ignore[arg-type]
    await run_post_commit(session)  # type: ignore[arg-type]

    hook.assert_awaited_once()


async def test_a_failing_hook_does_not_stop_the_others() -> None:
    """Nor does it escape: by the time these run the transaction is
    durable, and raising would report a committed write as failed."""
    session = _Session()
    ran: list[str] = []

    async def _boom() -> None:
        raise ConnectionError("Redis unavailable")

    async def _after() -> None:
        ran.append("after")

    register_post_commit(session, _boom)  # type: ignore[arg-type]
    register_post_commit(session, _after)  # type: ignore[arg-type]

    await run_post_commit(session)  # type: ignore[arg-type]

    assert ran == ["after"]


async def test_draining_an_empty_session_is_a_no_op() -> None:
    await run_post_commit(_Session())  # type: ignore[arg-type]


async def test_a_session_without_a_real_info_dict_is_a_no_op() -> None:
    """Unit suites pass `AsyncMock` sessions to services. Those have no
    post-commit queue, and both entry points must say so quietly rather
    than growing mock attributes that look like registered work — or
    raising, on a path that only runs after a successful commit."""
    session = AsyncMock()
    register_post_commit(session, AsyncMock())
    await run_post_commit(session)
