"""
A probe the lab makes on the agent's token is labelled by the lab — and the label needs
the lab's own credential (WO-R3-333, [ADR 0038]).

The evaluator has two callers that must wear the AGENT's token, because what that token
can and cannot do is the fact they exist to establish: the principal guards (a Tier-1
attempt that must be refused, a chaos attempt that must be refused) and the world audit
(reads taken exactly as the agent would take them). Every one of those calls wrote an
`agent.tool_invoked` row, and nothing downstream could tell them from the agent's own
work — so the third live take's action ledger showed seven reads the agent never made.

So `tools/call` accepts `_lab_probe` beside `arguments` (`protocol.LAB_PROBE_FIELD`) and
this module decides whether to honour it. Three rules, and the middle one is the point:

1. **The field is never in `arguments`.** A tool's input model never sees it, so
   `tools/list` is byte-identical and nothing the agent's planner reads can carry it.
2. **The label needs a second credential, `X-Lab-Principal`.** Without it the agent
   could relabel its own reads out of the ledger by adding one field — the audit trail
   would become something the subject under test writes. The credential is a bearer
   token for a principal holding `chaos:invoke` (the evaluator) or for the read-only
   smoke account (the traffic generator and the world audit, which hold no write scope
   at all).
3. **A field that cannot be honoured is refused, never ignored.** A silent ignore leaves
   the row labelled `agent.tool_invoked` — the exact mislabel this exists to remove —
   and the caller believes it was labelled. So the call does not run, the response is a
   JSON-RPC invalid-params error, and the attempt is audited in the agent's own stream.

**What the refusal may say.** The message names the field and the header and nothing
else. ADR 0012 rule 1 covers response bodies, and this response can reach the agent's
own client (it is the one principal that could send the field by accident), so the scope
that authorises the label is not printed on the wire — the rule is in ADR 0038 and in
the protocol section of `docs/ARCHITECTURE.md`, which the commander's builders read and
the agent cannot. `reason_code` says which half failed, in words that name no mechanism.

**Gated on `CHAOS_ENABLED` like everything else the lab owns** (ADR 0008). A production
deployment has no lab, so it must have no way to relabel an audit row: with the flag off
the field is refused whatever credential arrives. Every stack an eval or a demo runs
against has it on — the chaos tools are registered there — so this costs the evaluator
nothing and closes the surface everywhere else.
"""

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from app.config import Settings, get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.scopes import Scope
from app.mcp.protocol import LAB_PROBE_FIELD
from app.repositories.audit import AuditRepository
from app.repositories.service_account import (
    ServiceAccountRepository,
    ServiceAccountTokenRepository,
)
from app.services.service_account import ServiceAccountService
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

#: The second credential. A header rather than a param because it is authentication,
#: and because `params` is the half a tool's caller composes.
LAB_PRINCIPAL_HEADER: Final[str] = "X-Lab-Principal"

#: The reason string lands in `audit_logs.extra_data`, so it is bounded at the write
#: surface. Refused rather than truncated, as ADR 0037's excerpts are: a stored value the
#: caller did not write is worse than a refusal it can read.
LAB_PROBE_REASON_MAX_LENGTH: Final[int] = 200

#: Scopes that make a principal a writer. The smoke account holds none of them — that is
#: what "read-only" means here, and it is checked rather than assumed, so an account that
#: took the smoke account's name and grew a write scope is not a lab credential.
WRITE_SCOPES: Final[frozenset[str]] = frozenset(
    {
        Scope.ACTIONS_EXECUTE.value,
        Scope.ACTIONS_PROPOSE.value,
        Scope.AGENT_RUNS_WRITE.value,
    }
)

#: What every refusal says on the wire, whatever went wrong. One sentence, naming the
#: field, the header and the consequence — see the module docstring for why it names no
#: scope and no mechanism.
REFUSAL_MESSAGE: Final[str] = (
    f"{LAB_PROBE_FIELD} is only honoured on a request that also carries a valid "
    f"{LAB_PRINCIPAL_HEADER} credential authorised to label a call; this request did "
    "not, so nothing ran"
)

#: Which half failed. A closed set, on the wire in `data.reason_code` and in the audit
#: row, so a caller can fix the request without the platform explaining the rule.
ReasonCode = Literal[
    "not_available",
    "credential_missing",
    "credential_invalid",
    "credential_not_authorised",
    "reason_invalid",
]

#: What authorised the label. Recorded on the row so an operator reading a `lab.probe`
#: row knows which of the two credentials stood behind it.
LabProbeBasis = Literal["chaos_scope", "smoke_account"]


@dataclass(frozen=True)
class LabProbeLabel:
    """An honoured `_lab_probe`: the caller's reason, and who vouched for it."""

    reason: str
    principal_name: str
    basis: LabProbeBasis


class LabProbeRefused(Exception):
    """The field cannot be honoured, so the call must not run.

    `detail` is written to the audit row and the log. It is agent-readable — the refused
    call is audited in the agent's own stream — so it names no mechanism either.
    """

    def __init__(self, reason_code: ReasonCode, detail: str) -> None:
        self.reason_code: ReasonCode = reason_code
        self.detail = detail
        super().__init__(detail)

    @property
    def message(self) -> str:
        return REFUSAL_MESSAGE

    @property
    def audit_message(self) -> str:
        return f"{LAB_PROBE_FIELD} refused: {self.detail}"

    @property
    def error_data(self) -> dict[str, str]:
        return {"error_code": "lab_probe_refused", "reason_code": self.reason_code}


def _basis(
    *, name: str, scopes: frozenset[str], settings: Settings
) -> LabProbeBasis | None:
    """Which rule admits this principal, or None if neither does.

    Scope first: the evaluator's account is identified by what it holds, which is the
    durable fact. The smoke account has the agent account's scopes exactly (read-only,
    by design), so it cannot be told apart by scope and is matched by name — with the
    read-only claim re-checked, because a name is a weaker fact than a grant.
    """
    if Scope.CHAOS_INVOKE.value in scopes:
        return "chaos_scope"
    if name == settings.lab_probe_smoke_account_name and not (scopes & WRITE_SCOPES):
        return "smoke_account"
    return None


async def resolve_lab_probe(
    value: str,
    *,
    header: str | None,
    db: AsyncSession,
    caller_tenant_id: uuid.UUID,
    settings: Settings | None = None,
) -> LabProbeLabel:
    """Honour `_lab_probe`, or raise `LabProbeRefused`.

    Order matters, and it runs outward-in: what this deployment offers, then whether a
    credential arrived, then whether it is real, then whether it may label, and only
    then the shape of the caller's own reason string. A caller with no credential learns
    nothing about the reason rules, which is the same courtesy every scope check here
    already extends.

    The credential is verified, not adopted: no tenant context is applied and no
    contextvar moves, so the request stays the agent's request throughout. The only
    trace it leaves is `last_used_at` on the lab token, which is true and useful.
    """
    settings = settings or get_settings()

    if not settings.chaos_enabled:
        raise LabProbeRefused(
            "not_available", "this deployment does not accept a call label"
        )

    if not header or not header.strip():
        raise LabProbeRefused(
            "credential_missing", f"no {LAB_PRINCIPAL_HEADER} header on the request"
        )

    scheme, _, token = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise LabProbeRefused(
            "credential_invalid",
            f"{LAB_PRINCIPAL_HEADER} must be 'Bearer <token>'",
        )

    service = ServiceAccountService(
        ServiceAccountRepository(db),
        ServiceAccountTokenRepository(db),
        AuditRepository(db),
    )
    try:
        account, account_token = await service.verify_token(token.strip())
    except AppError:
        # Deliberately one reason code for every verification failure: an unknown,
        # revoked and expired token must look the same from outside.
        raise LabProbeRefused(
            "credential_invalid",
            f"the {LAB_PRINCIPAL_HEADER} credential was not accepted",
        ) from None

    scopes = frozenset(account_token.scopes)

    # Same tenant as the call it is labelling. A second tenant holding a copy of the
    # credential must not be able to label rows in this one (the WO-R2-18 shape).
    if account.tenant_id != caller_tenant_id:
        raise LabProbeRefused(
            "credential_not_authorised",
            f"the {LAB_PRINCIPAL_HEADER} credential belongs to another tenant",
        )

    basis = _basis(name=account.name, scopes=scopes, settings=settings)
    if basis is None:
        raise LabProbeRefused(
            "credential_not_authorised",
            f"the {LAB_PRINCIPAL_HEADER} credential may not label a call",
        )

    reason = value.strip()
    if not reason:
        raise LabProbeRefused(
            "reason_invalid", f"{LAB_PROBE_FIELD} must carry a short reason"
        )
    if len(reason) > LAB_PROBE_REASON_MAX_LENGTH:
        raise LabProbeRefused(
            "reason_invalid",
            f"{LAB_PROBE_FIELD} is at most {LAB_PROBE_REASON_MAX_LENGTH} characters, "
            f"got {len(reason)}",
        )

    logger.info(
        "lab probe honoured",
        extra={"lab_principal": account.name, "basis": basis},
    )
    return LabProbeLabel(reason=reason, principal_name=account.name, basis=basis)


__all__ = [
    "LAB_PRINCIPAL_HEADER",
    "LAB_PROBE_REASON_MAX_LENGTH",
    "REFUSAL_MESSAGE",
    "WRITE_SCOPES",
    "LabProbeBasis",
    "LabProbeLabel",
    "LabProbeRefused",
    "ReasonCode",
    "resolve_lab_probe",
]
