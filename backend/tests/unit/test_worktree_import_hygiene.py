"""Tripwire test binding the imported `app` package to the tree the tests live in.

Invariant: the code under test must come from the same checkout as the tests
doing the testing.

The venv holds an *editable* install, whose .pth file hardcodes the absolute
path of the checkout it was created from. Run the suite from a git worktree
and `import app` silently resolves to the MAIN checkout: the test files are
read from the worktree, the code they exercise is not. Nothing about the
output looks wrong. You get a green run for a change you never tested, or a
red one for a bug you already fixed, and the only tell is that the results
disagree with the diff in front of you.

`app` is a namespace package — there is no backend/app/__init__.py — which is
what makes this quiet rather than loud. A regular package would bind to one
directory and `app.__file__` would name it; a namespace package instead keeps
an ordered *list* of every directory contributing to it, so the main
checkout's copy is a perfectly valid source for `app.workers.dispatcher` and
nothing raises. It also explains why the fix works: PYTHONPATH entries are
scanned before site-packages, so $(CURDIR)/backend lands at the FRONT of
app.__path__ and shadows the .pth entry submodule by submodule.

Three separate agents lost time to this before it was written down (R2-117,
follow-up from R2-11/12). `make test` and `make test-integration` now set
PYTHONPATH=$(CURDIR)/backend; this test is what notices when something runs
outside that path — a bare `pytest` from a worktree, a new target that forgets
the prefix, or a stale editable install.
"""

from pathlib import Path

import app


def _tests_repo_root() -> Path:
    """Repo root of *this test file*, found by walking up to the Dockerfile."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "Dockerfile").is_file():
            return parent
    raise AssertionError("no Dockerfile found in any parent of this test file")


def test_app_resolves_to_the_tree_under_test() -> None:
    """The first entry of app.__path__ must be this checkout's backend/app.

    First, not merely present: with PYTHONPATH set from a worktree the path
    holds both trees, and precedence is the whole point — the entry that comes
    first is the one every `app.*` submodule is actually loaded from.
    """
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
