"""What `backend/app` actually publishes to CloudWatch, by static sweep.

Shared by `test_alarm_metrics_match_emitters.py` (does every alarm read a
metric that exists) and `test_runbook_lint.py` (does every metric an on-call
engineer is told to open exist). Those two questions have to be answered from
the same source or the runbooks can name a metric the alarm guard already
rejected.

Walks the tree rather than importing it: the emit sites are spread over
request middleware and several worker loops, and only a static sweep sees all
of them without running any of them.
"""

from __future__ import annotations

import ast

from ._hcl import repo_root

CUSTOM_NAMESPACE = "IncidentPlatform"

#: Emitter helpers whose first positional argument is the metric name and whose
#: `dimensions=` keyword carries the dimension keys.
EMITTER_FUNCTIONS = frozenset({"emit_count", "emit_gauge"})

#: Sentinel for a metric emitted with dimensions we cannot resolve statically
#: (a variable rather than a dict literal). Any alarm dimension set is accepted
#: for such a metric — better a gap than a false failure that teaches people to
#: edit this file to make it quiet.
WILDCARD = "*"


def _dimension_keys(call: ast.Call) -> frozenset[str] | str:
    """Dimension keys of one emit call, or WILDCARD if not statically knowable."""
    for keyword in call.keywords:
        if keyword.arg != "dimensions":
            continue
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
            return frozenset()
        if not isinstance(keyword.value, ast.Dict):
            return WILDCARD
        keys = set()
        for key in keyword.value.keys:
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return WILDCARD
            keys.add(key.value)
        return frozenset(keys)
    # No `dimensions=` kwarg at all: emitted with no dimensions.
    return frozenset()


def emitted_metrics() -> dict[str, set[frozenset[str]] | str]:
    """Map every emitted metric name to the dimension key-sets it is emitted with."""
    app_dir = repo_root() / "backend" / "app"
    emitted: dict[str, set[frozenset[str]] | str] = {}

    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            if name not in EMITTER_FUNCTIONS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                # Dynamic metric name — nothing to bind an alarm to.
                continue
            metric = first.value
            keys = _dimension_keys(node)
            existing = emitted.get(metric)
            if existing == WILDCARD:
                continue
            if keys == WILDCARD:
                emitted[metric] = WILDCARD
            else:
                assert isinstance(keys, frozenset)
                if isinstance(existing, set):
                    existing.add(keys)
                else:
                    emitted[metric] = {keys}

    return emitted
