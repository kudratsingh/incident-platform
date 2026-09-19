"""
Machine-principal scopes.

Fixed enum (ADR 0007): non-hierarchical, additive, orthogonal to the human role
enum — `actions:execute` does *not* imply `chaos:invoke`. Renaming or splitting a
scope is a token migration; tokens carry the string literal.
"""

from enum import StrEnum


class Scope(StrEnum):
    TELEMETRY_READ = "telemetry:read"
    INCIDENTS_READ = "incidents:read"
    ACTIONS_PROPOSE = "actions:propose"
    ACTIONS_EXECUTE = "actions:execute"
    CHAOS_INVOKE = "chaos:invoke"
    # Write-only, and the only scope whose holder is *denied* a read: the caller
    # reports what it is doing to the platform and cannot read it back (ADR 0035).
    AGENT_RUNS_WRITE = "agent_runs:write"


ALL_SCOPES: frozenset[str] = frozenset(s.value for s in Scope)

# Scopes a human may grant through the admin API. `chaos:invoke` is excluded:
# under CHAOS_ENABLED=false only scripts/seed_incident_commander.py grants it
# (ADR 0007 / ADR 0008).
API_GRANTABLE_SCOPES: frozenset[str] = ALL_SCOPES - {Scope.CHAOS_INVOKE.value}


def validate_scopes(scopes: list[str]) -> list[str]:
    """Return the list unchanged if every scope is known, else raise (never persist one)."""
    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope(s): {unknown}")
    return scopes


def assert_api_grantable(
    scopes: list[str] | None, *, chaos_enabled: bool = False
) -> None:
    """Raise ValueError if a requested scope must not be granted through the
    human API (X-01 hop 3).

    Separate from `validate_scopes`, and called ONLY from the API endpoints: the
    operator seed script provisions `chaos:invoke` through the service layer. With
    `chaos_enabled=True` (never production, ADR 0008) every known scope passes.
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
