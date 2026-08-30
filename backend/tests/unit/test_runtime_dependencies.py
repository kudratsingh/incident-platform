"""Every third-party module the app imports must be a *runtime* dependency.

`httpx` was imported at module scope by `app/services/alerts.py` — the alert
webhook, the incident commander's production trigger — while being declared
only in the `[dev]` extra. The production image installs `.` without extras,
so it got httpx purely as a transitive dependency of `anthropic`: an import
the application never asked for, held up by a package that has no obligation
to keep providing it (WO-R2-66).

A hand-written "httpx is declared" assertion would pin that one line and
nothing else, so this walks the application's own imports instead. It is the
same tripwire shape as `test_docs_redis_key_drift.py`: assert the property,
not the instance.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib
from importlib.metadata import packages_distributions

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_APP = _REPO_ROOT / "backend" / "app"


def _declared_runtime_distributions() -> set[str]:
    """Normalised distribution names from `[project].dependencies`."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    names = set()
    for spec in data["project"]["dependencies"]:
        # "sqlalchemy[asyncio]>=2.0.0" -> "sqlalchemy"
        name = spec.split(";")[0].strip()
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "["):
            name = name.split(sep)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def _module_scope_imports() -> dict[str, set[pathlib.Path]]:
    """Top-level import names used at module scope, mapped to their files.

    Module scope only: an import inside a function is paid when that function
    runs, which is a different (and often deliberate) decision — several
    modules here defer heavy imports on purpose. An import at module scope is
    paid at process start, so it must be installed for the process to boot.
    """
    found: dict[str, set[pathlib.Path]] = {}
    for path in _APP.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in tree.body:  # module scope only, not a full walk
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for name in names:
                found.setdefault(name.split(".")[0], set()).add(path)
    return found


def test_module_scope_imports_are_declared_runtime_dependencies() -> None:
    declared = _declared_runtime_distributions()
    # Import name -> distribution(s) that provide it, from the installed set.
    provided_by = packages_distributions()
    stdlib = set(sys.stdlib_module_names)

    undeclared: dict[str, tuple[set[str], list[str]]] = {}
    for module, files in _module_scope_imports().items():
        if module in stdlib or module in {"app", "scripts", "tests"}:
            continue
        distributions = {
            dist.lower().replace("_", "-") for dist in provided_by.get(module, [])
        }
        if not distributions:
            # Not installed in this environment — nothing to check against.
            continue
        if distributions & declared:
            continue
        undeclared[module] = (
            distributions,
            sorted(str(f.relative_to(_REPO_ROOT)) for f in files),
        )

    assert not undeclared, (
        "module-scope imports resolved only through the dev extra or a "
        "transitive dependency — declare them in [project].dependencies: "
        f"{undeclared}"
    )


def test_httpx_is_a_runtime_dependency_not_a_dev_one() -> None:
    """The instance that prompted the tripwire, pinned directly."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    runtime = " ".join(data["project"]["dependencies"])
    dev = " ".join(data["project"]["optional-dependencies"]["dev"])

    assert "httpx" in runtime
    assert "httpx" not in dev, (
        "httpx is a runtime dependency now; leaving it in [dev] as well "
        "invites the two to drift apart on version"
    )
