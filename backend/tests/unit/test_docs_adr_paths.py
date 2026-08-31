"""Tripwire against ADRs citing files and directories that do not exist.

ADR 0008 named `backend/app/mcp/chaos_tools/` in both its Reversibility
section and its Pointers list for two releases. The chaos tools have
always lived at `backend/app/mcp/tools/chaos/`. A reader following the
ADR to the code found nothing and had no way to tell whether the
directory had been deleted or renamed.

The check is deliberately narrow so it stays useful rather than noisy:
only backtick-quoted tokens containing a `/` whose first segment is a
real top-level directory of this repo are treated as paths, and each is
resolved against both the repo root and `backend/` because ADRs use the
`app/...` spelling that is relative to the backend package.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ADR_DIR = _REPO_ROOT / "docs" / "ADR"

# Paths are resolved against both roots: ADRs freely mix repo-relative
# (`backend/app/config.py`) and backend-relative (`app/config.py`).
_SEARCH_ROOTS = (_REPO_ROOT, _REPO_ROOT / "backend")

# First segment must be one of these for a backtick token to count as a
# path at all. Keeps URLs, HTTP routes (`/api/v1/jobs`), Redis keys and
# `a/b` prose out of the check.
_PATH_ROOTS = frozenset(
    {
        "backend",
        "frontend",
        "infra",
        "docs",
        "scripts",
        "tests",
        "app",
        "alembic",
        ".github",
    }
)

# A trailing segment with a dot is only a file when the dot introduces a
# suffix we recognise. This is what keeps `app/core/rls_check.assert_rls_posture`
# — a module-qualified *symbol*, not a path — from being checked as a file.
_FILE_SUFFIXES = (
    ".py",
    ".ts",
    ".tsx",
    ".md",
    ".yml",
    ".yaml",
    ".tf",
    ".sh",
    ".json",
    ".toml",
    ".sql",
)

_BACKTICKED = re.compile(r"`([A-Za-z0-9_.*/-]*/[A-Za-z0-9_.*/-]*)`")


def _is_path_like(token: str) -> bool:
    if token.split("/")[0] not in _PATH_ROOTS:
        return False
    # Work-order specs live in the audit workspace, not in this repo.
    if token.startswith("docs/wave"):
        return False
    last = token.split("/")[-1]
    if "." in last and not last.endswith(_FILE_SUFFIXES):
        return False
    return True


def _cited_paths(adr: pathlib.Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(adr.read_text(encoding="utf-8").splitlines(), 1):
        for match in _BACKTICKED.finditer(line):
            token = match.group(1).rstrip("/")
            if _is_path_like(token):
                found.append((lineno, token))
    return found


def _exists(token: str) -> bool:
    # A glob (`backend/app/schemas/kafka/*.schema.json`) is satisfied by
    # any match; a literal path just has to be there.
    if "*" in token:
        parent, _, pattern = token.rpartition("/")
        return any(list((root / parent).glob(pattern)) for root in _SEARCH_ROOTS)
    return any((root / token).exists() for root in _SEARCH_ROOTS)


@pytest.mark.parametrize("adr", sorted(_ADR_DIR.glob("*.md")), ids=lambda p: p.name)
def test_every_path_cited_in_an_adr_exists(adr: pathlib.Path) -> None:
    missing = [
        f"{adr.name}:{lineno} cites `{token}`"
        for lineno, token in _cited_paths(adr)
        if not _exists(token)
    ]
    assert not missing, "ADR cites path(s) that do not exist:\n  " + "\n  ".join(missing)


def test_the_check_actually_finds_paths() -> None:
    """Guard against the extraction regex silently matching nothing and
    turning the parametrised test above into a no-op."""
    total = sum(len(_cited_paths(adr)) for adr in _ADR_DIR.glob("*.md"))
    assert total > 40, f"only {total} paths extracted from the ADR set"
