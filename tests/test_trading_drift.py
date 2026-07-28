"""Continuous evaluation and deterioration — Phase 10 completion gate 2.

Gate 2: detect model and strategy deterioration.

The asymmetry is the safety argument and gets the most attention here:
descending the ladder is autonomous, ascending it is not, and no amount of
good-looking evidence lets an autonomous actor conclude its own deteriorating
model has recovered.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import drift as D
from olympus.trading.clock import FixedClock
from olympus.trading.errors import ConfigurationError, GovernanceViolation
from olympus.trading.governance import Actor

T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
OPERATOR = Actor.operator("alice", token="tok")
OLYMPUS = Actor.autonomous("drift-monitor")


def sample(n, mean=0.0, spread=1.0, seed=0):
    rng = random.Random(seed)
    return [rng.gauss(mean, spread) for _ in range(n)]


@pytest.fixture()
def monitor():
    return D.DeteriorationMonitor(clock=FixedClock(T0))


# --- statistics -------------------------------------------------------------

def test_psi_is_near_zero_for_two_draws_from_the_same_distribution():
    psi = D.population_stability_index(sample(500, seed=1), sample(500, seed=2))
    assert psi is not None and psi < D.PSI_MINOR


def test_psi_is_large_for_a_shifted_distribution():
    psi = D.population_stability_index(sample(500, 0.0, 1.0, seed=1),
                                       sample(500, 3.0, 1.0, seed=2))
    assert psi is not None and psi > D.PSI_MAJOR


def test_no_verdict_is_issued_below_the_minimum_sample():
    """Distribution comparisons on small samples find drift reliably and wrongly."""
    assert D.population_stability_index(sample(10), sample(10)) is None
    assert D.ks_statistic(sample(10), sample(10)) is None


def test_psi_is_finite_even_when_a_bin_is_empty():
    reference = sample(200, seed=3)
    current = [100.0] * 200          # every observation outside the reference range
    psi = D.population_stability_index(reference, current)
    assert psi is not None and psi == psi and psi != float("inf")


def test_a_constant_reference_yields_no_psi_rather_than_a_crash():
    assert D.population_stability_index([1.0] * 100, sample(100)) is None


def test_ks_detects_a_shift_the_reference_would_not_produce():
    ks = D.ks_statistic(sample(300, 0.0, seed=1), sample(300, 2.0, seed=2))
    assert ks is not None and ks > D.ks_critical(300, 300)


def test_mean_shift_is_expressed_in_reference_standard_deviations():
    shift = D.mean_shift(sample(500, 0.0, 1.0, seed=1),
                         sample(500, 2.0, 1.0, seed=2))
    assert shift is not None and 1.5 < shift < 2.5


# --- signals ----------------------------------------------------------------

def test_a_drift_signal_carries_the_numbers_it_was_computed_from():
    """An operator has to be able to check the number, not trust the verdict."""
    signal = D.detect_distribution_drift(
        sample(300, seed=1), sample(300, 3.0, seed=2),
        kind=D.DriftKind.MODEL, subject="kronos", metric="forecast_mean")
    assert signal.drifted is True
    assert signal.statistic is not None
    assert "PSI" in signal.detail and "KS" in signal.detail
    assert signal.reference_n == 300 and signal.current_n == 300


def test_an_insufficient_sample_reports_no_verdict_and_says_so():
    signal = D.detect_distribution_drift([1.0, 2.0], [3.0, 4.0],
                                         kind=D.DriftKind.DATA, subject="s",
                                         metric="m")
    assert signal.severity is D.Severity.NONE
    assert "no verdict issued" in signal.detail


def test_a_metric_that_improved_is_not_flagged_as_drift():
    """A latency detector that fired on latency dropping would train operators
    to ignore it."""
    signal = D.detect_metric_regression([100.0] * 50, [50.0] * 50,
                                        kind=D.DriftKind.LATENCY,
                                        subject="venue", metric="latency_ms")
    assert signal.severity is D.Severity.NONE
    assert "better" in signal.detail


def test_a_metric_that_doubled_is_critical():
    signal = D.detect_metric_regression([10.0] * 50, [25.0] * 50,
                                        kind=D.DriftKind.SLIPPAGE,
                                        subject="BTCUSDT", metric="slippage_bps")
    assert signal.severity is D.Severity.CRITICAL


def test_calibration_drift_measures_the_confidence_to_accuracy_gap():
    before_conf = [0.8] * 100
    before_hits = [True] * 80 + [False] * 20      # calibrated
    after_conf = [0.8] * 100
    after_hits = [True] * 45 + [False] * 55       # no longer
    signal = D.detect_calibration_drift(before_conf, before_hits, after_conf,
                                        after_hits, subject="kronos")
    assert signal.severity is D.Severity.CRITICAL
    assert signal.current_value > signal.reference_value


def test_correlations_converging_toward_one_are_major():
    """Convergence removes the diversification a limit was sized against."""
    rng = random.Random(7)
    independent = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(200)]
    together = [(x := rng.gauss(0, 1), x + rng.gauss(0, 0.05)) for _ in range(200)]
    signal = D.detect_correlation_change(independent, together, subject="btc/eth")
    assert signal.severity is D.Severity.MAJOR
    assert "diversified" in signal.detail


# --- gate 2: the ladder -----------------------------------------------------

def major(subject="m", metric="x"):
    return D.DriftSignal(kind=D.DriftKind.MODEL, subject=subject, metric=metric,
                         severity=D.Severity.MAJOR, detail="major")


def critical(subject="m", metric="y"):
    return D.DriftSignal(kind=D.DriftKind.MODEL, subject=subject, metric=metric,
                         severity=D.Severity.CRITICAL, detail="critical")


def test_one_major_signal_flags_a_component():
    health = D.assess("m", kind="model", signals=[major()],
                      last_evaluated_at=T0, now=T0)
    assert health.state is D.ComponentState.FLAGGED
    assert health.operating is True


def test_two_major_signals_restrict_it():
    health = D.assess("m", kind="model", signals=[major(metric="a"),
                                                  major(metric="b")],
                      last_evaluated_at=T0, now=T0)
    assert health.state is D.ComponentState.RESTRICTED


def test_a_critical_signal_moves_it_to_shadow():
    health = D.assess("m", kind="model", signals=[critical()],
                      last_evaluated_at=T0, now=T0)
    assert health.state is D.ComponentState.SHADOW
    assert health.operating is False, "shadow output must not reach a decision"


def test_two_critical_signals_disable_it():
    health = D.assess("m", kind="model",
                      signals=[critical(metric="a"), critical(metric="b")],
                      last_evaluated_at=T0, now=T0)
    assert health.state is D.ComponentState.DISABLED


def test_a_hard_performance_floor_disables_regardless_of_drift():
    """A strategy in a 30% drawdown is not healthy because its feature
    distributions are stable."""
    health = D.assess("s", kind="strategy", signals=[],
                      performance=D.PerformanceSnapshot(max_drawdown=0.30,
                                                        directional_accuracy=0.6,
                                                        failure_rate=0.0,
                                                        sample=200),
                      last_evaluated_at=T0, now=T0)
    assert health.state is D.ComponentState.DISABLED
    assert any("hard threshold crossed" in r for r in health.reasons)


def test_an_unmeasured_hard_threshold_flags_rather_than_passes():
    """A component must not stay healthy by never being measured."""
    health = D.assess("s", kind="strategy", signals=[],
                      performance=D.PerformanceSnapshot(sample=200),
                      last_evaluated_at=T0, now=T0)
    assert health.state is D.ComponentState.FLAGGED
    assert any("unverified" in r for r in health.reasons)


def test_a_stale_evaluation_flags_a_component_that_looks_perfect():
    """'It performed well historically' is not a licence to keep operating."""
    health = D.assess("m", kind="model", signals=[],
                      performance=D.PerformanceSnapshot(directional_accuracy=0.9,
                                                        max_drawdown=0.01,
                                                        failure_rate=0.0),
                      last_evaluated_at=T0, now=T0 + timedelta(days=40))
    assert health.state is D.ComponentState.FLAGGED
    assert any("past the" in r for r in health.reasons)


def test_a_never_evaluated_component_is_flagged():
    health = D.assess("m", kind="model", signals=[], last_evaluated_at=None,
                      now=T0)
    assert health.state is D.ComponentState.FLAGGED
    assert any("never evaluated" in r for r in health.reasons)


def test_assessment_never_relaxes_an_existing_state():
    health = D.assess("m", kind="model", signals=[], last_evaluated_at=T0,
                      now=T0, current=D.ComponentState.SHADOW)
    assert health.state is D.ComponentState.SHADOW


# --- the asymmetry ----------------------------------------------------------

def test_demotion_is_autonomous(monitor):
    health = monitor.restrict("kronos", kind="model",
                              to=D.ComponentState.SHADOW,
                              reason="forecast error doubled", actor=OLYMPUS)
    assert health.state is D.ComponentState.SHADOW
    assert monitor.health("kronos").operating is False


def test_an_autonomous_actor_cannot_reinstate_a_component(monitor):
    """The load-bearing refusal: a self-evolving system must not be able to
    conclude that its own deteriorating model has recovered."""
    monitor.restrict("kronos", kind="model", to=D.ComponentState.SHADOW,
                     reason="degraded", actor=OLYMPUS)
    with pytest.raises(GovernanceViolation):
        monitor.reinstate("kronos", kind="model", to=D.ComponentState.HEALTHY,
                          reason="it looks better now", actor=OLYMPUS)
    assert monitor.health("kronos").state is D.ComponentState.SHADOW


def test_an_operator_can_reinstate_with_a_reason(monitor):
    monitor.restrict("kronos", kind="model", to=D.ComponentState.SHADOW,
                     reason="degraded", actor=OLYMPUS)
    health = monitor.reinstate("kronos", kind="model",
                               to=D.ComponentState.HEALTHY,
                               reason="refitted and re-validated on Q2 data",
                               actor=OPERATOR)
    assert health.state is D.ComponentState.HEALTHY
    assert "alice" in health.reasons[0]


def test_reinstatement_requires_a_written_reason(monitor):
    monitor.restrict("k", kind="model", to=D.ComponentState.SHADOW,
                     reason="x", actor=OLYMPUS)
    with pytest.raises(ConfigurationError):
        monitor.reinstate("k", kind="model", to=D.ComponentState.HEALTHY,
                          reason="  ", actor=OPERATOR)


def test_restrict_refuses_to_relax(monitor):
    monitor.restrict("k", kind="model", to=D.ComponentState.SHADOW,
                     reason="x", actor=OLYMPUS)
    with pytest.raises(GovernanceViolation):
        monitor.restrict("k", kind="model", to=D.ComponentState.FLAGGED,
                         reason="better", actor=OLYMPUS)


def test_reinstate_refuses_to_demote(monitor):
    with pytest.raises(ConfigurationError):
        monitor.reinstate("k", kind="model", to=D.ComponentState.DISABLED,
                          reason="worse", actor=OPERATOR)


# --- persistence ------------------------------------------------------------

def test_health_survives_a_new_monitor_instance(monitor):
    monitor.evaluate("kronos", kind="model", signals=[critical("kronos")])
    fresh = D.DeteriorationMonitor(clock=FixedClock(T0))
    assert fresh.health("kronos").state is D.ComponentState.SHADOW


def test_re_evaluating_at_the_same_state_keeps_the_original_since(monitor):
    """How long a component has been degraded is itself the signal."""
    clock = FixedClock(T0)
    monitor = D.DeteriorationMonitor(clock=clock)
    first = monitor.evaluate("m", kind="model", signals=[major("m")])
    clock.advance(timedelta(days=3))
    second = monitor.evaluate("m", kind="model", signals=[major("m")])
    assert second.state is first.state
    assert second.since == first.since


def test_degraded_components_are_listed_worst_first(monitor):
    monitor.evaluate("a", kind="model", signals=[major("a")])
    monitor.evaluate("b", kind="model", signals=[critical("b"),
                                                 critical("b", metric="z")])
    assert [h.component_id for h in monitor.degraded()] == ["b", "a"]


def test_the_report_names_the_thresholds_it_used(monitor):
    monitor.evaluate("a", kind="model", signals=[major("a")])
    report = monitor.report()
    assert report["degraded"] == ["a"]
    assert "max_drawdown" in report["thresholds"]


# --- the schedule -----------------------------------------------------------

def test_the_standard_schedule_covers_every_specified_measurement():
    names = {job.name for job in D.STANDARD_JOBS}
    for required in ("forecast_by_instrument", "forecast_by_timeframe",
                     "forecast_by_regime", "strategy_by_regime", "cost_impact",
                     "model_drift", "data_drift", "feature_drift",
                     "calibration_drift", "failure_rate", "latency", "slippage",
                     "liquidity", "correlation", "microstructure"):
        assert required in names, required


def test_every_job_is_due_before_it_has_ever_run():
    schedule = D.EvaluationSchedule(clock=FixedClock(T0))
    assert len(schedule.due()) == len(D.STANDARD_JOBS)


def test_a_job_stops_being_due_until_its_interval_elapses():
    clock = FixedClock(T0)
    schedule = D.EvaluationSchedule(clock=clock)
    schedule.mark_ran("data_drift")
    assert "data_drift" not in {j.name for j in schedule.due()}
    clock.advance(timedelta(hours=2))
    assert "data_drift" in {j.name for j in schedule.due()}


def test_marking_an_unknown_job_is_refused():
    schedule = D.EvaluationSchedule(clock=FixedClock(T0))
    with pytest.raises(ConfigurationError):
        schedule.mark_ran("does_not_exist")
