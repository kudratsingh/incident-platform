"""`_lab_probe` and the credential that makes it mean something (WO-R3-333, ADR 0038).

Three things are pinned here, and they are the three ways this feature could go wrong.

**The field is beside `arguments`, never inside it.** Inside, it would reach a tool's
input model and therefore `tools/list`, which is the contract the commander pins — and it
would be in the agent's prompt. So the envelope carries it and nothing else does; the last
test in this file asserts the advertised surface does not move.

**The label needs the lab's own credential.** An agent token alone cannot relabel its own
reads, or the audit trail would be something the subject under test writes.

**A field that cannot be honoured is refused, not ignored.** A silent ignore leaves the
row saying `agent.tool_invoked`, which is the mislabel the whole order exists to remove.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.core.exceptions import AuthenticationError
from app.mcp.handlers import handle_tools_list
from app.mcp.lab_probe import (
    LAB_PRINCIPAL_HEADER,
    LAB_PROBE_REASON_MAX_LENGTH,
    LabProbeRefused,
    resolve_lab_probe,
)
from app.mcp.protocol import LAB_PROBE_FIELD, ToolCallParams, ToolInfo

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

SMOKE_NAME = "incident-commander-smoke"
_TENANT = uuid.uuid4()


def _settings(**over: Any) -> Settings:
    fields: dict[str, Any] = {
        "chaos_enabled": True,
        "environment": "test",
        "lab_probe_smoke_account_name": SMOKE_NAME,
    }
    fields.update(over)
    return Settings(**fields)


class _Account:
    def __init__(self, name: str, tenant_id: uuid.UUID) -> None:
        self.name = name
        self.tenant_id = tenant_id


class _Token:
    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes


def _service(
    *,
    name: str = "incident-commander-chaos",
    scopes: list[str] | None = None,
    tenant_id: uuid.UUID | None = None,
    raises: Exception | None = None,
) -> Any:
    """Patch in a token verifier. The real one is exercised by the API tier; here the
    question is what the rule does with the principal it gets back."""
    service = MagicMock()
    if raises is not None:
        service.verify_token = AsyncMock(side_effect=raises)
    else:
        service.verify_token = AsyncMock(
            return_value=(
                _Account(name, tenant_id or _TENANT),
                _Token(scopes if scopes is not None else ["chaos:invoke"]),
            )
        )
    return patch("app.mcp.lab_probe.ServiceAccountService", return_value=service)


async def _resolve(
    value: str = "principal guard probe",
    *,
    header: str | None = "Bearer sa_lab",
    settings: Settings | None = None,
    caller_tenant_id: uuid.UUID | None = None,
) -> Any:
    return await resolve_lab_probe(
        value,
        header=header,
        db=MagicMock(),
        caller_tenant_id=caller_tenant_id or _TENANT,
        settings=settings or _settings(),
    )


# ---------------------------------------------------------------------------
# The envelope: where the field lives
# ---------------------------------------------------------------------------


def test_the_field_is_read_from_params_and_stays_out_of_arguments() -> None:
    params = ToolCallParams.model_validate(
        {
            "name": "get_consumer_lag",
            "arguments": {"consumer_group": "worker-dispatcher"},
            LAB_PROBE_FIELD: "world audit read",
        }
    )

    assert params.lab_probe == "world audit read"
    assert params.arguments == {"consumer_group": "worker-dispatcher"}
    assert LAB_PROBE_FIELD not in params.arguments


def test_the_field_inside_arguments_is_just_an_argument() -> None:
    """Which is the point: it reaches the tool's input model, where `extra="forbid"`
    refuses it, and it never reaches the label."""
    params = ToolCallParams.model_validate(
        {
            "name": "list_audit_events",
            "arguments": {LAB_PROBE_FIELD: "sneaky"},
        }
    )

    assert params.lab_probe is None
    assert params.arguments == {LAB_PROBE_FIELD: "sneaky"}


def test_the_underscore_spelling_is_the_only_spelling() -> None:
    """`lab_probe` without the underscore is an unknown key, not a second name for the
    field: one wire spelling, so there is one thing to grep for and one thing to test."""
    params = ToolCallParams.model_validate(
        {"name": "list_audit_events", "arguments": {}, "lab_probe": "no"}
    )

    assert params.lab_probe is None


def test_the_alias_literal_and_the_shared_constant_are_the_same_string() -> None:
    """mypy requires a literal alias on the model, so the wire name exists twice. This is
    what stops the two drifting — the refusal messages and the commander's request shape
    are both built from the constant."""
    assert ToolCallParams.model_fields["lab_probe"].alias == LAB_PROBE_FIELD


def test_a_call_without_the_field_carries_none() -> None:
    params = ToolCallParams.model_validate(
        {"name": "list_audit_events", "arguments": {}}
    )

    assert params.lab_probe is None


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


async def test_a_chaos_scoped_credential_may_label() -> None:
    with _service(scopes=["telemetry:read", "chaos:invoke"]):
        label = await _resolve("guard: tier-1 must be refused")

    assert label.basis == "chaos_scope"
    assert label.reason == "guard: tier-1 must be refused"
    assert label.principal_name == "incident-commander-chaos"


async def test_the_read_only_smoke_account_may_label() -> None:
    """The world audit's credential. It cannot be told apart by scope — it holds the
    agent's scopes exactly, which is the point of it — so it is matched by name."""
    with _service(name=SMOKE_NAME, scopes=["telemetry:read", "incidents:read"]):
        label = await _resolve("world audit read")

    assert label.basis == "smoke_account"
    assert label.principal_name == SMOKE_NAME


async def test_an_agent_credential_may_not_label() -> None:
    """The one that matters: the agent's own token in the header buys nothing, so the
    agent cannot relabel its own reads out of the ledger."""
    with _service(name="incident-commander", scopes=["telemetry:read", "actions:execute"]):
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve()

    assert exc.value.reason_code == "credential_not_authorised"


async def test_the_smoke_name_with_a_write_scope_may_not_label() -> None:
    """A name is a weaker fact than a grant, so the read-only half is checked rather
    than assumed: an account that took this name and grew a write scope is not it."""
    with _service(name=SMOKE_NAME, scopes=["telemetry:read", "actions:execute"]):
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve()

    assert exc.value.reason_code == "credential_not_authorised"


async def test_a_credential_from_another_tenant_may_not_label() -> None:
    """The row is written in the caller's tenant; a second tenant holding a copy of the
    credential must not be able to label rows in this one (the WO-R2-18 shape)."""
    with _service(scopes=["chaos:invoke"], tenant_id=uuid.uuid4()):
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve()

    assert exc.value.reason_code == "credential_not_authorised"


async def test_no_header_is_refused_as_a_missing_credential() -> None:
    with pytest.raises(LabProbeRefused) as exc:
        await _resolve(header=None)

    assert exc.value.reason_code == "credential_missing"
    assert LAB_PRINCIPAL_HEADER in exc.value.message
    assert LAB_PROBE_FIELD in exc.value.message


@pytest.mark.parametrize("header", ["sa_lab", "Basic sa_lab", "Bearer ", "Bearer"])
async def test_a_header_that_is_not_a_bearer_token_is_refused(header: str) -> None:
    with pytest.raises(LabProbeRefused) as exc:
        await _resolve(header=header)

    assert exc.value.reason_code == "credential_invalid"


async def test_every_verification_failure_looks_the_same() -> None:
    """Unknown, revoked and expired are one reason code on purpose: which of them it was
    is not a fact this surface owes the caller."""
    with _service(raises=AuthenticationError("Token has been revoked")):
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve()

    assert exc.value.reason_code == "credential_invalid"


async def test_the_reason_string_is_required() -> None:
    with _service():
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve("   ")

    assert exc.value.reason_code == "reason_invalid"


async def test_an_over_long_reason_is_refused_not_truncated() -> None:
    """Same rule as ADR 0037's excerpts: the value lands in `audit_logs.extra_data`, and
    a stored value the caller did not write is worse than a refusal it can read."""
    with _service():
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve("x" * (LAB_PROBE_REASON_MAX_LENGTH + 1))

    assert exc.value.reason_code == "reason_invalid"

    with _service():
        label = await _resolve("x" * LAB_PROBE_REASON_MAX_LENGTH)
    assert len(label.reason) == LAB_PROBE_REASON_MAX_LENGTH


async def test_a_deployment_without_the_lab_refuses_every_label() -> None:
    """ADR 0008's flag applied to the one other surface the lab owns: production has no
    lab, so it must have no way to relabel an audit row — whatever credential arrives."""
    with _service(scopes=["chaos:invoke"]):
        with pytest.raises(LabProbeRefused) as exc:
            await _resolve(settings=_settings(chaos_enabled=False))

    assert exc.value.reason_code == "not_available"


def test_the_refusal_names_the_field_and_no_mechanism() -> None:
    """The refusal can reach the agent's own client — it is the one principal that could
    send the field by accident — and ADR 0012 rule 1 covers response bodies. So it names
    the field and the header, and never the scope that would have authorised it."""
    refusal = LabProbeRefused("credential_not_authorised", "not authorised to label")
    wire = json.dumps([refusal.message, refusal.error_data, refusal.audit_message])

    assert LAB_PROBE_FIELD in wire
    assert LAB_PRINCIPAL_HEADER in wire
    for word in ("chaos", "smoke", "evaluator", "eval"):
        assert word not in wire.lower(), f"the refusal names the lab: {word}"


# ---------------------------------------------------------------------------
# The advertised surface does not move
# ---------------------------------------------------------------------------


def test_the_tools_list_surface_says_nothing_about_any_of_this() -> None:
    """`tools/list` is byte-identical to the release before this one: the field is on the
    `tools/call` envelope, which `tools/list` does not describe, and no tool's name,
    description or schema moved. So there is nothing to rebless."""
    body = handle_tools_list("1")
    assert body.result is not None
    serialized = json.dumps(body.result)

    for token in (LAB_PROBE_FIELD, LAB_PRINCIPAL_HEADER, "lab.probe", "lab_probe"):
        assert token not in serialized, f"{token} reached the advertised surface"


def test_each_tools_list_entry_still_carries_the_same_six_fields() -> None:
    """The other half of "byte-identical": the entry shape is pinned, so a later author
    cannot advertise the label by adding a field here either."""
    assert set(ToolInfo.model_fields) == {
        "name",
        "description",
        "inputSchema",
        "outputSchema",
        "required_scope",
        "is_idempotent",
    }


# ---------------------------------------------------------------------------
# The written record — the commander re-pins and the console is built off these
# ---------------------------------------------------------------------------


def test_adr_0038_exists_and_is_indexed() -> None:
    adr = _REPO_ROOT / "docs" / "ADR" / "0038-a-probe-by-the-lab-is-labelled-by-the-lab.md"
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")

    assert adr.is_file(), "ADR 0038 is missing"
    assert adr.name in index, "ADR 0038 is not in docs/ADR/README.md"


def test_adr_0012_records_the_amendment() -> None:
    """This is an amendment to ADR 0012, and that ADR is where a reader looks for every
    rule about what the agent may see."""
    text = (
        _REPO_ROOT / "docs" / "ADR" / "0012-the-lab-is-invisible-to-the-agent.md"
    ).read_text(encoding="utf-8")

    assert "WO-R3-333" in text
    assert LAB_PROBE_FIELD in text
    assert LAB_PRINCIPAL_HEADER in text


def test_the_rebless_ledger_names_this_delta() -> None:
    """The commander re-pins off that paragraph, and this batch's headline is that there
    is nothing in `tools/list` to rebless — which has to be *said*, or the next re-pin
    goes looking for the delta it cannot find."""
    ledger = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    assert "WO-R3-333" in ledger
    for token in (
        LAB_PROBE_FIELD,
        LAB_PRINCIPAL_HEADER,
        "lab.probe",
        "lab_probe_refused",
        "lag_samples_cleared",
    ):
        assert token in ledger, token


def test_the_request_shape_is_documented_where_a_caller_will_look() -> None:
    """The other repository writes the request against this section, not against the
    diff — so the field, the header, every reason code and the placement rule are here."""
    architecture = (_REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert LAB_PROBE_FIELD in architecture
    assert LAB_PRINCIPAL_HEADER in architecture
    for reason in (
        "not_available",
        "credential_missing",
        "credential_invalid",
        "credential_not_authorised",
        "reason_invalid",
    ):
        assert reason in architecture, reason
