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

import re
from dataclasses import dataclass

from ._emitters import CUSTOM_NAMESPACE, WILDCARD
from ._emitters import emitted_metrics as _emitted_metrics
from ._hcl import blocks as _blocks
from ._hcl import excise as _excise
from ._hcl import has_key as _has_key
from ._hcl import repo_root as _repo_root
from ._hcl import scalar as _scalar

# ---------------------------------------------------------------------------
# Alarm side — reads infra/cloudwatch.tf through the shared HCL scanner
# ---------------------------------------------------------------------------


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
