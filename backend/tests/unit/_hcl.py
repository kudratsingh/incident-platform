"""A small, shared HCL scanner for the Terraform tripwire tests."""

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
    """Index just past the `}` closing the `{` at `open_index`."""
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
    """Value of a top-level `key = "value"` assignment, still backslash-escaped."""
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
    """Body with every nested `{...}` block blanked out, offsets preserved."""
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
    """Value of `key = ...` at the top level of `body`, quotes stripped."""
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
    """`variable "x" { default = "y" }` from infra/variables.tf, as x -> y."""
    text = (repo_root() / "infra" / "variables.tf").read_text()
    out: dict[str, str] = {}
    for header, body, _, _ in blocks(text, r'variable\s+"(\w+)"\s*(?=\{)'):
        default = scalar(body, "default")
        if default is not None:
            out[header.group(1)] = default
    return out


def resolve(value: str, variables: dict[str, str]) -> str | None:
    """Substitute `${var.x}` / `var.x` in a Terraform scalar."""
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
