"""Runbook lint — the commands in runbooks/*.yaml must be executable.

`test_runbooks.py` checks the YAML *shape*: does the file load, does it have
an id and a summary. That is worth having and it is not this. A runbook can
satisfy every one of those assertions and still be useless at 3am, because
the shape says nothing about whether the resources it names exist or whether
its commands can be pasted into a shell.

Finding R2-52 was nine of those. Two runbooks named an ECS service that does
not exist (workers run in-process inside the backend task; `infra/ecs.tf`
defines exactly two services). Two named the log group and the ECR repository
with a hyphen where Terraform uses a slash. One recommended `FLUSHDB` against
a key prefix that has never existed, and `FLUSHDB` is the one command
`docs/REDIS.md` singles out as too destructive to run — it takes
`delayed_queue` and `priority_queue` with it, which are durable state with no
TTL. One had an unterminated quote and could not be pasted at all.

Every one of those is mechanically checkable against a file already in the
repo, which is what this module does:

  * resource names → `infra/*.tf`, with `${var.x}` resolved from variables.tf
  * metric names   → the emit sites in `backend/app`
  * Redis keys     → the key catalog in `docs/REDIS.md`
  * shell syntax   → `shlex`, POSIX mode
  * alarm names    → `aws_cloudwatch_metric_alarm.alarm_name`

The alarm check is the one that decays fastest: #160 repointed two alarms
from QueueDepth to ConsumerLag and #174 added the SLO alert loop, and a
runbook that still names the old metric sends on-call to a flat graph during
an incident. Bind it to Terraform and the code, not to review.

What this cannot check: whether a command that parses does the *right* thing.
`aws logs tail` against a real log group with the wrong `--filter-pattern`
passes here. Semantics stay with review.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from app.services import runbooks

from ._emitters import CUSTOM_NAMESPACE, emitted_metrics
from ._hcl import blocks, repo_root, resolve, top_attribute, variable_defaults

# ---------------------------------------------------------------------------
# The Terraform inventory
# ---------------------------------------------------------------------------

#: Resource type -> the attribute holding the name AWS actually sees. Only the
#: types a runbook can legitimately name; anything else is not addressable
#: from a shell and has nothing to check.
_NAME_ATTRIBUTE = {
    "aws_cloudwatch_log_group": "name",
    "aws_ecr_repository": "name",
    "aws_ecs_cluster": "name",
    "aws_ecs_service": "name",
    "aws_cloudwatch_metric_alarm": "alarm_name",
    "aws_elasticache_replication_group": "node_type",
}


def _terraform_names() -> dict[str, set[str]]:
    """Resource type -> every resolved name of that type declared in infra/.

    A name that still contains an unresolved interpolation is dropped rather
    than compared literally: comparing a runbook against the string
    `"${var.app_name}-backend"` would pass nothing and fail everything.
    """
    variables = variable_defaults()
    found: dict[str, set[str]] = {kind: set() for kind in _NAME_ATTRIBUTE}

    for path in sorted((repo_root() / "infra").glob("*.tf")):
        text = path.read_text()
        for header, body, _, _ in blocks(text, r'resource\s+"(\w+)"\s+"(\w+)"\s*(?=\{)'):
            kind = header.group(1)
            attribute = _NAME_ATTRIBUTE.get(kind)
            if attribute is None:
                continue
            raw = top_attribute(body, attribute)
            if raw is None:
                continue
            resolved = resolve(raw, variables)
            if resolved is not None:
                found[kind].add(resolved)

    return found


# ---------------------------------------------------------------------------
# The runbooks
# ---------------------------------------------------------------------------


def _runbook_paths() -> list[Path]:
    return sorted((repo_root() / "runbooks").glob("*.yaml"))


def _runbook_docs() -> list[tuple[str, dict[str, Any]]]:
    """(filename, parsed) for every shipped runbook."""
    out = []
    for path in _runbook_paths():
        out.append((path.name, yaml.safe_load(path.read_text())))
    return out


def _strings(node: Any) -> Iterator[str]:
    """Every string anywhere in a parsed runbook."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _commands(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """(step id, command) for every diagnosis step that ships a command."""
    out = []
    for step in doc.get("diagnosis_steps") or []:
        if isinstance(step, dict) and step.get("command"):
            out.append((str(step.get("id", "?")), str(step["command"])))
    return out


def _all_text(doc: dict[str, Any]) -> str:
    return "\n".join(_strings(doc))


#: One case per shipped runbook, named after the file so a failure report says
#: which runbook is wrong without anyone opening the traceback.
_EACH_RUNBOOK = pytest.mark.parametrize(
    "filename,doc", _runbook_docs(), ids=lambda v: v if isinstance(v, str) else ""
)


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------

#: How a runbook can name each kind of resource on a command line. The AWS CLI
#: is the only way any of these are addressable, so the flag *is* the type.
_FLAG_PATTERNS = {
    "aws_ecs_cluster": re.compile(r"--cluster[= ]+([\w./-]+)"),
    "aws_ecs_service": re.compile(r"--services[= ]+([\w./-]+)|--service-name[= ]+([\w./-]+)"),
    "aws_ecr_repository": re.compile(r"--repository-name[= ]+([\w./-]+)"),
}

#: Log groups are addressed positionally (`aws logs tail <group>`), not by
#: flag, but they are unmistakable: nothing else in these runbooks starts /ecs/.
_LOG_GROUP = re.compile(r"(/ecs/[\w./-]+)")

_NODE_TYPE = re.compile(r"\bcache\.\w+\.\w+\b")

#: The two ways a runbook names a custom metric: as a path in a command
#: (`CloudWatch metric IncidentPlatform/ConsumerLag`) and as a dashboard entry
#: (`IncidentPlatform → QueueDepth, InFlightJobs`). Matching a bare space
#: after the namespace instead would swallow ordinary prose — "on the
#: IncidentPlatform CloudWatch namespace" would read as a metric named
#: CloudWatch.
_METRIC_PATH = re.compile(rf"\b{CUSTOM_NAMESPACE}/(\w+)")
_METRIC_DASHBOARD = re.compile(rf"\b{CUSTOM_NAMESPACE}\s*→\s*([\w,\s]+)")


def _named_metrics(doc: dict[str, Any]) -> set[str]:
    text = _all_text(doc)
    metrics = set(_METRIC_PATH.findall(text))
    for listing in _METRIC_DASHBOARD.findall(text):
        for chunk in listing.split(","):
            # "JobDeadLettered, JobCompleted (per JobType)" — the qualifier
            # after the metric name is prose, so keep the leading token only.
            words = chunk.split()
            if words:
                metrics.add(words[0])
    return metrics

_RUNBOOK_ID = re.compile(r"\brb-[\w-]+\b")

#: Prose naming an ECS service — "raise the desired count of the worker ECS
#: service". Mitigations are prose, not commands, so the command-line scan
#: above cannot see them, and "scale the worker ECS service" was two of the
#: nine R2-52 findings.
_PROSE_ECS_SERVICE = re.compile(r"`?([\w-]+)`?\s+ECS service\b")

#: Function words that can precede "ECS service" without naming one ("roll
#: back via ECS service force-new-deployment"). Anything else in that slot is
#: being used as a name, and a name has to exist.
_NOT_A_SERVICE_NAME = frozenset(
    {"a", "an", "the", "this", "that", "its", "each", "one", "new", "previous", "via", "per"}
)

#: A Redis key as a runbook writes one: no whitespace, at least one `:`
#: separator, and a lowercase leading segment. Extracted only from contexts
#: that are unambiguously keys — a `--pattern` argument or a backticked span —
#: so that `http://<task>:8000/...` in a curl line is never mistaken for one.
_KEYISH = re.compile(r"^[a-z][a-z0-9_.-]*:[\w:{}*.-]*$")
_PATTERN_ARG = re.compile(r"--pattern[= ]+'([^']+)'|--pattern[= ]+([^\s']+)")
_BACKTICKED = re.compile(r"`([^`]+)`")


def _referenced(pattern: re.Pattern[str], text: str) -> set[str]:
    """Every non-empty capture group of every match."""
    return {group for match in pattern.finditer(text) for group in match.groups() if group}


def _redis_keys(doc: dict[str, Any]) -> set[str]:
    text = _all_text(doc)
    candidates = _referenced(_PATTERN_ARG, text) | set(_BACKTICKED.findall(text))
    return {c for c in candidates if _KEYISH.match(c)}


def _documented_redis_prefixes() -> set[str]:
    """Literal leading segments of every key pattern in the docs/REDIS.md catalog.

    The catalog's first column is a backticked pattern. We keep the segments
    up to the first one carrying a `{placeholder}` or `*`, which is the part
    that has to match exactly for a key to exist at all.
    """
    text = (repo_root() / "docs" / "REDIS.md").read_text()
    prefixes = set()
    for line in text.splitlines():
        if not line.startswith("| `"):
            continue
        match = _BACKTICKED.search(line)
        if match:
            prefixes.add(_literal_prefix(match.group(1)))
    return prefixes


def _literal_prefix(key: str) -> str:
    """Leading `:`-segments of a key pattern that contain no wildcard."""
    literal = []
    for segment in key.split(":"):
        if "*" in segment or "{" in segment:
            break
        literal.append(segment)
    return ":".join(literal)


# ---------------------------------------------------------------------------
# Meta-guards — a lint that scanned nothing would pass everything
# ---------------------------------------------------------------------------


def test_the_scanners_actually_found_something() -> None:
    names = _terraform_names()
    assert len(_runbook_paths()) >= 8, "expected the full shipped runbook set"
    assert "/ecs/incident-platform/backend" in names["aws_cloudwatch_log_group"]
    assert "incident-platform/backend" in names["aws_ecr_repository"]
    assert "incident-platform-backend" in names["aws_ecs_service"]
    assert "incident-platform" in names["aws_ecs_cluster"]
    assert len(names["aws_cloudwatch_metric_alarm"]) >= 9
    assert len(_documented_redis_prefixes()) >= 15
    assert sum(len(_commands(doc)) for _, doc in _runbook_docs()) >= 20


# ---------------------------------------------------------------------------
# The lint
# ---------------------------------------------------------------------------


@_EACH_RUNBOOK
def test_every_command_parses_as_a_shell_command(filename: str, doc: dict[str, Any]) -> None:
    """A command an operator cannot paste is worse than no command at all.

    POSIX `shlex` is the same lexer a shell uses for quoting, so this catches
    exactly the class of fault that makes a line unusable: an unbalanced quote.
    It deliberately does not require the first token to be an executable —
    several steps are SQL or a CloudWatch metric path rather than a shell
    line, and those are legitimate. Quoting has to be right either way.
    """
    for step_id, command in _commands(doc):
        try:
            shlex.split(command)
        except ValueError as exc:
            pytest.fail(f"{filename} step {step_id!r} does not lex: {exc}\n  {command}")


@_EACH_RUNBOOK
def test_every_named_aws_resource_exists_in_terraform(filename: str, doc: dict[str, Any]) -> None:
    """Every AWS resource a command names must be declared in infra/."""
    names = _terraform_names()
    failures = []

    for step_id, command in _commands(doc):
        for kind, pattern in _FLAG_PATTERNS.items():
            for referenced in _referenced(pattern, command):
                if referenced not in names[kind]:
                    failures.append(
                        f"{filename} step {step_id!r} names {kind} {referenced!r}; "
                        f"infra declares {sorted(names[kind])}"
                    )
        for referenced in _LOG_GROUP.findall(command):
            if referenced not in names["aws_cloudwatch_log_group"]:
                failures.append(
                    f"{filename} step {step_id!r} names log group {referenced!r}; "
                    f"infra declares {sorted(names['aws_cloudwatch_log_group'])}"
                )

    assert not failures, "runbooks naming resources that do not exist:\n  " + "\n  ".join(failures)


@_EACH_RUNBOOK
def test_mitigations_scale_an_ecs_service_that_exists(filename: str, doc: dict[str, Any]) -> None:
    """"Scale the worker ECS service" is not a thing anyone can do here.

    Workers run in-process inside the backend task — `infra/ecs.tf` declares
    exactly two services, backend and frontend — so a responder who reaches
    for a worker service finds nothing and loses the time it takes to work
    that out. The instruction has to name the service that actually scales
    workers.
    """
    services = _terraform_names()["aws_ecs_service"]
    failures = []

    for text in _strings(doc):
        for referenced in _PROSE_ECS_SERVICE.findall(text):
            if referenced.lower() in _NOT_A_SERVICE_NAME:
                continue
            if referenced not in services:
                failures.append(
                    f"{filename} tells on-call to use the {referenced!r} ECS service; "
                    f"infra declares {sorted(services)}"
                )

    assert not failures, "runbooks naming an ECS service that does not exist:\n  " + "\n  ".join(
        failures
    )


@_EACH_RUNBOOK
def test_alarm_field_names_a_real_alarm(filename: str, doc: dict[str, Any]) -> None:
    """The `alarm:` field is how the console links an alarm to its runbook."""
    declared = doc.get("alarm")
    if declared is None:
        return
    alarms = _terraform_names()["aws_cloudwatch_metric_alarm"]
    assert declared in alarms, (
        f"{filename} claims alarm {declared!r}, which infra/cloudwatch.tf does not "
        f"declare. Declared: {sorted(alarms)}"
    )


@_EACH_RUNBOOK
def test_every_custom_metric_named_is_actually_emitted(filename: str, doc: dict[str, Any]) -> None:
    """`IncidentPlatform/Foo` in a runbook must be a metric the code publishes.

    This is the one that #160 and #174 would have broken silently: repointing
    an alarm from QueueDepth to ConsumerLag leaves the runbook telling on-call
    to open a graph that is flat by construction.
    """
    emitted = emitted_metrics()
    missing = sorted(metric for metric in _named_metrics(doc) if metric not in emitted)
    assert not missing, (
        f"{filename} points on-call at {CUSTOM_NAMESPACE} metrics nothing in "
        f"backend/app emits: {missing}"
    )


@_EACH_RUNBOOK
def test_every_redis_key_is_in_the_documented_catalog(filename: str, doc: dict[str, Any]) -> None:
    """A key prefix that does not exist makes its whole step a no-op.

    `redis-cli --scan --pattern 'jobs:create:*'` exits 0 and prints nothing,
    which reads exactly like "checked, nothing wrong" (R2-52).
    """
    documented = _documented_redis_prefixes()
    failures = [
        f"{filename} names Redis key {key!r} (prefix {_literal_prefix(key)!r}), "
        f"which docs/REDIS.md does not catalog"
        for key in sorted(_redis_keys(doc))
        if _literal_prefix(key) not in documented
    ]
    assert not failures, "\n  ".join(["runbooks naming uncatalogued Redis keys:"] + failures)


@_EACH_RUNBOOK
def test_no_runbook_recommends_a_whole_database_flush(filename: str, doc: dict[str, Any]) -> None:
    """`FLUSHDB`/`FLUSHALL` are unsafe on this Redis, and docs/REDIS.md says so.

    Both `delayed_queue` and `priority_queue` are durable state with no TTL
    and no rebuild path — a flush loses every pending retry silently. Naming
    the command in order to warn against it is fine; recommending it is not,
    so the ban is on mitigation and diagnosis text, not on the whole file.
    """
    actionable = _strings(
        {k: v for k, v in doc.items() if k in ("diagnosis_steps", "mitigation", "escalation")}
    )
    offenders = [
        text
        for text in actionable
        if re.search(r"\bFLUSH(DB|ALL)\b", text) and "Do NOT" not in text and "not use" not in text
    ]
    assert not offenders, (
        f"{filename} recommends flushing the whole Redis database — docs/REDIS.md "
        f"calls this too blunt (it also wipes delayed_queue and priority_queue, "
        f"which have no TTL and no rebuild):\n  " + "\n  ".join(offenders)
    )


@_EACH_RUNBOOK
def test_cache_node_type_advice_starts_from_the_deployed_size(
    filename: str, doc: dict[str, Any]
) -> None:
    """"Scale up one size" is only actionable from the size actually deployed.

    A runbook naming `cache.t3.small -> cache.t3.medium` against a deployed
    `cache.t3.micro` tells on-call to make a change that is already two sizes
    off, so the configured type must appear among the ones it names.
    """
    named = set(_NODE_TYPE.findall(_all_text(doc)))
    if not named:
        return
    deployed = _terraform_names()["aws_elasticache_replication_group"]
    assert named & deployed, (
        f"{filename} names cache node types {sorted(named)}, none of which is the "
        f"deployed {sorted(deployed)}"
    )


def test_every_runbook_referenced_by_an_alarm_or_an_slo_exists() -> None:
    """Both directions of the link, so neither side can drift alone.

    The SLO half is #174: `SLODefinition.runbook_id` is rendered straight into
    the fast-burn alert body, so a stale id ships to whoever is paged.
    """
    from app.services.slo import SLOS

    shipped = {rb["id"] for rb in runbooks.list_all()}
    cloudwatch = (repo_root() / "infra" / "cloudwatch.tf").read_text()

    referenced = {rb_id for rb_id in _RUNBOOK_ID.findall(cloudwatch)}
    referenced |= {slo.runbook_id for slo in SLOS if slo.runbook_id}

    missing = sorted(referenced - shipped)
    assert not missing, (
        f"alarms/SLOs point at runbooks that do not ship: {missing}. Shipped: {sorted(shipped)}"
    )
