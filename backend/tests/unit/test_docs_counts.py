"""The counts CLAUDE.md and the ADR index state are the counts in the tree (WO-R3-323)."""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CLAUDE = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
_ADR_DIR = _ROOT / "docs" / "ADR"
_INTEGRATION = _ROOT / "backend" / "tests" / "integration"

_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _word(n: int) -> str:
    if n < 20:
        return _WORDS[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + ("" if ones == 0 else "-" + _WORDS[ones])


def _adr_numbers() -> set[str]:
    return {p.name[:4] for p in _ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")}


def _integration_census() -> dict[str, int]:
    # One file may start more than one container, so the per-image counts can
    # sum past the file count; "other" is a file that starts none of the three.
    census = {"files": 0, "postgres": 0, "redpanda": 0, "redis": 0, "other": 0, "gated": 0}
    for path in sorted(_INTEGRATION.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        found = {
            "postgres": "PostgresContainer(" in text,
            "redpanda": "redpandadata/redpanda" in text,
            "redis": 'DockerContainer("redis:' in text,
        }
        census["files"] += 1
        for key, hit in found.items():
            census[key] += hit
        census["other"] += not any(found.values())
        census["gated"] += bool(re.search(r"RUN_[A-Z_]+_TEST", text))
    return census


def test_the_adr_index_lists_every_adr_in_one_table() -> None:
    readme = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| \[(\d{4})\]", readme, re.M)
    assert set(rows) == _adr_numbers() and len(rows) == len(set(rows))
    # A blank line inside the table ends it, and every row after renders as text.
    table = re.search(r"^\| # \|.*?(?=\n\n)", readme, re.M | re.S)
    assert table is not None
    assert len(re.findall(r"^\| \[\d{4}\]", table.group(0), re.M)) == len(rows)
    assert f"All {len(rows)} are accepted" in readme


def test_the_claude_md_doc_map_lists_every_adr() -> None:
    listed = re.findall(r"^  - \[(\d{4}) — ", _CLAUDE, re.M)
    assert set(listed) == _adr_numbers() and len(listed) == len(set(listed))
    assert f"all {len(listed)} are listed below:" in _CLAUDE


def test_the_integration_tier_counts_match_the_directory() -> None:
    c = _integration_census()
    assert c["other"] == 0, "a new kind of container needs its own count in CLAUDE.md"
    assert (
        f"# {c['files']} files — Testcontainers (Docker-gated: Postgres ×{c['postgres']}, "
        f"Redpanda ×{c['redpanda']}, Redis ×{c['redis']})"
    ) in _CLAUDE
    assert (
        f"Postgres 16 in {_word(c['postgres'])} files, Redpanda in {_word(c['redpanda'])}, "
        f"Redis in {_word(c['redis'])};"
    ) in _CLAUDE
    files = _word(c["files"])
    assert _CLAUDE.count(f"{files.capitalize()} test files.") == 2
    assert f"{_word(c['gated'])} of the {files} files carry an **opt-in** env gate" in _CLAUDE
