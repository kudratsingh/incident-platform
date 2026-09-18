"""The three worlds Family C could not reach, read back through the agent's own tools.

WO-R3-274 + WO-R3-275, end to end over the MCP wire. The unit twin
(`tests/unit/test_stranded_chain_and_lab_pause.py`) pins the schemas, the
descriptions and the two documentation promises; this file proves that what the
hooks write is what the read tools return, because that is the acceptance
criterion for every one of these worlds — a fault nobody can observe through
`get_dag_state` / `search_traces` / `list_dlq_messages` is not a world.

  * `resolver_stall` — `create_stuck_dag(root_status="completed")`: root
    `completed`, descendants `waiting`, and the DLQ untouched. The
    discriminator is an *absence*, so the absence is asserted on the tool that
    would show it.
  * `downstream_child_failed` — the same plus `failed_step`: exactly one DLQ
    row, under a root that succeeded.
  * `paused_dag` — `pause_dag_chaos`: a pause that reads identically to one
    `pause_dag` set. Asserted by taking BOTH pauses on two identical chains in
    one test and comparing the tool output field by field, which is the only
    form of that claim that cannot rot as `get_dag_state` grows fields.

Harness is `test_mcp_chaos_stuck_dag.py`'s: a fresh MCP app under
`CHAOS_ENABLED=true` with only the modules these tests invoke reloaded.
"""

from __future__ import annotations

import importlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from app.config import Settings
from app.core.scopes import Scope
from app.dependencies import get_db, get_redis
from app.mcp import protocol
from app.mcp.registry import _restore_for_tests, _snapshot_for_tests
from app.models.enums import JobStatus
from app.models.job import Job
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from app.utils.dag_pause import pause_key_for
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _RedisStub:
    def __init__(self) -> None:
        self._store: dict[str, bytes | str] = {}
        self._ttls: dict[str, int] = {}

    async def get(self, key: str) -> bytes | str | None:
        return self._store.get(key)

    async def set(
        self, key: str, value: bytes | str, ex: int | None = None
    ) -> bool:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    async def mget(self, keys: list[str]) -> list[bytes | str | None]:
        return [self._store.get(k) for k in keys]

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2
        return self._ttls.get(key, -1)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._ttls.pop(k, None)
                removed += 1
        return removed

    def keys_matching(self, prefix: str) -> list[str]:
        return sorted(k for k in self._store if k.startswith(prefix))


def _mcp_app_with_chaos_enabled(  # type: ignore[no-untyped-def]
    db_session: AsyncSession, redis_stub: _RedisStub
):
    with patch(
        "app.mcp.standalone.assert_chaos_gate", lambda *a, **kw: None
    ), patch(
        "app.mcp.chaos.get_settings",
        return_value=Settings(chaos_enabled=True, environment="test"),
    ):
        from app.mcp.tools import chaos as chaos_pkg

        snap = _snapshot_for_tests()
        _restore_for_tests({})
        importlib.reload(chaos_pkg.create_stuck_dag)  # type: ignore[attr-defined]
        importlib.reload(chaos_pkg.pause_dag_chaos)  # type: ignore[attr-defined]

        from app.mcp.tools import dag_state as _dag_state
        from app.mcp.tools import list_dlq_messages as _dlq
        from app.mcp.tools import traces as _traces
        from app.mcp.tools.actions import pause_dag as _pause_dag

        for _mod in (_dag_state, _dlq, _traces, _pause_dag):
            importlib.reload(_mod)

        from app.mcp.standalone import create_mcp_app

        app = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis_stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis

    def _teardown() -> None:
        _restore_for_tests(snap)

    return app, _teardown


async def _token(
    db_session: AsyncSession, tenant_id: uuid.UUID, scopes: list[str]
) -> str:
    svc = ServiceAccountService(
        ServiceAccountRepository(db_session),
        ServiceAccountTokenRepository(db_session),
        AuditRepository(db_session),
    )
    sa = await svc.create_service_account(
        tenant_id=tenant_id,
        name=f"probe-{uuid.uuid4().hex[:8]}",
        scopes=scopes,
        created_by_user_id=None,
    )
    _, plaintext = await svc.mint_token(
        service_account=sa, scopes=None, ttl=None, minted_by_user_id=None
    )
    return plaintext


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}


async def _call(
    ac: AsyncClient, token: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    resp = await ac.post(
        "/mcp",
        json=_rpc("tools/call", {"name": tool_name, "arguments": arguments}),
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


def _content(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(body["result"]["content"][0]["text"])


async def _job(db_session: AsyncSession, job_id: str) -> Job:
    row = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    await db_session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# resolver_stall — a chain with no dead-letter row in it
# ---------------------------------------------------------------------------


async def test_the_stranded_chain_reads_exactly_as_the_plan_describes(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Every fact plan 01 §7.2 lists for `resolver_stall`, from the read tools.

    Root `completed`, descendants `waiting`, `paused` false with `paused_by`
    null, the descendants visible to `search_traces(status="waiting")` with a
    long-past `created_at`, and the DLQ showing nothing of the chain. The last
    one is the discriminator: there is nothing to replay, which is what makes
    escalating the correct answer rather than a guess.
    """
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(
                    ac,
                    chaos,
                    "create_stuck_dag",
                    {
                        "chain_name": "stranded",
                        "root_status": "completed",
                        "child_age_seconds": 2820,  # the proof's 47 minutes
                    },
                )
            )
            assert made["created"] is True
            assert made["dead_letter_job_id"] is None
            assert made["waiting_job_ids"] == made["step_job_ids"]

            reader = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            child = made["waiting_job_ids"][0]
            dag = _content(
                await _call(ac, reader, "get_dag_state", {"job_id": child})
            )
            waiting = _content(
                await _call(
                    ac, reader, "search_traces", {"status": "waiting"}
                )
            )
            dlq = _content(await _call(ac, reader, "list_dlq_messages", {}))

        # get_dag_state: self waiting, parent completed, nothing paused.
        by_id = {n["id"]: n for n in dag["nodes"]}
        assert by_id[child]["status"] == JobStatus.WAITING.value
        assert by_id[made["root_job_id"]]["status"] == JobStatus.COMPLETED.value
        assert dag["paused"] is False
        assert dag["paused_by"] is None
        assert dag["paused_expires_in_seconds"] is None

        # search_traces: the descendants are there, and already old.
        matched = {m["job_id"]: m for m in waiting["matches"]}
        for step_id in made["step_job_ids"]:
            assert step_id in matched, "a stranded descendant is not searchable"
            created = datetime.fromisoformat(matched[step_id]["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            assert datetime.now(UTC) - created > timedelta(minutes=45)

        # list_dlq_messages: nothing of this chain — the whole point.
        assert dlq["total"] == 0
        assert dlq["items"] == []

        # And no row of the chain is dead-lettered, checked past the tools too.
        for job_id in (
            made["root_job_id"],
            made["completed_parent_id"],
            *made["step_job_ids"],
        ):
            row = await _job(db_session, job_id)
            assert row.status != JobStatus.DEAD_LETTER.value
            assert row.payload is not None
            assert row.payload["seeded_fixture"] is True
    finally:
        teardown()


async def test_the_default_chain_is_unchanged_by_the_new_inputs(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The compatibility claim, asserted rather than asserted-about.

    Four scenarios and the commander's canned fixtures are graded against the
    dead-lettered chain, so a call that passes none of the new arguments has to
    produce exactly what it always produced — including the two new output
    fields agreeing with the old ones.
    """
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "unchanged"}
                )
            )
        assert made["dead_letter_job_id"] == made["root_job_id"]
        assert made["step_job_ids"] == made["waiting_job_ids"]

        root = await _job(db_session, made["root_job_id"])
        assert root.status == JobStatus.DEAD_LETTER.value
        assert root.retry_count == 3
        assert root.error_message is not None
        assert root.remediation_hint == "wait_and_replay"
        # No backdate was asked for, so the rows carry the server's own clock.
        assert datetime.now(UTC) - root.created_at.replace(
            tzinfo=root.created_at.tzinfo or UTC
        ) < timedelta(minutes=5)
        for step_id in made["waiting_job_ids"]:
            step = await _job(db_session, step_id)
            assert step.status == JobStatus.WAITING.value
            assert step.retry_count == 0
            assert step.error_message is None
            assert step.remediation_hint is None
    finally:
        teardown()


# ---------------------------------------------------------------------------
# downstream_child_failed — exactly one dead-letter row, below a good root
# ---------------------------------------------------------------------------


async def test_failed_step_dead_letters_one_descendant_and_only_one(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The `downstream_child_failed` world, and its coherence.

    `failed_step=2` on a three-step chain: step-1 `completed` (it had to run for
    step-2 to have been dispatched at all), step-2 `dead_letter`, step-3
    `waiting` behind it. The DLQ shows step-2 and nothing else, and the root —
    the job an alert would name — is `completed`, which is what makes the world
    different from every chain this hook could build before.
    """
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            made = _content(
                await _call(
                    ac,
                    chaos,
                    "create_stuck_dag",
                    {
                        "chain_name": "downstream",
                        "root_status": "completed",
                        "waiting_steps": 3,
                        "failed_step": 2,
                        "remediation_hint": "human_required",
                    },
                )
            )
            reader = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            dlq = _content(await _call(ac, reader, "list_dlq_messages", {}))
            dag = _content(
                await _call(
                    ac, reader, "get_dag_state", {"job_id": made["step_job_ids"][1]}
                )
            )

        first, failed, last = made["step_job_ids"]
        assert made["dead_letter_job_id"] == failed
        assert made["waiting_job_ids"] == [last]

        assert (await _job(db_session, made["root_job_id"])).status == (
            JobStatus.COMPLETED.value
        )
        assert (await _job(db_session, first)).status == JobStatus.COMPLETED.value
        assert (await _job(db_session, last)).status == JobStatus.WAITING.value

        failed_row = await _job(db_session, failed)
        assert failed_row.status == JobStatus.DEAD_LETTER.value
        assert failed_row.retry_count == 3
        assert failed_row.remediation_hint == "human_required"
        assert failed_row.error_message is not None

        # Exactly one DLQ row, and it is the descendant — not the root.
        assert dlq["total"] == 1
        assert [item["id"] for item in dlq["items"]] == [failed]

        by_id = {n["id"]: n for n in dag["nodes"]}
        assert by_id[failed]["status"] == JobStatus.DEAD_LETTER.value
        assert by_id[first]["status"] == JobStatus.COMPLETED.value
        assert by_id[last]["status"] == JobStatus.WAITING.value
    finally:
        teardown()


async def test_the_same_chain_name_in_a_different_shape_is_refused(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Ids do not depend on the shape, so asking for a different one under a
    name that already exists is drift, not a repeat.

    Refusing is the right answer: rewriting a chain's shape under its own name
    would silently change a world a scenario has already pinned by id.
    """
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            first = _content(
                await _call(
                    ac,
                    chaos,
                    "create_stuck_dag",
                    {"chain_name": "reshape", "root_status": "completed"},
                )
            )
            assert first["created"] is True

            # Same shape again: idempotent.
            again = _content(
                await _call(
                    ac,
                    chaos,
                    "create_stuck_dag",
                    {"chain_name": "reshape", "root_status": "completed"},
                )
            )
            assert again["created"] is False

            # Different shape: refused, and the existing chain is untouched.
            body = await _call(
                ac, chaos, "create_stuck_dag", {"chain_name": "reshape"}
            )
        assert body["error"]["data"]["error_code"] == "stuck_chain_name_in_use"
        assert (await _job(db_session, first["root_job_id"])).status == (
            JobStatus.COMPLETED.value
        )
    finally:
        teardown()


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"failed_step": 1}, id="failed_step-without-completed-root"),
        pytest.param(
            {"root_status": "completed", "waiting_steps": 2, "failed_step": 3},
            id="failed_step-past-the-end",
        ),
        pytest.param({"child_age_seconds": 86_401}, id="backdate-past-a-day"),
        pytest.param({"root_status": "cancelled"}, id="unknown-root-status"),
    ],
)
async def test_incoherent_arguments_are_refused_over_the_wire(
    db_session: AsyncSession,
    default_tenant,  # type: ignore[no-untyped-def]
    test_user,  # type: ignore[no-untyped-def]
    arguments: dict[str, Any],
) -> None:
    """All four refusals are argument errors, not `stuck_chain_name_in_use`:
    nothing about the environment is wrong, and no row is written."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            body = await _call(
                ac,
                chaos,
                "create_stuck_dag",
                {"chain_name": "refused", **arguments},
            )
        assert body["error"]["code"] == protocol.JSONRPC_INVALID_PARAMS
        rows = (
            (
                await db_session.execute(
                    select(Job).where(Job.status == JobStatus.WAITING.value)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []
    finally:
        teardown()


# ---------------------------------------------------------------------------
# paused_dag — the lab pause and the operator pause, side by side
# ---------------------------------------------------------------------------


async def test_the_lab_pause_reads_identically_to_an_operator_pause(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """The claim the whole hook exists for, asserted as an equality.

    Two identical chains; one paused by `pause_dag` with `actions:execute`, the
    other by `pause_dag_chaos` with `chaos:invoke`. Every field `get_dag_state`
    returns is compared with the ids normalised away — so a field added to that
    output later is covered by this test the day it ships, which a hand-listed
    set of assertions would not be.
    """
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            actions = await _token(
                db_session, default_tenant.id, [Scope.ACTIONS_EXECUTE.value]
            )
            reader = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )

            by_operator = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "by-operator"}
                )
            )
            by_lab = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "by-lab"}
                )
            )

            operator_pause = _content(
                await _call(
                    ac,
                    actions,
                    "pause_dag",
                    {
                        "root_job_id": by_operator["root_job_id"],
                        "idempotency_key": "operator-pause-1",
                    },
                )
            )
            lab_pause = _content(
                await _call(
                    ac,
                    chaos,
                    "pause_dag_chaos",
                    {"root_job_id": by_lab["root_job_id"]},
                )
            )

            reads = {}
            for label, made in (("operator", by_operator), ("lab", by_lab)):
                reads[label] = {
                    "root": _content(
                        await _call(
                            ac,
                            reader,
                            "get_dag_state",
                            {"job_id": made["root_job_id"]},
                        )
                    ),
                    "child": _content(
                        await _call(
                            ac,
                            reader,
                            "get_dag_state",
                            {"job_id": made["waiting_job_ids"][0]},
                        )
                    ),
                }

        # Same default TTL, so the two pauses are the same pause.
        assert operator_pause["ttl_seconds"] == lab_pause["ttl_seconds"] == 600
        assert operator_pause["pause_key"] == pause_key_for(
            by_operator["root_job_id"]
        )
        assert lab_pause["pause_key"] == pause_key_for(by_lab["root_job_id"])

        def _normalise(read: dict[str, Any], made: dict[str, Any]) -> Any:
            """Everything but the ids, which are the one thing that must differ.

            Serialise, swap each chain id for its role, parse back. Comparing
            the whole structure is what makes this survive `get_dag_state`
            growing a field — a hand-listed set of assertions would not.
            """
            names = {
                made["completed_parent_id"]: "upstream",
                made["root_job_id"]: "root",
                **{
                    job_id: f"step-{i}"
                    for i, job_id in enumerate(made["step_job_ids"], start=1)
                },
            }
            text = json.dumps(read, sort_keys=True)
            for job_id, role in names.items():
                text = text.replace(job_id, role)
            return json.loads(text)

        for where in ("root", "child"):
            assert _normalise(reads["operator"][where], by_operator) == _normalise(
                reads["lab"][where], by_lab
            ), f"a lab pause is distinguishable from an operator pause at {where}"

        # …and the thing that equality is really about: paused true with an
        # expiry on the root, the descendant naming the root as `paused_by`.
        assert reads["lab"]["root"]["paused"] is True
        assert reads["lab"]["root"]["paused_expires_in_seconds"] == 600
        assert reads["lab"]["child"]["paused"] is False
        assert reads["lab"]["child"]["paused_by"] == by_lab["root_job_id"]
    finally:
        teardown()


async def test_the_lab_pause_lapses_and_the_reset_sweep_reaches_its_key(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """Two teardowns, both asserted, because the hook has no undo call.

    The TTL: `get_dag_state` reports `paused` off the key's presence, so
    dropping the key is what expiry does on Redis's own clock, and the chain
    reads unpaused again with nothing called. The reset: the key is
    `dag:paused:<root>` — deliberately NOT under `chaos:*`, because it has to be
    the key the platform reads — so the sweep that reaches it is
    `_clear_dag_pauses`, matched here against the pattern that sweep uses.
    """
    import fnmatch

    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            reader = await _token(
                db_session, default_tenant.id, [Scope.INCIDENTS_READ.value]
            )
            made = _content(
                await _call(
                    ac, chaos, "create_stuck_dag", {"chain_name": "lapsing"}
                )
            )
            paused = _content(
                await _call(
                    ac,
                    chaos,
                    "pause_dag_chaos",
                    {"root_job_id": made["root_job_id"], "ttl_seconds": 30},
                )
            )
            assert paused["accepted"] is True

            live = _content(
                await _call(
                    ac, reader, "get_dag_state", {"job_id": made["root_job_id"]}
                )
            )
            assert live["paused"] is True
            assert live["paused_expires_in_seconds"] == 30

            # The key the lab wrote is the one the reset's pattern matches, and
            # it is not one the chaos:* scan would ever see.
            written = redis_stub.keys_matching("dag:paused:")
            assert written == [paused["pause_key"]]
            assert all(fnmatch.fnmatch(k, "dag:paused:*") for k in written)
            assert redis_stub.keys_matching("chaos:") == []

            await redis_stub.delete(*written)  # what the TTL, or the reset, does
            lapsed = _content(
                await _call(
                    ac, reader, "get_dag_state", {"job_id": made["root_job_id"]}
                )
            )
        assert lapsed["paused"] is False
        assert lapsed["paused_by"] is None
        assert lapsed["paused_expires_in_seconds"] is None
    finally:
        teardown()


async def test_the_lab_pause_refuses_a_root_it_cannot_see(
    db_session: AsyncSession, default_tenant, test_user  # type: ignore[no-untyped-def]
) -> None:
    """A scenario that mistypes a job id must be told, not quietly succeed
    against a key nothing reads. Same refusal `pause_dag` gives."""
    redis_stub = _RedisStub()
    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            chaos = await _token(
                db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
            )
            body = await _call(
                ac,
                chaos,
                "pause_dag_chaos",
                {"root_job_id": str(uuid.uuid4())},
            )
        assert body["error"]["data"]["error_code"] == "not_found"
        assert redis_stub.keys_matching("dag:paused:") == []
    finally:
        teardown()


async def test_the_lab_pause_needs_chaos_scope_and_is_gated_off_by_default(
    db_session: AsyncSession, default_tenant  # type: ignore[no-untyped-def]
) -> None:
    """ADR 0008's first two gates, on the new hook. The third principal that
    must not reach it is the one holding `actions:execute`: that is the operator
    path, and it already has `pause_dag`."""
    from app.mcp.standalone import create_mcp_app

    redis_stub = _RedisStub()
    plain = create_mcp_app()

    async def _override_db():  # type: ignore[no-untyped-def]
        yield db_session

    async def _override_redis():  # type: ignore[no-untyped-def]
        yield redis_stub

    plain.dependency_overrides[get_db] = _override_db
    plain.dependency_overrides[get_redis] = _override_redis
    async with AsyncClient(
        transport=ASGITransport(app=plain, raise_app_exceptions=False),
        base_url="http://test",
    ) as ac:
        token = await _token(
            db_session, default_tenant.id, [Scope.CHAOS_INVOKE.value]
        )
        body = await _call(
            ac, token, "pause_dag_chaos", {"root_job_id": str(uuid.uuid4())}
        )
    assert body["error"]["code"] == protocol.MCP_TOOL_NOT_FOUND

    app, teardown = _mcp_app_with_chaos_enabled(db_session, redis_stub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac:
            for scope in (Scope.INCIDENTS_READ, Scope.ACTIONS_EXECUTE):
                token = await _token(db_session, default_tenant.id, [scope.value])
                body = await _call(
                    ac,
                    token,
                    "pause_dag_chaos",
                    {"root_job_id": str(uuid.uuid4())},
                )
                assert body["error"]["code"] == protocol.MCP_FORBIDDEN, scope
    finally:
        teardown()
