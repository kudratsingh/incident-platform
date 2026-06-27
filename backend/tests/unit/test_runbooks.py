"""Unit tests for the runbook loader."""

from app.services import runbooks


def test_loads_runbooks_from_repo() -> None:
    """The repo ships at least the alarms-and-SLOs set; loader returns them all."""
    items = runbooks.list_all()
    ids = {rb["id"] for rb in items}
    expected = {
        "rb-alb-5xx",
        "rb-ecs-tasks-low",
        "rb-rds-cpu-high",
        "rb-redis-memory-low",
        "rb-queue-depth-high",
        "rb-slo-job-completion",
        "rb-slo-dispatch-latency",
    }
    assert expected.issubset(ids)


def test_get_returns_full_structure() -> None:
    rb = runbooks.get("rb-slo-job-completion")
    assert rb is not None
    assert rb["title"]
    assert rb["severity"] in ("low", "medium", "high", "critical")
    assert isinstance(rb["diagnosis_steps"], list)
    assert isinstance(rb["mitigation"], list)


def test_get_unknown_returns_none() -> None:
    assert runbooks.get("does-not-exist") is None


def test_every_runbook_has_required_fields() -> None:
    """If a runbook ships, it should at least name itself and explain when it fires."""
    for rb in runbooks.list_all():
        assert rb["id"]
        assert rb["title"]
        assert rb.get("summary"), f"runbook {rb['id']} missing a summary"
