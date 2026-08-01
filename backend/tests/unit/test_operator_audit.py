"""Unit tests for the operator-audit helper — schema of the
`agent.tool_invoked` row and translation of the Principal shape into
the audit-row identity fields."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.dependencies import Principal
from app.models.audit import (
    PRINCIPAL_TYPE_SERVICE_ACCOUNT,
    PRINCIPAL_TYPE_USER,
)
from app.models.service_account import ServiceAccount
from app.models.user import User
from app.services.operator_audit import (
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    TOOL_INVOKED_ACTION,
    record_tool_invocation,
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
