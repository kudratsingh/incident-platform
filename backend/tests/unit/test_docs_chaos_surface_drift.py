"""Tripwire against the compose comment mis-describing the chaos surface.

`docker-compose.yml` explains to a reader of the `mcp` service what
`CHAOS_ENABLED=true` actually turns on. It enumerated five chaos tools
and called that the whole set. Nine are registered. An operator reading
the comment to decide whether flipping the flag was safe was reading a
blast radius roughly half the real one.

The registered set is derived here from the `@chaos_tool` decorators in
`app/mcp/tools/chaos/` rather than from the live registry, because the
registry is populated at import time under `CHAOS_ENABLED=true` and the
unit tier runs with the gate closed. The decorator call *is* the
registration, so the source is the honest authority either way.
"""

from __future__ import annotations

import ast
import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CHAOS_PKG = _REPO_ROOT / "backend" / "app" / "mcp" / "tools" / "chaos"
_COMPOSE = _REPO_ROOT / "docker-compose.yml"


def _registered_chaos_tools() -> set[str]:
    """Every tool name passed to a `@chaos_tool(...)` decorator."""
    names: set[str] = set()
    for module in _CHAOS_PKG.glob("*.py"):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for decorator in getattr(node, "decorator_list", []):
                if (
                    isinstance(decorator, ast.Call)
                    and getattr(decorator.func, "id", None) == "chaos_tool"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                ):
                    names.add(decorator.args[0].value)
    return names


def test_chaos_package_registers_tools() -> None:
    """Sanity floor: if the AST walk finds nothing the assertions below
    would pass vacuously."""
    assert len(_registered_chaos_tools()) >= 9


def test_compose_chaos_comment_states_the_real_tool_count() -> None:
    registered = _registered_chaos_tools()
    text = _COMPOSE.read_text(encoding="utf-8")

    match = re.search(r"the (\d+) chaos tools register", text)
    assert match is not None, (
        "docker-compose.yml no longer describes the chaos tool count in the "
        "form this test pins ('the N chaos tools register'). Update the test "
        "or restore the comment."
    )
    claimed = int(match.group(1))
    assert claimed == len(registered), (
        f"docker-compose.yml claims {claimed} chaos tools register with "
        f"CHAOS_ENABLED=true; {len(registered)} are registered: "
        f"{sorted(registered)}"
    )


def test_compose_chaos_comment_names_every_registered_tool() -> None:
    """The comment enumerates the tools by name. A tool added to the
    package without being added to the list leaves an operator with an
    understated blast radius, which is the exact defect this guards."""
    registered = _registered_chaos_tools()
    text = _COMPOSE.read_text(encoding="utf-8")

    # The enumeration lives in the parenthesised list directly after the
    # count sentence.
    block = re.search(r"the \d+ chaos tools register\s*(.*?)\);", text, re.DOTALL)
    assert block is not None, "chaos tool enumeration not found in compose"
    listed = set(re.findall(r"[a-z_]+", block.group(1).replace("#", " ")))

    unlisted = sorted(name for name in registered if name not in listed)
    assert not unlisted, f"chaos tools registered but not named in the compose comment: {unlisted}"
