"""Shared safety gate for every script under `scripts/` that writes
credentials, seeds fixtures, or destroys state.

## Why the ENVIRONMENT label was never the gate

`reset_eval_state._assert_not_production()` read the *local process's*
`ENVIRONMENT` setting and nothing else, while every destructive
statement in that script runs against the `database_url` / `redis_url`
the caller hands `reset()`. Those are two unrelated facts. An operator
sitting in a `development` shell — which is the normal way anyone
invokes these scripts — passes the gate unconditionally, and then the
`DELETE FROM jobs` lands on whatever DSN happens to be in the
environment. The label describes the shell; the DSN chooses the victim
(WO-R2-18).

So the gate here is about the **target**, and it is two independent
checks, in this order:

  1. `ENVIRONMENT=production` refuses outright. Unchanged, and kept
     first so its message is the one an operator sees in the worst
     case. This is the "braces" of ADR 0008's belt-and-braces.
  2. The target DSN must be the one this deployment is configured for
     (`settings.database_url` / `settings.redis_url`). Anything else
     refuses unless the caller passes an explicit override. A script
     pointed at a database its own config has never heard of is the
     definition of "destroying something you did not mean to".

Check 2 is what makes the guardrail true for library callers. `reset()`
is exported and takes arbitrary URLs; before this, its only gate could
be satisfied by a shell variable while the URLs pointed anywhere.

## What "the same target" means

Identity is `(backend scheme, host, port, database)`:

  * **Driver suffix stripped** — `postgresql://` and
    `postgresql+asyncpg://` are the same server. Refusing on the driver
    would be a false alarm on every script that swaps the async driver
    in, which is all of them.
  * **Credentials excluded** — a rotated password, or connecting as a
    different role, still points at the same rows. This is not a
    hypothetical here: since WO-P2-03 this repo runs a **two-URL
    scheme**, where the runtime `DATABASE_URL` is the non-owner
    `incident_app` role and the owner URL is `postgres:postgres`
    (CLAUDE.md, "the two-URL scheme"; ADR 0015). Those two DSNs differ
    only in their credentials and name the same database. A gate that
    compared usernames would refuse every script run through the owner
    URL — which is how ad-hoc ops work is done — and the fix for that
    would be an `--i-know-what-im-doing` baked into the Makefile, at
    which point there is no gate left.
  * **Query string excluded** — `?sslmode=require` does not change
    which database you are about to empty.
  * **Port defaulted** per scheme, so `localhost` and `localhost:5432`
    do not read as different servers.

The residual gap is deliberate and worth naming: two databases behind
the same host/port/name — a restored snapshot swapped underneath, say —
are indistinguishable from here. Closing that needs a sentinel row
inside the target, which is a schema change and a separate order.

## Escape hatch

One lever, explicit, per invocation: `allow_target_mismatch=True`
programmatically, `--i-know-what-im-doing` on the CLI. There is no
environment variable for it on purpose — an envvar is exactly the kind
of ambient state that let the original gate be satisfied by accident.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

# Default ports, so an omitted port doesn't read as a different server.
_DEFAULT_PORTS = {"postgresql": 5432, "postgres": 5432, "mysql": 3306, "redis": 6379}

# Scheme aliases that name the same backend.
_SCHEME_ALIASES = {"postgres": "postgresql"}


def _identity(url: str) -> tuple[str, str, int | None, str]:
    """`(scheme, host, port, database)` for a DSN, normalised so that
    cosmetic differences don't read as a different target.

    Driver suffix, credentials and query string are all dropped — see
    the module docstring for why each one is noise rather than signal."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.split("+", 1)[0].lower()
    scheme = _SCHEME_ALIASES.get(scheme, scheme)
    host = (parts.hostname or "").lower()
    port = parts.port or _DEFAULT_PORTS.get(scheme)
    # `/incident_platform` -> `incident_platform`; for sqlite the path
    # *is* the database file, and for redis it is the numeric db index.
    database = parts.path.lstrip("/")
    return (scheme, host, port, database)


def redact(url: str) -> str:
    """A DSN safe to print: password replaced, everything else intact.

    Operators need to see *which* host they are about to write to
    (WO-R2-19), and these scripts print to CI logs and eval reports."""
    parts = urlsplit(url.strip())
    if parts.password is None:
        return url
    netloc = parts.hostname or ""
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def describe_target(database_url: str, redis_url: str | None = None) -> str:
    """One-line, password-free description of what a script is about to
    write to. Printed as the opening line of every seeder."""
    described = f"target database: {redact(database_url)}"
    if redis_url is not None:
        described += f"\ntarget redis:    {redact(redis_url)}"
    return described


def assert_safe_target(
    *,
    script: str,
    database_url: str,
    redis_url: str | None = None,
    allow_target_mismatch: bool = False,
) -> None:
    """Raise `RuntimeError` unless it is safe to write to this target.

    Call before constructing an engine or a Redis client — the whole
    point is that nothing is connected, let alone mutated, when this
    refuses. `script` names the caller so the message says which script
    stopped.

    Two checks, production label first (see module docstring). The
    override only relaxes the second: `ENVIRONMENT=production` has no
    override here, because overriding `ENVIRONMENT` is itself the
    documented escape hatch for that one and a second lever on the same
    invariant is how gates get worn away."""
    from app.config import get_settings

    settings = get_settings()

    env = settings.environment
    if env == "production":
        raise RuntimeError(
            f"{script} refuses to run in production "
            f"(ENVIRONMENT={env!r}). If this is a real production-parity "
            "eval env, override ENVIRONMENT before invoking."
        )

    if allow_target_mismatch:
        return

    configured: list[tuple[str, str, str]] = [
        ("database_url", database_url, str(settings.database_url))
    ]
    if redis_url is not None:
        configured.append(("redis_url", redis_url, str(settings.redis_url)))

    for label, given, expected in configured:
        if _identity(given) != _identity(expected):
            raise RuntimeError(
                f"{script} refuses to run against a {label} that is not the "
                f"configured one.\n"
                f"  requested:  {redact(given)}\n"
                f"  configured: {redact(expected)}\n"
                "This script destroys or overwrites data at the target it is "
                "given, so the target — not the ENVIRONMENT label — is what "
                "is checked. Point the process at the stack you mean (set "
                "DATABASE_URL/REDIS_URL to match), or pass "
                "--i-know-what-im-doing (CLI) / allow_target_mismatch=True "
                "(library) if the mismatch is deliberate."
            )


def refuse_unsafe_target(
    *,
    script: str,
    database_url: str,
    redis_url: str | None = None,
    allow_target_mismatch: bool = False,
) -> None:
    """CLI wrapper around `assert_safe_target`: loud message on stderr,
    exit code 1 — the shape a `make` target and CI can act on."""
    try:
        assert_safe_target(
            script=script,
            database_url=database_url,
            redis_url=redis_url,
            allow_target_mismatch=allow_target_mismatch,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
