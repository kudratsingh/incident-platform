"""Tripwire against docs drifting from the real consumer-lag key (D-16).

docs/REDIS.md and docs/KAFKA.md once documented
`kafka:consumer_lag:dispatcher` while the code writes
`kafka:consumer_lag:worker-dispatcher` — so the docs' copy-pasteable
inspect command read a key that never exists and pointed on-call at a
"dead" metrics loop. This exact drift already happened once; asserting
against the constant the code actually uses makes a rename fail here
instead of silently stranding the docs again.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from app.utils.backpressure import BACKPRESSURE_LAG_KEY

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# The stale spelling as a *full* key: not followed by a word character
# or hyphen, so the real `kafka:consumer_lag:worker-dispatcher` (which
# differs in the segment prefix) and any longer group name never trip it.
_STALE_KEY = re.compile(r"kafka:consumer_lag:dispatcher(?![\w-])")


def _doc_text(name: str) -> str:
    return (_REPO_ROOT / "docs" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("doc", ["REDIS.md", "KAFKA.md"])
def test_docs_name_the_real_consumer_lag_key(doc: str) -> None:
    assert BACKPRESSURE_LAG_KEY in _doc_text(doc), (
        f"docs/{doc} must document the real backpressure key "
        f"{BACKPRESSURE_LAG_KEY!r} (app/utils/backpressure.py)"
    )


@pytest.mark.parametrize("doc", ["REDIS.md", "KAFKA.md"])
def test_docs_do_not_name_the_stale_consumer_lag_key(doc: str) -> None:
    match = _STALE_KEY.search(_doc_text(doc))
    assert match is None, (
        f"docs/{doc} still names the stale key {match.group(0)!r} — the "
        f"metrics loop writes {BACKPRESSURE_LAG_KEY!r}"
    )
