"""Tripwire tests binding module-level loader paths to the shipped-artifact manifest.

Invariant: every directory a module-level loader resolves against the
in-container app root must appear in the shipped-artifact manifest — the
Dockerfile COPY list and the compose bind mounts. The unit suite runs from
the source checkout, where <repo>/runbooks always exists, so no test that
exercises the loader can ever see a missing-COPY bug; only a check against
the artifact-manifest text can. Finding E2-03: the image and compose stack
served an empty runbooks API forever because runbooks/ was never shipped.
"""

import re
from pathlib import Path

import yaml
from app.services import runbooks


def _repo_root() -> Path:
    """Locate the repo root by walking up from this file until Dockerfile is found."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "Dockerfile").is_file():
            return parent
    raise AssertionError("no Dockerfile found in any parent of this test file")


def test_dockerfile_ships_runbooks_dir() -> None:
    """The runtime image must COPY runbooks/ into the app root.

    services/runbooks.py resolves <app root>/runbooks at import time and
    caches the result for the process lifetime; an image without the
    directory serves an empty runbooks API forever (E2-03).
    """
    dockerfile = (_repo_root() / "Dockerfile").read_text()
    assert re.search(r"^COPY\s+runbooks/\s+\./runbooks/", dockerfile, flags=re.MULTILINE), (
        "Dockerfile has no `COPY runbooks/ ./runbooks/` line — the image would "
        "serve an empty runbooks API for the process lifetime (E2-03)"
    )


def test_compose_mounts_runbooks_on_app_service() -> None:
    """The dev stack must bind-mount ./runbooks on the app service.

    The compose stack overlays source mounts on the image; without this one
    the --reload dev loop (and any stack built before the COPY existed) has
    no /app/runbooks. Only `app` needs it — the mcp process never imports
    app.services.runbooks.
    """
    compose = yaml.safe_load((_repo_root() / "docker-compose.yml").read_text())
    volumes = compose["services"]["app"]["volumes"]
    assert "./runbooks:/app/runbooks" in volumes, (
        "docker-compose.yml app service does not mount ./runbooks:/app/runbooks — "
        "the compose stack would serve an empty runbooks API (E2-03)"
    )


def test_loader_path_is_bound_to_the_manifest_checks() -> None:
    """Sanity binding: the loader resolves exactly <repo root>/runbooks.

    The manifest tests above assert about runbooks/ at the app root; this
    pins the loader to that same path — exactly 4 parents above
    services/runbooks.py, named `runbooks`. If someone moves the module or
    renames the directory, this fails loudly instead of the manifest checks
    silently guarding a path the loader no longer reads.
    """
    module_file = Path(runbooks.__file__).resolve()
    assert runbooks._RUNBOOKS_DIR.name == "runbooks"
    # parents[3] == parent.parent.parent.parent — the in-container app root (/app).
    assert runbooks._RUNBOOKS_DIR.parent == module_file.parents[3]
