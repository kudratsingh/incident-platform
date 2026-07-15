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


def validate_scopes(scopes: list[str]) -> list[str]:
    """Return the list unchanged if every entry is a known scope; else raise.

    Used at token mint and account create — we refuse to persist unknown
    scope strings rather than silently ignore them.
    """
    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope(s): {unknown}")
    return scopes
