"""Unit tests for the operator-audit helper — schema of the
`agent.tool_invoked` row and translation of the Principal shape into
the audit-row identity fields.

Since WO-R3-327 it also covers the `lab.` stream: the `lab.world_reset` row the
environment reset appends, and the withholding that keeps it off the agent's own
read surface beside `chaos.` (ADR 0012, 2026-09-20 amendment).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.core.scopes import Scope
from app.dependencies import Principal
from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
)
from app.models.service_account import ServiceAccount
from app.models.user import User
from app.services.operator_audit import (
    CHAOS_ACTION_PREFIX,
    LAB_ACTION_PREFIX,
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    TOOL_INVOKED_ACTION,
    WORLD_RESET_ACTION,
    hidden_audit_action_prefixes,
    record_tool_invocation,
    record_world_reset,
)


def _mock_audit_repo() -> AsyncMock:
    """AuditRepository double whose `.session.begin_nested()` behaves as an
    async context manager. `AsyncMock` alone doesn't — sync methods that
    return an async CM need MagicMock (which auto-implements `__aenter__` /
    `__aexit__` since 3.8)."""
    mock = AsyncMock()
    mock.session = MagicMock()
    return mock


def _sa_principal() -> Principal:
    sa = ServiceAccount()
    sa.id = uuid.uuid4()
    sa.tenant_id = uuid.uuid4()
    sa.name = "incident-commander"
    sa.scopes = ["telemetry:read"]
    sa.is_active = True
    return Principal(
        kind="service_account",
        tenant_id=sa.tenant_id,
        service_account=sa,
        scopes=frozenset({"telemetry:read"}),
    )


def _user_principal() -> Principal:
    user = User()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.email = "op@example.com"
    return Principal(kind="user", tenant_id=user.tenant_id, user=user)


async def test_records_success_row_for_service_account() -> None:
    repo = _mock_audit_repo()
    principal = _sa_principal()
    await record_tool_invocation(
        repo,
        principal=principal,
        tool_name="get_consumer_lag",
        arguments={"group": "worker-dispatcher"},
        scope_used="telemetry:read",
        latency_ms=12.4567,
        outcome=OUTCOME_SUCCESS,
        request_id="req-abc",
    )
    repo.log.assert_awaited_once()
    args, kwargs = repo.log.call_args
    assert args[0] == TOOL_INVOKED_ACTION
    assert kwargs["tenant_id"] == principal.tenant_id
    assert kwargs["principal_type"] == PRINCIPAL_TYPE_SERVICE_ACCOUNT
    assert kwargs["principal_id"] == principal.id
    assert kwargs["user_id"] is None
    assert kwargs["resource_type"] == "mcp_tool"
    assert kwargs["resource_id"] == "get_consumer_lag"
    assert kwargs["request_id"] == "req-abc"
    extra = kwargs["extra_data"]
    assert extra["tool_name"] == "get_consumer_lag"
    assert extra["arguments"] == {"group": "worker-dispatcher"}
    assert extra["scope_used"] == "telemetry:read"
    assert extra["latency_ms"] == 12.457  # rounded to 3 dp
    assert extra["outcome"] == OUTCOME_SUCCESS
    assert "error_message" not in extra


async def test_records_error_row_carries_message() -> None:
    repo = _mock_audit_repo()
    await record_tool_invocation(
        repo,
        principal=_sa_principal(),
        tool_name="get_consumer_lag",
        arguments={},
        scope_used="telemetry:read",
        latency_ms=3.0,
        outcome=OUTCOME_ERROR,
        error_message="redis timeout",
    )
    extra = repo.log.call_args.kwargs["extra_data"]
    assert extra["outcome"] == OUTCOME_ERROR
    assert extra["error_message"] == "redis timeout"


async def test_records_row_for_human_principal_when_present() -> None:
    """The helper accepts either principal shape — humans get user path."""
    repo = _mock_audit_repo()
    principal = _user_principal()
    await record_tool_invocation(
        repo,
        principal=principal,
        tool_name="list_dlq_messages",
        arguments=None,
        scope_used=None,
        latency_ms=1.0,
        outcome=OUTCOME_SUCCESS,
    )
    kwargs = repo.log.call_args.kwargs
    assert kwargs["principal_type"] == PRINCIPAL_TYPE_USER
    assert kwargs["principal_id"] == principal.id
    assert kwargs["user_id"] == principal.id
    # None arguments becomes empty dict for a stable schema
    assert kwargs["extra_data"]["arguments"] == {}


async def test_none_scope_serialized_verbatim() -> None:
    """Some tools (e.g. tools/list) run without a scope requirement — the
    audit row records that as an explicit null rather than dropping the
    field."""
    repo = _mock_audit_repo()
    await record_tool_invocation(
        repo,
        principal=_sa_principal(),
        tool_name="tools/list",
        arguments={},
        scope_used=None,
        latency_ms=0.5,
        outcome=OUTCOME_SUCCESS,
    )
    extra = repo.log.call_args.kwargs["extra_data"]
    assert extra["scope_used"] is None


async def test_failing_audit_insert_does_not_propagate() -> None:
    """SAVEPOINT contract (#6): a failing audit insert (FK drift, constraint
    bug — the replay_job/#70 class) must not surface to the caller. The
    tool's own success response has to survive an audit-side crash."""
    repo = _mock_audit_repo()
    repo.log.side_effect = RuntimeError("audit table constraint violation")
    # Must not raise — the caller depends on record_tool_invocation being
    # a best-effort side effect, per the module docstring's contract.
    await record_tool_invocation(
        repo,
        principal=_sa_principal(),
        tool_name="get_consumer_lag",
        arguments={"group": "worker-dispatcher"},
        scope_used="telemetry:read",
        latency_ms=1.0,
        outcome=OUTCOME_SUCCESS,
    )
    repo.log.assert_awaited_once()


# ---------------------------------------------------------------------------
# `lab.world_reset` — the boundary row (WO-R3-327)
# ---------------------------------------------------------------------------


def _scoped_principal(*scopes: Scope) -> Principal:
    return Principal(
        kind="service_account",
        tenant_id=uuid.uuid4(),
        service_account=ServiceAccount(
            id=uuid.uuid4(), tenant_id=uuid.uuid4(), name="p", scopes=[]
        ),
        scopes=frozenset(s.value for s in scopes),
    )


def test_the_world_reset_action_lives_under_its_own_prefix() -> None:
    """Not `chaos.`: the console reads the newest `chaos.` row as the FAULT, and a
    reset filed under that prefix would be read as one."""
    assert WORLD_RESET_ACTION == "lab.world_reset"
    assert WORLD_RESET_ACTION.startswith(LAB_ACTION_PREFIX)
    assert not WORLD_RESET_ACTION.startswith(CHAOS_ACTION_PREFIX)


def test_the_lab_stream_is_withheld_from_a_principal_without_the_chaos_scope() -> None:
    """THE assertion. The payload is the reset's own counters — `chaos_keys_cleared`,
    `seeded_dlq_deleted`, `hot_set_reseeded` — so a readable row tells the agent under
    test that it is under test, and with what apparatus (ADR 0012)."""
    agent = _scoped_principal(Scope.TELEMETRY_READ, Scope.INCIDENTS_READ)

    hidden = hidden_audit_action_prefixes(agent)

    assert LAB_ACTION_PREFIX in hidden
    assert CHAOS_ACTION_PREFIX in hidden


def test_the_evaluator_reads_the_lab_stream() -> None:
    """Whoever may fire the lab may read the lab — the same condition the `chaos.`
    rule carries, because the reset is the evaluator's own act."""
    evaluator = _scoped_principal(
        Scope.TELEMETRY_READ, Scope.INCIDENTS_READ, Scope.CHAOS_INVOKE
    )

    assert hidden_audit_action_prefixes(evaluator) == ()


def test_the_lab_withholding_does_not_disturb_the_run_report_rule() -> None:
    """The two rules point opposite ways and must stay independent: a reporter with
    no chaos scope loses `chaos.` and `lab.` AND its own report stream."""
    reporter = _scoped_principal(Scope.TELEMETRY_READ, Scope.AGENT_RUNS_WRITE)

    hidden = hidden_audit_action_prefixes(reporter)

    assert CHAOS_ACTION_PREFIX in hidden
    assert LAB_ACTION_PREFIX in hidden
    assert "agent.run_reported" in hidden


async def test_record_world_reset_writes_one_row_carrying_the_counters() -> None:
    repo = _mock_audit_repo()
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    counters = {"chaos_keys_cleared": 3, "dlq_reset": 5, "hot_set_reseeded": 1}

    await record_world_reset(
        repo,
        tenant_id=tenant_id,
        principal_id=principal_id,
        counters=counters,
    )

    repo.log.assert_awaited_once()
    args, kwargs = repo.log.call_args
    assert args[0] == WORLD_RESET_ACTION
    assert kwargs["tenant_id"] == tenant_id
    assert kwargs["principal_type"] == PRINCIPAL_TYPE_SERVICE_ACCOUNT
    assert kwargs["principal_id"] == principal_id
    # No human did this, and naming one would be a lie about who did.
    assert kwargs["user_id"] is None
    assert kwargs["extra_data"] == counters


async def test_record_world_reset_accepts_an_unnamed_machine_principal() -> None:
    """A stack whose evaluator account has not been seeded yet still gets a boundary:
    the row is worth more than the attribution, and `principal_id` is nullable (ADR
    0007) precisely so a missing principal does not block an audit write."""
    repo = _mock_audit_repo()

    await record_world_reset(
        repo,
        tenant_id=uuid.uuid4(),
        principal_id=None,
        counters={},
    )

    kwargs = repo.log.call_args.kwargs
    assert kwargs["principal_type"] == PRINCIPAL_TYPE_SERVICE_ACCOUNT
    assert kwargs["principal_id"] is None


async def test_a_failed_boundary_write_is_loud() -> None:
    """The opposite contract to `record_tool_invocation`. There is no response to
    protect here, and a reset whose boundary was not recorded is a reset the console
    reads as the previous take — so the caller must hear about it."""
    repo = _mock_audit_repo()
    repo.log.side_effect = RuntimeError("audit insert failed")

    try:
        await record_world_reset(
            repo, tenant_id=uuid.uuid4(), principal_id=None, counters={}
        )
    except RuntimeError:
        pass
    else:  # pragma: no cover - the assertion is the failure path
        raise AssertionError("record_world_reset must not swallow a failed write")
