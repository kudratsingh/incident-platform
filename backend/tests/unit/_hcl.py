"""A small, shared HCL scanner for the Terraform tripwire tests.

Not a Terraform parser: it reads enough of `infra/*.tf` to answer "does this
block exist and what is this scalar set to", which is all the guards in
`test_alarm_metrics_match_emitters.py` and `test_runbook_lint.py` need. Both
files grew the same brace-matcher independently, which is how the two of them
could have disagreed about what the stack contains; there is one copy now.

Deliberately not `python-hcl2`: this runs in the unit tier, which has no
Terraform toolchain and no network, and the guards need to survive a syntax
error in a .tf file by failing loudly rather than by not running at all.
"""

from __future__ import annotations

import re
from pathlib import Path


def repo_root() -> Path:
    """Locate the repo root by walking up from this file until Dockerfile is found."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "Dockerfile").is_file():
            return parent
    raise AssertionError("no Dockerfile found in any parent of this file")


def match_brace(text: str, open_index: int) -> int:
    """Index just past the `}` closing the `{` at `open_index`.

    String-aware and comment-aware: SEARCH expressions embed literal braces
    (`'{IncidentPlatform,JobType} MetricName=...'`) and a naive depth counter
    walks straight off the end of the file on them.
    """
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif char == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced braces from offset {open_index}")


def blocks(text: str, pattern: str) -> list[tuple[re.Match[str], str, int, int]]:
    """Every block whose header matches `pattern`, as (header, body, start, end)."""
    found = []
    for header in re.finditer(pattern, text, flags=re.MULTILINE):
        open_index = text.index("{", header.end() - 1)
        end = match_brace(text, open_index)
        found.append((header, text[open_index + 1 : end - 1], open_index, end))
    return found


def scalar(body: str, key: str) -> str | None:
    """Value of a top-level `key = "value"` assignment, still backslash-escaped.

    The string body must tolerate `\\"` — the SEARCH expressions embed quoted
    metric names — so a plain `[^"]*` would truncate at the first inner quote
    and silently yield no match at all.
    """
    match = re.search(
        rf'^\s*{key}\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', body, flags=re.MULTILINE
    )
    return match.group(1) if match else None


def has_key(body: str, key: str) -> bool:
    return re.search(rf"^\s*{key}\s*=", body, flags=re.MULTILINE) is not None


def excise(body: str, spans: list[tuple[int, int]]) -> str:
    """Body with the given [start, end) spans blanked out, offsets preserved."""
    chars = list(body)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def strip_nested(body: str) -> str:
    """Body with every nested `{...}` block blanked out, offsets preserved.

    Without this, a line-anchored search for `name` inside
    `resource "aws_ecs_cluster"` finds the `name = "containerInsights"` of the
    nested `setting` block instead of the cluster's own — a wrong answer that
    still looks like a resource name, which is the worst kind.
    """
    spans = []
    i = 0
    n = len(body)
    while i < n:
        char = body[i]
        if char == '"':
            i += 1
            while i < n and body[i] != '"':
                i += 2 if body[i] == "\\" else 1
        elif char == "#":
            while i < n and body[i] != "\n":
                i += 1
            continue
        elif char == "{":
            end = match_brace(body, i)
            spans.append((i, end))
            i = end
            continue
        i += 1
    return excise(body, spans)


#: A value assigned at the top level of a block: either a quoted string or a
#: bare expression (`var.app_name`), up to an end-of-line comment. Requires a
#: non-space first character so a blanked-out nested block never matches.
_ASSIGNMENT = r'^\s*{key}\s*=\s*([^\s#][^\n#]*?)\s*$'


def top_attribute(body: str, key: str) -> str | None:
    """Value of `key = ...` at the top level of `body`, quotes stripped.

    Unlike `scalar`, this accepts unquoted values — Terraform writes
    `name = var.app_name` as often as it writes a string literal, and a
    scanner that silently skips the unquoted half reports a resource as
    nameless rather than as named-by-a-variable.
    """
    match = re.search(
        _ASSIGNMENT.format(key=key), strip_nested(body), flags=re.MULTILINE
    )
    if match is None:
        return None
    value = match.group(1)
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------

_INTERPOLATION = re.compile(r"\$\{\s*var\.(\w+)\s*\}")
_BARE_VAR = re.compile(r"^var\.(\w+)$")


def variable_defaults() -> dict[str, str]:
    """`variable "x" { default = "y" }` from infra/variables.tf, as x -> y.

    Only string defaults: every name this scanner resolves is a string, and a
    number or list default would be a signal that the caller is reading
    something it should not be.
    """
    text = (repo_root() / "infra" / "variables.tf").read_text()
    out: dict[str, str] = {}
    for header, body, _, _ in blocks(text, r'variable\s+"(\w+)"\s*(?=\{)'):
        default = scalar(body, "default")
        if default is not None:
            out[header.group(1)] = default
    return out


def resolve(value: str, variables: dict[str, str]) -> str | None:
    """Substitute `${var.x}` / `var.x` in a Terraform scalar.

    Returns None when the value still depends on something this scanner cannot
    resolve — a variable with no default, or a reference to another resource's
    attribute. Callers must treat None as "unknown", never as a name: silently
    resolving to a literal `"${var.app_name}"` would let a guard compare a
    runbook against a string no AWS account ever sees.
    """
    bare = _BARE_VAR.match(value.strip())
    if bare:
        return variables.get(bare.group(1))

    resolved = value
    for match in _INTERPOLATION.finditer(value):
        replacement = variables.get(match.group(1))
        if replacement is None:
            return None
        resolved = resolved.replace(match.group(0), replacement)

    # Anything left with a `${...}` is an unresolvable expression.
    if "${" in resolved:
        return None
    return resolved
