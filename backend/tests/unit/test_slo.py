"""Unit tests for the SLO computation."""

from app.services.slo import SLODefinition, _state


def _def(target: float = 0.99) -> SLODefinition:
    return SLODefinition(
        id="t", name="t", description="t", target=target, window_hours=24, runbook_id="rb"
    )


def test_no_traffic_is_healthy() -> None:
    """An idle service should not look broken in the dashboard."""
    s = _state(_def(), total=0, failed=0)
    assert s.healthy is True
    assert s.current == 1.0
    assert s.budget_remaining_pct == 100.0
    assert s.burn_rate == 0.0


def test_zero_failures_full_budget() -> None:
    s = _state(_def(0.99), total=100, failed=0)
    assert s.healthy is True
    assert s.current == 1.0
    assert s.budget_remaining_pct == 100.0
    assert s.burn_rate == 0.0


def test_at_target_burn_rate_one() -> None:
    """Exactly at the target = consuming budget at the SLO-window rate, burn=1×."""
    s = _state(_def(0.99), total=100, failed=1)
    assert s.healthy is True  # 0.99 >= 0.99
    assert s.current == 0.99
    assert abs(s.burn_rate - 1.0) < 1e-9
    assert abs(s.budget_remaining_pct - 0.0) < 1e-9


def test_above_target_breaches_and_burn_above_one() -> None:
    s = _state(_def(0.99), total=100, failed=5)
    assert s.healthy is False
    assert abs(s.current - 0.95) < 1e-9
    assert abs(s.burn_rate - 5.0) < 1e-9  # 0.05 / 0.01
    # Budget over-consumed; clamped at -100%.
    assert s.budget_remaining_pct == -100.0


def test_partial_consumption_reports_remaining_share() -> None:
    """50% of the budget used → 50% remaining."""
    s = _state(_def(0.99), total=200, failed=1)  # 0.5% failure rate vs 1% allowed
    assert abs(s.budget_remaining_pct - 50.0) < 1e-6
    assert abs(s.burn_rate - 0.5) < 1e-6
    assert s.healthy is True


def test_target_100pct_any_failure_breaches() -> None:
    s = _state(_def(1.0), total=100, failed=1)
    assert s.healthy is False
    assert s.burn_rate == float("inf")
