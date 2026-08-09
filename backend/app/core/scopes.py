"""
Machine-principal scopes.

Fixed enum, locked in during agent-platform Step 0 (see
`docs/ADR/0007-machine-principal-scope-model.md`). Scopes are
non-hierarchical, additive, and orthogonal to the human role enum:
holding `actions:execute` does *not* imply `chaos:invoke`, and no human
role grants any of them.

Adding a new scope is a decision; renaming or splitting one is a token
migration (existing tokens carry the string literal). So resist adding
scopes reactively — bundle capabilities into an existing scope where
the semantics fit.
"""

from enum import StrEnum


class Scope(StrEnum):
    TELEMETRY_READ = "telemetry:read"
    INCIDENTS_READ = "incidents:read"
    ACTIONS_PROPOSE = "actions:propose"
    ACTIONS_EXECUTE = "actions:execute"
    CHAOS_INVOKE = "chaos:invoke"


ALL_SCOPES: frozenset[str] = frozenset(s.value for s in Scope)

# Scopes a human may grant through the self-service admin API when the chaos
# gate is closed. `chaos:invoke` is excluded: with CHAOS_ENABLED=false it is
# provisioned only by the operator seed script through the service layer
# (scripts/seed_incident_commander.py). On chaos-enabled stacks a platform
# admin may still grant it via the API — see `assert_api_grantable` and the
# amendments to ADR 0007 / ADR 0008.
API_GRANTABLE_SCOPES: frozenset[str] = ALL_SCOPES - {Scope.CHAOS_INVOKE.value}


def validate_scopes(scopes: list[str]) -> list[str]:
    """Return the list unchanged if every entry is a known scope; else raise.

    Used at token mint and account create — we refuse to persist unknown
    scope strings rather than silently ignore them.
    """
    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope(s): {unknown}")
    return scopes


def assert_api_grantable(
    scopes: list[str] | None, *, chaos_enabled: bool = False
) -> None:
    """Raise ValueError if any requested scope must not be granted through
    the human API (X-01 hop 3).

    This is deliberately a SEPARATE check from `validate_scopes` (which only
    checks membership) and is called ONLY from the API endpoints, never from
    the service layer — the operator seed script provisions `chaos:invoke`
    through the service layer and must keep working.

    With the chaos gate open (`chaos_enabled=True`, never true in
    production per ADR 0008) every known scope is API-grantable, so a
    platform admin can still provision the agent's chaos:invoke account on
    demo/eval stacks. Unknown scopes are not this check's job — they fall
    through to `validate_scopes` in the service for the canonical error.
    """
    if not scopes or chaos_enabled:
        return
    refused = sorted(set(scopes) & (ALL_SCOPES - API_GRANTABLE_SCOPES))
    if refused:
        raise ValueError(
            f"Scope(s) not grantable through this API: {refused}. "
            "chaos:invoke is provisioned by the operator seed script, or by "
            "a platform admin only on a chaos-enabled stack (ADR 0008)."
        )
