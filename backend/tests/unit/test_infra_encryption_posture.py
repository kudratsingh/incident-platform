"""F2-15: the Redis encryption flags must be *on*, not merely mentioned.

This replaces a CI step that ran

    grep -q transit_encryption_enabled infra/elasticache.tf &&
    grep -q at_rest_encryption_enabled infra/elasticache.tf

which asserted the flag *names* appear somewhere in the file. Flipping either
to `false` leaves both names exactly where they were, so the one regression
the step existed to catch was the one it could not see. It would also have
passed on the names appearing in a comment, or on a second unencrypted
replication group added alongside the encrypted one.

Parsing the assigned value closes all three. The guard lives in the unit tier
rather than the `infra` CI job because that job has Terraform but no Python
environment, and the check is worth more than the shell one-liner it would
have to be to run there.
"""

from __future__ import annotations

from ._hcl import blocks, repo_root, top_attribute

#: Both flags, and what they have to be. `transit` is the reason this stack
#: uses a replication group at all — `aws_elasticache_cluster` cannot express
#: in-transit encryption for Redis, so a revert to the bare cluster resource
#: silently drops it while remaining valid HCL that `terraform validate` likes.
_REQUIRED_FLAGS = {
    "at_rest_encryption_enabled": "true",
    "transit_encryption_enabled": "true",
}

_ENCRYPTED_RESOURCE = "aws_elasticache_replication_group"

#: The resource type that cannot carry in-transit encryption. Its presence is
#: the revert this guard is watching for, so it is banned outright rather than
#: inspected.
_FORBIDDEN_RESOURCE = "aws_elasticache_cluster"


def _elasticache_blocks(kind: str) -> list[tuple[str, str]]:
    """(resource label, body) for every block of `kind` anywhere in infra/."""
    found = []
    for path in sorted((repo_root() / "infra").glob("*.tf")):
        for header, body, _, _ in blocks(
            path.read_text(), rf'resource\s+"{kind}"\s+"(\w+)"\s*(?=\{{)'
        ):
            found.append((header.group(1), body))
    return found


def test_the_scanner_found_the_replication_group() -> None:
    """Meta-guard: a guard that parsed nothing would pass silently."""
    assert _elasticache_blocks(_ENCRYPTED_RESOURCE), (
        f"no {_ENCRYPTED_RESOURCE} found in infra/ — either the scanner broke "
        "or Redis is no longer provisioned by this stack"
    )


def test_every_redis_replication_group_is_encrypted() -> None:
    """Both flags present *and* true, on every group, not just somewhere in the file."""
    failures = []

    for label, body in _elasticache_blocks(_ENCRYPTED_RESOURCE):
        for flag, expected in _REQUIRED_FLAGS.items():
            actual = top_attribute(body, flag)
            if actual is None:
                failures.append(
                    f"{_ENCRYPTED_RESOURCE}.{label}: {flag} is not set (the AWS "
                    f"default is off)"
                )
            elif actual != expected:
                failures.append(
                    f"{_ENCRYPTED_RESOURCE}.{label}: {flag} = {actual}, expected {expected}"
                )

    assert not failures, "Redis encryption posture regressed:\n  " + "\n  ".join(failures)


def test_no_bare_elasticache_cluster_resource() -> None:
    """The bare cluster resource cannot express in-transit encryption for Redis."""
    offenders = [label for label, _ in _elasticache_blocks(_FORBIDDEN_RESOURCE)]
    assert not offenders, (
        f"{_FORBIDDEN_RESOURCE} cannot carry transit_encryption_enabled for Redis; "
        f"use {_ENCRYPTED_RESOURCE}. Found: {offenders}"
    )
