"""
Runbook loader.

Runbooks are YAML files at the repo root in /runbooks/, one per CloudWatch
alarm or SLO. They're loaded once at import time and exposed to operators
via the admin API so the on-call engineer can see diagnosis steps right
next to the alarm that fired.

The file format is intentionally lightweight (a dict per file). We don't
enforce a strict schema — fields are passed through to the API response
verbatim so adding new advisory sections doesn't require a code change.
"""

from pathlib import Path
from typing import Any

import yaml
from app.core.logging import get_logger

logger = get_logger(__name__)


# Repo root → runbooks/. The module lives at backend/app/services/runbooks.py.
_RUNBOOKS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "runbooks"
)


def _load_all() -> dict[str, dict[str, Any]]:
    if not _RUNBOOKS_DIR.is_dir():
        logger.warning(
            "runbooks directory missing — admin endpoints will return empty",
            extra={"path": str(_RUNBOOKS_DIR)},
        )
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(_RUNBOOKS_DIR.glob("*.yaml")):
        with path.open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.error("runbook is not a mapping", extra={"path": str(path)})
            continue
        rb_id = data.get("id")
        if not rb_id:
            logger.error("runbook missing id", extra={"path": str(path)})
            continue
        out[str(rb_id)] = data
    return out


_RUNBOOKS: dict[str, dict[str, Any]] = _load_all()


def list_all() -> list[dict[str, Any]]:
    """Return the runbooks ordered by id."""
    return list(_RUNBOOKS.values())


def get(runbook_id: str) -> dict[str, Any] | None:
    return _RUNBOOKS.get(runbook_id)


def reload() -> None:
    """Re-scan the runbooks directory. Useful for tests."""
    global _RUNBOOKS
    _RUNBOOKS = _load_all()
