"""Evaluation, and the refusal to spin a small sample into a result.

The arithmetic tests are against hand-computed fixtures rather than against the
implementation's own output, because a metric that agrees with itself proves
nothing. The comparison tests are about the opposite failure: a difference that
looks like an improvement and is not.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from olympus.trading.contracts import (DataQuality, ForecastPath,
                                       ForecastResult)
from olympus.trading.errors import ConfigurationError
from olympus.trading.evaluate import (ComparisonReport, ForecastEvaluator,
                                      ForecastMetrics, ForecastSample,
                                      StrategyComparison, compare_to_baseline,
                                      coverage, crps_ensemble,
                                      directional_accuracy, kronos_is_valuable,
                                      kronos_verdict, mae, mape,
                                      paired_bootstrap, pinball_loss,
                                      quantile_level, rmse, run_strategy_comparison,
                                      sign_test, smape)
from olympus.trading.perf import PerformanceReport, ReturnStats

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
KEY = "NASDAQ:AAPL"


# --- primitive metrics, hand-computed --------------------------------------

def test_mae_and_rmse_against_hand_computed_values():
    assert mae([1.0, 2.0, 3.0], [1.0, 2.0, 4.0]) == pytest.approx(1 / 3)
    # sqrt((3^2 + 4^2) / 2) = sqrt(12.5)
    assert rmse([0.0, 0.0], [3.0, 4.0]) == pytest.approx(math.sqrt(12.5))
    assert mae([], []) is None


def test_mape_skips_zero_actuals_rather_than_returning_infinity():
    assert mape([110.0], [100.0]) == pytest.approx(0.10)
    # The zero actual is skipped; only the usable point counts.
    assert mape([110.0, 5.0], [100.0, 0.0]) == pytest.approx(0.10)
    assert mape([1.0], [0.0]) is None


def test_smape_is_symmetric_where_mape_is_not():
    # |110-100| / ((110+100)/2) = 10/105
    assert smape([110.0], [100.0]) == pytest.approx(10 / 105)
    over = smape([200.0], [100.0])
    under = smape([100.0], [200.0])
    assert over == pytest.approx(under)                  # the whole point
    assert mape([200.0], [100.0]) != pytest.approx(mape([100.0], [200.0]))


def test_directional_accuracy_does_not_credit_a_flat_market():
    assert directional_accuracy([1.0, -1.0, 0.0], [2.0, -3.0, 0.0]) == 1.0
    assert directional_accuracy([1.0], [0.0]) == 0.0     # predicted up, no move
    assert directional_accuracy([1.0, 1.0], [1.0, -1.0]) == 0.5


def test_pinball_loss_is_asymmetric_in_the_documented_direction():
    # tau=0.9, under-prediction (actual above the quantile) costs 0.9 x error
    assert pinball_loss([8.0], [10.0], 0.9) == pytest.approx(1.8)
    # ...over-prediction costs only 0.1 x error, which is what makes it a p90
    assert pinball_loss([12.0], [10.0], 0.9) == pytest.approx(0.2)
    with pytest.raises(ConfigurationError):
        pinball_loss([1.0], [1.0], 1.0)


def test_crps_of_an_ensemble_matches_the_empirical_formula():
    # mean|X-y| = 1, mean|X-X'| = 1  ->  CRPS = 1 - 0.5 = 0.5
    assert crps_ensemble([1.0, 3.0], 2.0) == pytest.approx(0.5)
    # A single sample degenerates to absolute error.
    assert crps_ensemble([1.0], 3.0) == pytest.approx(2.0)
    # A tight but wrong ensemble scores worse than a wide one containing truth.
    assert crps_ensemble([10.0, 10.0], 0.0) > crps_ensemble([-5.0, 5.0], 0.0)


def test_coverage_counts_inclusive_bounds():
    assert coverage([0.0, 0.0], [10.0, 10.0], [5.0, 15.0]) == 0.5
    assert coverage([0.0], [10.0], [10.0]) == 1.0
    with pytest.raises(ConfigurationError):
        coverage([0.0], [1.0, 2.0], [0.5])


def test_quantile_labels_parse_or_refuse():
    assert quantile_level("p05") == pytest.approx(0.05)
    assert quantile_level("p95") == pytest.approx(0.95)
    assert quantile_level("q0.1") == pytest.approx(0.1)
    assert quantile_level("0.5") == pytest.approx(0.5)
    assert quantile_level("median") is None
    assert quantile_level("p100") is None


def test_length_mismatch_is_refused_not_truncated():
    with pytest.raises(ConfigurationError):
        mae([1.0, 2.0], [1.0])


# --- the evaluator ----------------------------------------------------------

def make_forecast(mean, *, quantiles=None, paths=(), abstained=False,
                  created_at=None, horizon=None, model_version="kronos/1"):
    common = dict(instrument_key=KEY, timeframe="1d", input_start=T0,
                  input_end=T0 + timedelta(days=1),
                  created_at=created_at or (T0 + timedelta(days=1)),
                  horizon=horizon if horizon is not None else len(mean))
    if abstained:
        return ForecastResult.abstain(reason="not enough bars",
                                      code="DATA_INSUFFICIENT", **common)
    return ForecastResult(mean_close=tuple(mean),
                          quantiles=quantiles or {},
                          paths=tuple(ForecastPath(closes=tuple(p))
                                      for p in paths),
                          model_version=model_version, uncertainty=0.3,
                          **common)


def test_abstentions_are_counted_and_never_silently_dropped():
    """A model graded only on the days it felt confident looks excellent."""
    ev = ForecastEvaluator(label="kronos")
    assert ev.add(make_forecast([], abstained=True), [101.0]) is None
    ev.add(make_forecast([101.0]), [101.0])
    metrics = ev.metrics()
    assert metrics.n == 1
    assert metrics.abstentions == 1
    assert metrics.abstention_rate == pytest.approx(0.5)
    assert any("abstentions" in w for w in metrics.warnings)


def test_a_graded_forecast_produces_hand_checkable_numbers():
    ev = ForecastEvaluator()
    sample = ev.add(make_forecast([101.0, 102.0]), [101.0, 103.0],
                    last_close=100.0)
    assert sample.n_steps == 2
    assert sample.mae == pytest.approx(0.5)              # (0 + 1) / 2
    assert sample.rmse == pytest.approx(math.sqrt(0.5))  # sqrt((0 + 1) / 2)
    assert sample.predicted_return == pytest.approx(0.02)
    assert sample.actual_return == pytest.approx(0.03)
    assert sample.direction_correct is True
    metrics = ev.metrics()
    assert metrics.hit_rate == 1.0
    assert metrics.n == 1


def test_a_wrong_direction_call_is_recorded_as_a_miss():
    ev = ForecastEvaluator()
    sample = ev.add(make_forecast([105.0]), [95.0], last_close=100.0)
    assert sample.direction_correct is False
    assert ev.metrics().hit_rate == 0.0


def test_metrics_are_none_when_undefined_never_zero():
    """A metric that reports 0.0 when it could not be computed makes an
    unevaluated model look perfect."""
    metrics = ForecastEvaluator().metrics()
    assert metrics.n == 0
    assert metrics.mae is None and metrics.rmse is None
    assert metrics.hit_rate is None and metrics.calibration_error is None


def test_calibration_measures_whether_the_bands_cover_at_their_stated_rate():
    """A p05/p95 band should contain the outcome 90% of the time. Here it
    contains it 90% of the time, and the evaluator says so."""
    ev = ForecastEvaluator()
    for i in range(100):
        fc = make_forecast([100.0], quantiles={"p05": (95.0,), "p95": (105.0,)},
                           created_at=T0 + timedelta(days=i + 1))
        realised = 100.0 if i < 90 else 110.0            # 10 escapes, upward
        ev.add(fc, [realised], last_close=100.0)
    metrics = ev.metrics()
    assert metrics.interval_coverage["p05-p95"] == pytest.approx(0.90)
    assert metrics.expected_interval_coverage["p05-p95"] == pytest.approx(0.90)
    assert metrics.calibration["p95"] == pytest.approx(0.90)
    assert metrics.expected_calibration["p95"] == pytest.approx(0.95)
    assert metrics.calibration_error == pytest.approx((0.05 + 0.05 + 0.0) / 3)


def test_an_overconfident_band_shows_up_as_calibration_error():
    """Bands that are too narrow are the failure mode that matters for risk:
    they under-report how wrong the forecast can be."""
    narrow = ForecastEvaluator()
    for i in range(50):
        fc = make_forecast([100.0], quantiles={"p05": (99.9,), "p95": (100.1,)},
                           created_at=T0 + timedelta(days=i + 1))
        narrow.add(fc, [100.0 + (5.0 if i % 2 else -5.0)], last_close=100.0)
    metrics = narrow.metrics()
    assert metrics.interval_coverage["p05-p95"] == 0.0
    assert metrics.calibration_error > 0.3


def test_path_ensembles_are_scored_with_crps():
    ev = ForecastEvaluator()
    sample = ev.add(make_forecast([2.0], paths=[[1.0], [3.0]]), [2.0],
                    last_close=2.0)
    assert sample.crps == pytest.approx(0.5)
    assert ev.metrics().crps == pytest.approx(0.5)


def test_horizon_mismatch_is_graded_on_the_overlap_and_warned_about():
    ev = ForecastEvaluator()
    sample = ev.add(make_forecast([101.0, 102.0, 103.0]), [101.0])
    assert sample.n_steps == 1
    assert any("horizon" in w for w in ev.metrics().warnings)


# --- comparison and significance -------------------------------------------

def samples_from(errors, *, directions=None):
    """Build graded samples with the given absolute errors."""
    out = []
    for i, err in enumerate(errors):
        correct = True if directions is None else directions[i]
        out.append(ForecastSample(
            instrument_key=KEY, created_at=T0 + timedelta(days=i + 1),
            horizon=1, n_steps=1, mae=err, rmse=err, mape=err / 100.0,
            smape=err / 100.0, directional_accuracy=1.0 if correct else 0.0,
            direction_correct=correct, predicted_return=0.01,
            actual_return=0.01 if correct else -0.01))
    return out


def test_a_random_noise_improvement_is_not_reported_as_a_win():
    """The test this module exists for: a point-estimate improvement on a small
    noisy sample must not pass the significance check."""
    rng = random.Random(11)
    baseline_errors = [rng.uniform(1.0, 5.0) for _ in range(20)]
    # Model is better on 12 of 20 and worse on 8 — the shape of every "our
    # model wins" chart drawn from noise.
    model_errors = [e * (0.95 if i % 5 < 3 else 1.15)
                    for i, e in enumerate(baseline_errors)]
    report = compare_to_baseline(samples_from(model_errors),
                                 samples_from(baseline_errors),
                                 label="noise", seed=5, iterations=500)
    assert report.n_pairs == 20
    assert not report.significant_improvement
    assert report.verdict == "no_significant_difference"
    assert any("small sample" in w for w in report.warnings)


def test_a_shuffled_identical_error_set_shows_no_difference():
    """Same errors, different order: nothing to find, and nothing is found."""
    rng = random.Random(3)
    errors = [rng.uniform(1.0, 5.0) for _ in range(60)]
    shuffled = list(errors)
    rng.shuffle(shuffled)
    report = compare_to_baseline(samples_from(shuffled), samples_from(errors),
                                 seed=1, iterations=500)
    assert not report.significant_improvement
    assert report.significance["absolute_error"].p_value > 0.05


def test_a_real_improvement_is_detected():
    """The check must be able to say yes, or it is not a check."""
    rng = random.Random(19)
    baseline_errors = [rng.uniform(2.0, 6.0) for _ in range(100)]
    model_errors = [e * 0.5 for e in baseline_errors]
    report = compare_to_baseline(samples_from(model_errors),
                                 samples_from(baseline_errors),
                                 seed=2, iterations=500)
    assert report.improvements["mae"] == pytest.approx(0.5, abs=1e-9)
    assert report.significant_improvement
    assert report.verdict == "model_better"
    assert report.significance["absolute_error_sign"].significant


def test_a_worse_model_is_reported_as_worse_not_as_no_difference():
    baseline_errors = [2.0] * 60
    model_errors = [3.0] * 60
    report = compare_to_baseline(samples_from(model_errors),
                                 samples_from(baseline_errors),
                                 seed=2, iterations=400)
    assert report.verdict == "baseline_better"
    assert report.improvements["mae"] < 0


def test_unpaired_comparisons_are_refused():
    with pytest.raises(ConfigurationError):
        compare_to_baseline(samples_from([1.0, 2.0]), samples_from([1.0]))


def test_the_bootstrap_is_seeded_and_reproducible():
    diffs = [random.Random(4).uniform(-1, 1) for _ in range(40)]
    a = paired_bootstrap(diffs, seed=7, iterations=300)
    b = paired_bootstrap(diffs, seed=7, iterations=300)
    c = paired_bootstrap(diffs, seed=8, iterations=300)
    assert (a.p_value, a.ci_low, a.ci_high) == (b.p_value, b.ci_low, b.ci_high)
    assert a.seed == 7 and a.iterations == 300
    # A different seed may differ slightly; the point is that both are recorded.
    assert c.seed == 8


def test_a_bootstrap_whose_interval_straddles_zero_is_not_significant():
    result = paired_bootstrap([0.1, -0.1] * 30, seed=1, iterations=400)
    assert not result.significant


def test_sign_test_is_exact_and_matches_the_binomial_by_hand():
    # 8 heads in 10: two-sided p = 2 x P(X >= 8) = 2 x 56/1024
    assert sign_test(8, 10).p_value == pytest.approx(2 * 56 / 1024)
    assert not sign_test(8, 10).significant
    assert sign_test(20, 20).p_value == pytest.approx(2 / 2 ** 20)
    assert sign_test(20, 20).significant
    assert sign_test(0, 0).p_value == 1.0
    with pytest.raises(ConfigurationError):
        sign_test(5, 2)


# --- strategy comparison and the verdict ------------------------------------

def curve(per_bar_return, n=41, start=100000.0):
    equity, value = [], start
    for i in range(n):
        equity.append((T0 + timedelta(days=i), Decimal(str(round(value, 6)))))
        value *= (1.0 + per_bar_return)
    return tuple(equity)


def arm(per_bar_return, *, n_trades=12, fees="150", sharpe=1.0, n=41,
        alternate=None):
    points = curve(per_bar_return, n=n)
    if alternate is not None:
        # An arm whose per-bar edge flips sign: the same total return, no
        # consistent advantage.
        equity, value = [], 100000.0
        for i in range(n):
            equity.append((T0 + timedelta(days=i), Decimal(str(round(value, 6)))))
            value *= (1.0 + (alternate if i % 2 else -alternate))
        points = tuple(equity)
    total = float(points[-1][1]) / float(points[0][1]) - 1.0
    report = PerformanceReport(
        gross=ReturnStats(total_return=total + 0.001, sharpe=sharpe,
                          max_drawdown=0.05),
        net=ReturnStats(total_return=total, sharpe=sharpe, max_drawdown=0.05),
        n_bars=n, n_trades=n_trades, start=points[0][0], end=points[-1][0],
        starting_equity=points[0][1], ending_equity=points[-1][1],
        total_fees=Decimal(fees))
    return SimpleNamespace(report=report, equity_curve=points, trades=(),
                           orders=(), intents=(), decisions=())


def comparison(**kw):
    defaults = dict(label="kronos vs baseline", kronos=arm(0.002),
                    baseline=arm(0.001), out_of_sample=True, seed=3,
                    iterations=400)
    defaults.update(kw)
    return StrategyComparison(**defaults)


def test_kronos_is_valuable_defaults_to_false_on_anything_that_is_not_evidence():
    assert kronos_is_valuable(None) is False
    assert kronos_is_valuable({"net_return_delta": 999}) is False
    assert kronos_is_valuable(SimpleNamespace(net_return_delta=999)) is False


def test_an_in_sample_win_is_never_valuable():
    """However large. In-sample improvement is not evidence of anything."""
    comp = comparison(out_of_sample=False)
    assert comp.net_return_delta > 0
    assert kronos_is_valuable(comp) is False
    assert "out-of-sample" in " ".join(kronos_verdict(comp).reasons)


def test_a_costless_backtest_cannot_produce_a_verdict():
    comp = comparison(kronos=arm(0.002, fees="0"), baseline=arm(0.001, fees="0"))
    verdict = kronos_verdict(comp)
    assert verdict.valuable is False
    assert verdict.checks["costs_modelled"] is False
    assert any("cost" in note for note in comp.notes)


def test_an_insignificant_win_does_not_pass():
    """A per-bar edge that flips sign has the right average and the wrong
    consistency — exactly what a bootstrap is for."""
    comp = comparison(kronos=arm(0.002, alternate=0.004), baseline=arm(0.002))
    verdict = kronos_verdict(comp)
    assert verdict.checks.get("significant") is False
    assert verdict.valuable is False


def test_too_few_observations_cannot_pass():
    comp = comparison(kronos=arm(0.002, n=12), baseline=arm(0.001, n=12))
    verdict = kronos_verdict(comp)
    assert verdict.checks["enough_observations"] is False
    assert verdict.valuable is False


def test_an_arm_that_never_traded_is_not_a_comparison():
    comp = comparison(kronos=arm(0.002, n_trades=0))
    assert kronos_verdict(comp).checks["kronos_traded"] is False
    assert kronos_is_valuable(comp) is False


def test_misaligned_curves_are_refused_rather_than_compared():
    comp = comparison(kronos=arm(0.002, n=41), baseline=arm(0.001, n=30))
    assert comp.comparable is False
    assert comp.significance is None
    assert any("not aligned" in note for note in comp.notes)
    assert kronos_is_valuable(comp) is False


def test_a_worse_sharpe_blocks_the_verdict():
    comp = comparison(kronos=arm(0.002, sharpe=0.4), baseline=arm(0.001, sharpe=1.5))
    assert kronos_verdict(comp).checks["risk_adjusted_not_worse"] is False
    assert kronos_is_valuable(comp) is False


def test_a_genuine_out_of_sample_net_improvement_does_pass():
    """The rule must be capable of saying yes — otherwise it is decoration
    rather than a decision procedure."""
    comp = comparison()
    verdict = kronos_verdict(comp)
    assert verdict.reasons == ()
    assert all(verdict.checks.values())
    assert verdict.valuable is True
    assert kronos_is_valuable(comp) is True
    assert comp.net_return_delta > 0
    assert comp.significance.significant


def test_the_verdict_records_every_criterion_it_checked():
    verdict = kronos_verdict(comparison(out_of_sample=False))
    for name in ("out_of_sample", "costs_modelled", "kronos_traded",
                 "comparable", "net_return_improved", "significant"):
        assert name in verdict.checks
    assert verdict.to_dict()["valuable"] is False


def test_run_strategy_comparison_uses_one_engine_builder_for_both_arms():
    """Both arms must see the same data, limits and costs, or the comparison
    measures the configuration rather than the signal."""
    built = []

    class Engine:
        def __init__(self, strategy):
            self.strategy = strategy

        def run(self):
            built.append(self.strategy.id)
            return arm(0.002 if self.strategy.id == "k" else 0.001)

    class Fake:
        def __init__(self, id, params):
            self.id = id
            self._params = params

        def parameters(self):
            return self._params

    shared = {"target_volatility": 0.15, "stop_atr_multiple": 2.0}
    comp = run_strategy_comparison(
        build_engine=Engine,
        kronos_strategy=Fake("k", {**shared, "signal_source": "kronos_forecast"}),
        baseline_strategy=Fake("b", {**shared, "signal_source": "close_momentum"}),
        out_of_sample=True, seed=3, iterations=400)
    assert built == ["k", "b"]
    assert comp.notes == ()
    assert kronos_is_valuable(comp) is True


def test_arms_that_differ_beyond_the_signal_source_are_flagged():
    class Engine:
        def __init__(self, strategy):
            self.strategy = strategy

        def run(self):
            return arm(0.002 if self.strategy.id == "k" else 0.001)

    class Fake:
        def __init__(self, id, params):
            self.id = id
            self._params = params

        def parameters(self):
            return self._params

    comp = run_strategy_comparison(
        build_engine=Engine,
        kronos_strategy=Fake("k", {"target_volatility": 0.30}),
        baseline_strategy=Fake("b", {"target_volatility": 0.15}),
        out_of_sample=True)
    assert any("more than their signal source" in note for note in comp.notes)


def test_comparison_serialises_for_the_audit_trail():
    import json
    json.dumps(comparison().to_dict())
