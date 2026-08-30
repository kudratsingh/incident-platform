"""Tripwire tests binding every CloudWatch alarm to a metric the code publishes.

Invariant: an alarm that names a metric/dimension pair nothing emits is not a
weak alarm, it is a *silent* one. With `treat_missing_data = "notBreaching"`
it sits in OK forever and reads exactly like a healthy system, so the failure
is invisible until the incident it was meant to catch has already happened.
Nothing else in the repo can see this: Terraform validates the alarm's syntax,
never its data source, and the alarm only meets the emitters in a deployed
account.

Finding R2-14: the job-completion SLO fast-burn alarm queried
JobDeadLettered/JobCompleted with no dimensions while the dispatcher only ever
publishes them with a JobType dimension, so the ratio had no data source and
could never fire. Three more of the same family shipped alongside it (the RDS
alarm's `.id`-vs-`.identifier` dimension, and one alarm with no
`treat_missing_data` at all).

What this file can and cannot catch: it cross-checks the *mechanical* contract
— does this (metric, dimension-keys) tuple exist on the emitter side — which
covers every fault above. It cannot judge whether a metric that does exist
*measures the right thing*; the QueueDepth-vs-ConsumerLag half of R2-14 was a
semantic fault (QueueDepth reads only the Redis delayed-retry set, not the
Kafka backlog) and is held by review and the runbooks, not by this guard.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

CUSTOM_NAMESPACE = "IncidentPlatform"

#: Emitter helpers whose first positional argument is the metric name and whose
#: `dimensions=` keyword carries the dimension keys.
EMITTER_FUNCTIONS = frozenset({"emit_count", "emit_gauge"})

#: Sentinel for a metric emitted with dimensions we cannot resolve statically
#: (a variable rather than a dict literal). Any alarm dimension set is accepted
#: for such a metric — better a gap than a false failure that teaches people to
#: edit this file to make it quiet.
WILDCARD = "*"


def _repo_root() -> Path:
    """Locate the repo root by walking up from this file until Dockerfile is found."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "Dockerfile").is_file():
            return parent
    raise AssertionError("no Dockerfile found in any parent of this test file")


# ---------------------------------------------------------------------------
# Emitter side — what backend/app actually publishes
# ---------------------------------------------------------------------------


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


def _emitted_metrics() -> dict[str, set[frozenset[str]] | str]:
    """Map every emitted metric name to the dimension key-sets it is emitted with.

    Walks backend/app rather than importing it: the emit sites are spread over
    request middleware and several worker loops, and only a static sweep sees
    all of them without running any of them.
    """
    app_dir = _repo_root() / "backend" / "app"
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


# ---------------------------------------------------------------------------
# Alarm side — a small HCL scanner over infra/cloudwatch.tf
# ---------------------------------------------------------------------------


def _match_brace(text: str, open_index: int) -> int:
    """Index just past the `}` closing the `{` at `open_index`.

    String-aware and comment-aware: SEARCH expressions embed literal braces
    (`'{IncidentPlatform,JobType} MetricName=...'`) and a naive depth counter
    walks straight off the end of the file on them.
    """
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif char == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced braces from offset {open_index}")


def _blocks(text: str, pattern: str) -> list[tuple[re.Match[str], str, int, int]]:
    """Every block whose header matches `pattern`, as (header, body, start, end)."""
    found = []
    for header in re.finditer(pattern, text, flags=re.MULTILINE):
        open_index = text.index("{", header.end() - 1)
        end = _match_brace(text, open_index)
        found.append((header, text[open_index + 1 : end - 1], open_index, end))
    return found


def _scalar(body: str, key: str) -> str | None:
    """Value of a top-level `key = "value"` assignment, still backslash-escaped.

    The string body must tolerate `\\"` — the SEARCH expressions embed quoted
    metric names — so a plain `[^"]*` would truncate at the first inner quote
    and silently yield no match at all.
    """
    match = re.search(
        rf'^\s*{key}\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', body, flags=re.MULTILINE
    )
    return match.group(1) if match else None


def _has_key(body: str, key: str) -> bool:
    return re.search(rf"^\s*{key}\s*=", body, flags=re.MULTILINE) is not None


def _dimensions(body: str) -> dict[str, str]:
    """The `dimensions = { ... }` map as key -> raw (unparsed) value expression."""
    blocks = _blocks(body, r"^\s*dimensions\s*=\s*(?=\{)")
    if not blocks:
        return {}
    _, inner, _, _ = blocks[0]
    dims = {}
    for line in inner.splitlines():
        match = re.match(r'\s*(\w+)\s*=\s*(.+?)\s*$', line)
        if match and not line.lstrip().startswith("#"):
            dims[match.group(1)] = match.group(2).strip('"')
    return dims


def _excise(body: str, spans: list[tuple[int, int]]) -> str:
    """Body with the given [start, end) spans blanked out, offsets preserved."""
    chars = list(body)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


@dataclass(frozen=True)
class MetricRef:
    """One (metric, dimensions) pair an alarm reads, with where it came from."""

    alarm: str
    where: str
    namespace: str
    metric_name: str
    dimension_keys: frozenset[str]

    def __str__(self) -> str:
        dims = ", ".join(sorted(self.dimension_keys)) or "<no dimensions>"
        return f"{self.alarm}{self.where}: {self.namespace}/{self.metric_name} [{dims}]"


#: `SEARCH('{Namespace,Dim1,Dim2} MetricName="Foo"', 'Sum', 3600)` as it appears
#: in the .tf source, where the inner quotes are backslash-escaped by HCL.
_SEARCH = re.compile(
    r"SEARCH\(\s*'\{(?P<schema>[^}]*)\}\s*MetricName=\\?\"(?P<metric>[^\"\\]+)\\?\""
)


@dataclass(frozen=True)
class Alarm:
    name: str
    body: str
    refs: tuple[MetricRef, ...]
    dimensions: dict[str, str]

    def __hash__(self) -> int:  # dict field is not hashable; identity by name
        return hash(self.name)


def _parse_alarms() -> list[Alarm]:
    text = (_repo_root() / "infra" / "cloudwatch.tf").read_text()
    alarms = []

    for header, body, _, _ in _blocks(
        text, r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(\w+)"\s*(?=\{)'
    ):
        name = header.group(1)
        refs: list[MetricRef] = []

        query_blocks = _blocks(body, r"^\s*metric_query\s*(?=\{)")
        for query_header, query_body, _, _ in query_blocks:
            del query_header
            for _, metric_body, _, _ in _blocks(query_body, r"^\s*metric\s*(?=\{)"):
                metric_name = _scalar(metric_body, "metric_name")
                namespace = _scalar(metric_body, "namespace")
                query_id = _scalar(query_body, "id") or "?"
                if metric_name and namespace:
                    refs.append(
                        MetricRef(
                            alarm=name,
                            where=f"[{query_id}]",
                            namespace=namespace,
                            metric_name=metric_name,
                            dimension_keys=frozenset(_dimensions(metric_body)),
                        )
                    )
            expression = _scalar(query_body, "expression") or ""
            for match in _SEARCH.finditer(expression):
                schema = [part.strip() for part in match.group("schema").split(",")]
                query_id = _scalar(query_body, "id") or "?"
                refs.append(
                    MetricRef(
                        alarm=name,
                        where=f"[{query_id}]",
                        namespace=schema[0],
                        metric_name=match.group("metric"),
                        dimension_keys=frozenset(schema[1:]),
                    )
                )

        # Top-level metric_name/namespace, with any metric_query bodies blanked
        # out so a nested metric_name is never read as the alarm's own.
        top = _excise(body, [(start, end) for _, _, start, end in query_blocks])
        metric_name = _scalar(top, "metric_name")
        namespace = _scalar(top, "namespace")
        dimensions = _dimensions(top)
        if metric_name and namespace:
            refs.append(
                MetricRef(
                    alarm=name,
                    where="",
                    namespace=namespace,
                    metric_name=metric_name,
                    dimension_keys=frozenset(dimensions),
                )
            )

        alarms.append(Alarm(name=name, body=body, refs=tuple(refs), dimensions=dimensions))

    return alarms


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


def test_the_scanner_actually_found_the_alarms() -> None:
    """Meta-guard: a scanner that silently parses nothing would pass everything."""
    alarms = _parse_alarms()
    assert len(alarms) >= 9, f"expected the full alarm set, scanned {len(alarms)}"
    assert all(alarm.refs for alarm in alarms), (
        "every alarm must resolve to at least one metric reference; "
        f"bare: {[a.name for a in alarms if not a.refs]}"
    )
    emitted = _emitted_metrics()
    for expected in ("JobDeadLettered", "JobCompleted", "QueueDepth", "ConsumerLag"):
        assert expected in emitted, f"emitter sweep missed {expected}"


def test_every_custom_alarm_reads_a_metric_the_code_publishes() -> None:
    """Each IncidentPlatform alarm must name a (metric, dimension-keys) pair emitted.

    The dimension keys have to match exactly, not merely overlap: CloudWatch
    treats a dimension set as part of the metric's identity, so an alarm on
    JobDeadLettered with no dimensions and an emitter publishing
    JobDeadLettered[JobType] are two different metrics and the alarm's one is
    always empty (R2-14).
    """
    emitted = _emitted_metrics()
    failures = []

    for alarm in _parse_alarms():
        for ref in alarm.refs:
            if ref.namespace != CUSTOM_NAMESPACE:
                continue  # AWS-published namespace; not ours to emit.
            known = emitted.get(ref.metric_name)
            if known is None:
                failures.append(f"{ref} -> nothing in backend/app emits this metric")
            elif known != WILDCARD and ref.dimension_keys not in known:
                shapes = " | ".join(
                    ", ".join(sorted(keys)) or "<no dimensions>"
                    for keys in sorted(known, key=sorted)
                )
                failures.append(f"{ref} -> emitted only as [{shapes}]")

    assert not failures, "alarms reading metrics nothing publishes:\n  " + "\n  ".join(failures)


def test_every_alarm_declares_treat_missing_data() -> None:
    """No alarm may inherit the `missing` default.

    The default holds the alarm in whatever state it was last in when the
    datapoints stop, which is the one behaviour nobody wants from a resource
    that has gone away entirely: it reads OK because it read OK an hour ago.
    Declaring it makes the choice deliberate and reviewable per alarm.
    """
    missing = [
        alarm.name
        for alarm in _parse_alarms()
        if not _has_key(alarm.body, "treat_missing_data")
    ]
    assert not missing, f"alarms with no explicit treat_missing_data: {missing}"


def test_rds_alarm_uses_the_instance_identifier_not_the_resource_id() -> None:
    """`aws_db_instance.<x>.id` is the DBI resource id (`db-ABC…`) under AWS provider v5.

    CloudWatch's DBInstanceIdentifier dimension carries the *instance
    identifier* ("incident-platform"), so an alarm wired to `.id` watches a
    dimension that never receives a datapoint. The two attributes are both
    plausible-looking strings, which is exactly why this needs a test and not
    a comment.
    """
    offenders = []
    for alarm in _parse_alarms():
        for key, value in alarm.dimensions.items():
            if key == "DBInstanceIdentifier" and re.search(r"aws_db_instance\.\w+\.id\b", value):
                offenders.append(f"{alarm.name}: {key} = {value}")
    assert not offenders, (
        "DBInstanceIdentifier must come from .identifier, not .id:\n  " + "\n  ".join(offenders)
    )
