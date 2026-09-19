"""Tripwire tests binding module-level loader paths to the shipped-artifact manifest."""

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
    """The runtime image must COPY runbooks/ into the app root."""
    dockerfile = (_repo_root() / "Dockerfile").read_text()
    assert re.search(r"^COPY\s+runbooks/\s+\./runbooks/", dockerfile, flags=re.MULTILINE), (
        "Dockerfile has no `COPY runbooks/ ./runbooks/` line — the image would "
        "serve an empty runbooks API for the process lifetime (E2-03)"
    )


def test_compose_mounts_runbooks_on_app_service() -> None:
    """The dev stack must bind-mount ./runbooks on the app service."""
    compose = yaml.safe_load((_repo_root() / "docker-compose.yml").read_text())
    volumes = compose["services"]["app"]["volumes"]
    assert "./runbooks:/app/runbooks" in volumes, (
        "docker-compose.yml app service does not mount ./runbooks:/app/runbooks — "
        "the compose stack would serve an empty runbooks API (E2-03)"
    )


def test_loader_path_is_bound_to_the_manifest_checks() -> None:
    """Sanity binding: the loader resolves exactly <repo root>/runbooks."""
    module_file = Path(runbooks.__file__).resolve()
    assert runbooks._RUNBOOKS_DIR.name == "runbooks"
    # parents[3] == parent.parent.parent.parent — the in-container app root (/app).
    assert runbooks._RUNBOOKS_DIR.parent == module_file.parents[3]
