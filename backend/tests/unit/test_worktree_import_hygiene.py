"""Tripwire test binding the imported `app` package to the tree the tests live in."""

from pathlib import Path

import app


def _tests_repo_root() -> Path:
    """Repo root of *this test file*, found by walking up to the Dockerfile."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "Dockerfile").is_file():
            return parent
    raise AssertionError("no Dockerfile found in any parent of this test file")


def test_app_resolves_to_the_tree_under_test() -> None:
    """The first entry of app.__path__ must be this checkout's backend/app."""
    search_path = [Path(entry).resolve() for entry in app.__path__]
    assert search_path, "app.__path__ is empty; nothing to check"

    expected = (_tests_repo_root() / "backend" / "app").resolve()

    assert search_path[0] == expected, (
        "pytest is testing a different checkout than the one it was launched "
        f"from.\n  expected `app` from: {expected}\n"
        f"  actually resolves to: {search_path[0]}\n"
        f"  full app.__path__:    {[str(p) for p in search_path]}\n"
        "The venv's editable-install .pth points at the checkout it was built "
        "in, so from a git worktree `import app` reaches the main checkout "
        "unless PYTHONPATH takes precedence. Run the suite via `make test` "
        f"(which sets it), or export PYTHONPATH={expected.parent}."
    )
