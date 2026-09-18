"""Shared safety gate for every script under `scripts/` that writes or destroys state.

Two checks, production label first (ADR 0008), then the target DSN must be the one `settings`
names — the `ENVIRONMENT` label describes the shell, the DSN chooses the victim (WO-R2-18).
Identity is `(scheme, host, port, database)`, so the two-URL scheme (ADR 0015) is not a mismatch.
One escape hatch per invocation: `allow_target_mismatch=True` / `--i-know-what-im-doing`, never
an env var, because ambient state is what let the original gate be satisfied by accident.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

# Default ports, so an omitted port doesn't read as a different server.
_DEFAULT_PORTS = {"postgresql": 5432, "postgres": 5432, "mysql": 3306, "redis": 6379}

# Scheme aliases that name the same backend.
_SCHEME_ALIASES = {"postgres": "postgresql"}


def _identity(url: str) -> tuple[str, str, int | None, str]:
    """`(scheme, host, port, database)` for a DSN, ignoring driver suffix,
    credentials and query string."""
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
    """A DSN safe to print: password replaced, host intact (WO-R2-19)."""
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
    """Password-free target line, printed before a seeder writes."""
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

    Call before building an engine or Redis client — nothing is connected when this refuses.
    `allow_target_mismatch` relaxes only the DSN check; `ENVIRONMENT=production` has none."""
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
    """CLI wrapper on `assert_safe_target`: stderr message, exit 1."""
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
