"""The nine baselines, and the harness that scores them.

The point of a baseline suite is to be able to lose to it. So the tests here are
arranged around the ways a benchmark is usually rigged rather than around the
baselines themselves:

* **the split.** Every model gets the identical train and test sequences, and
  `test_every_model_is_scored_on_the_same_windows` checks it rather than
  assuming it;
* **the costs.** A forecast smaller than the round trip is not a trade, and
  `test_costs_suppress_a_trade_smaller_than_the_round_trip` constructs that case;
* **the metric.** One function scores every model, and no model chooses;
* **abstention.** A model that answers only when confident would win by
  abstaining, so `compare_scored` intersects window sets and the abstention rate
  sits beside every score.

The result that matters most is
`test_a_linear_process_is_won_by_the_linear_models`. On a synthetic AR(1)
process the ridge and autoregressive baselines *should* beat a small neural
network, and asserting that they do is what makes the suite capable of
reporting a loss. A benchmark that could only confirm the native model would be
worth nothing.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.errors import ConfigurationError, InsufficientDataError
from olympus.trading.contracts import Candle
from olympus.trading.native import baselines as B
from olympus.trading.native import benchmark as BM
from olympus.trading.native import dataset as D
from olympus.trading.native import torchutil as TU
from olympus.trading.native.data import make_windows
from olympus.trading.native.quantile import Declined, quantile_label

START = datetime(2027, 6, 1, tzinfo=timezone.utc)
KEY = "SIM:B"
HORIZON = 3
LOOKBACK = 33

needs_torch = pytest.mark.skipif(not TU.torch_available(),
                                 reason="torch is an optional `native` extra")


def ar1(n: int = 1200, seed: int = 5, phi: float = 0.97,
        noise: float = 0.001) -> list[float]:
    """A linear process. The linear baselines should win on it, and do."""
    rng = random.Random(seed)
    prices, step = [100.0], 0.0
    for _ in range(n - 1):
        step = phi * step + rng.gauss(0.0, noise)
        prices.append(prices[-1] * math.exp(step))
    return prices


def bars(prices) -> list[Candle]:
    return [Candle(instrument_key=KEY, timeframe="1h",
                   ts_open=START + timedelta(hours=i),
                   ts_close=START + timedelta(hours=i + 1),
                   open=p, high=p * 1.004, low=p * 0.996, close=p,
                   volume=1000.0 + i)
            for i, p in enumerate(prices)]


@pytest.fixture(scope="module")
def split():
    windows = make_windows(bars(ar1()), lookback=LOOKBACK, horizon=HORIZON)
    return D.three_way_split(windows, train_fraction=0.6,
                             validation_fraction=0.2)


@pytest.fixture(scope="module")
def report(split):
    """One benchmark run, reused. Fitting nine models takes seconds."""
    return BM.run_benchmark(train=split.train, test=split.test, horizon=HORIZON)


# ===========================================================================
# the registry
# ===========================================================================

def test_all_nine_baselines_the_plan_names_are_present():
    assert set(B.BASELINES) == {
        "persistence", "moving_average", "linear_regression", "autoregression",
        "gradient_boosted_trees", "recurrent", "temporal_transformer",
        "volatility", "regime"}


def test_the_stdlib_baselines_run_without_torch():
    """Seven of nine, so a benchmark is still meaningful with torch absent."""
    stdlib = {k for k, entry in B.BASELINES.items() if not entry["torch_required"]}
    assert len(stdlib) == 7
    assert {"recurrent", "temporal_transformer"} == set(B.BASELINES) - stdlib
    assert set(B.available_baselines()) >= stdlib


def test_an_unknown_baseline_is_refused():
    with pytest.raises(ConfigurationError, match="unknown baseline"):
        B.build_baseline("prophet", horizon=1)


@pytest.mark.parametrize("bad", [{"quantiles": ()}, {"quantiles": (0.0, 0.5)},
                                 {"quantiles": (0.5, 1.0)}, {"horizon": 0}])
def test_an_invalid_configuration_is_refused(bad):
    settings = {"horizon": 1, **bad}
    with pytest.raises(ConfigurationError):
        B.PersistenceBaseline(**settings)


def test_baseline_specific_configuration_is_validated():
    with pytest.raises(ConfigurationError, match="order must be"):
        B.AutoregressionBaseline(horizon=1, order=0)
    with pytest.raises(ConfigurationError, match="learning_rate"):
        B.GradientBoostedTreesBaseline(horizon=1, learning_rate=0.0)
    with pytest.raises(ConfigurationError, match="window must be"):
        B.VolatilityBaseline(horizon=1, window=1)


@needs_torch
def test_a_transformer_whose_width_does_not_divide_its_heads_is_refused():
    with pytest.raises(ConfigurationError, match="divide by heads"):
        B.TemporalTransformerBaseline(horizon=1, d_model=10, heads=3)


# ===========================================================================
# fitting and predicting
# ===========================================================================

@pytest.mark.parametrize("key", sorted(k for k, e in B.BASELINES.items()
                                       if not e["torch_required"]))
def test_every_stdlib_baseline_fits_and_predicts(key, split):
    model = B.build_baseline(key, horizon=HORIZON)
    report = model.fit(split.train)
    assert report.rows > 0 and report.parameters > 0
    assert len(report.train_mae) == HORIZON
    assert model.fitted

    prediction = model.predict_from_bars(split.test[0].inputs)
    assert not isinstance(prediction, Declined), prediction
    assert prediction.horizon == HORIZON
    assert set(prediction.log_return_quantiles) == {
        quantile_label(q) for q in B.DEFAULT_QUANTILES}


@pytest.mark.parametrize("key", sorted(k for k, e in B.BASELINES.items()
                                       if not e["torch_required"]))
def test_every_baselines_quantiles_are_ordered(key, split):
    """A crossed interval is not a distribution."""
    model = B.build_baseline(key, horizon=HORIZON)
    model.fit(split.train)
    prediction = model.predict_from_bars(split.test[5].inputs)
    labels = [quantile_label(q) for q in sorted(B.DEFAULT_QUANTILES)]
    for step in range(HORIZON):
        values = [prediction.log_return_quantiles[label][step] for label in labels]
        assert values == sorted(values), f"{key} crossed its quantiles at {step}"


def test_an_unfitted_baseline_declines_rather_than_guessing(split):
    model = B.build_baseline("persistence", horizon=HORIZON)
    prediction = model.predict_from_bars(split.test[0].inputs)
    assert isinstance(prediction, Declined)


def test_a_short_window_declines(split):
    model = B.build_baseline("moving_average", horizon=HORIZON, window=20)
    model.fit(split.train)
    assert model.predict_from_bars(split.test[0].inputs[:3]) is B.DECLINE_SHORT_WINDOW


def test_fitting_on_a_mismatched_horizon_is_refused(split):
    model = B.build_baseline("persistence", horizon=HORIZON + 1)
    with pytest.raises(ConfigurationError, match="do not match the configured"):
        model.fit(split.train)


def test_fitting_on_nothing_is_refused():
    with pytest.raises(InsufficientDataError, match="no training windows"):
        B.build_baseline("persistence", horizon=1).fit([])


# ===========================================================================
# the properties each baseline claims about itself
# ===========================================================================

def test_persistence_predicts_no_move_and_its_skill_is_all_in_the_residuals(split):
    model = B.build_baseline("persistence", horizon=HORIZON)
    model.fit(split.train)
    assert model._project(split.test[0].inputs) == (0.0, 0.0, 0.0)
    prediction = model.predict_from_bars(split.test[0].inputs)
    # The interval is not degenerate, so the residual quantiles did their work.
    labels = [quantile_label(q) for q in sorted(B.DEFAULT_QUANTILES)]
    spread = (prediction.log_return_quantiles[labels[-1]][-1]
              - prediction.log_return_quantiles[labels[0]][-1])
    assert spread > 0


def test_a_constant_point_forecast_scores_exactly_as_persistence(split):
    """The property `PointBaseline` documents: a constant offset is absorbed.

    It is why a benchmark row identical to persistence usually means a model
    degenerated rather than that the harness is broken, and it is worth having
    as a test so that nobody rediscovers it as a bug.
    """
    class ConstantDrift(B.PersistenceBaseline):
        KIND = "constant_drift"

        def _project(self, window):
            return tuple(0.01 * (step + 1) for step in range(self.horizon))

    persistence = B.build_baseline("persistence", horizon=HORIZON)
    persistence.fit(split.train)
    constant = ConstantDrift(horizon=HORIZON)
    constant.fit(split.train)

    a, _ = BM.score_model(persistence, split.test[:60])
    b, _ = BM.score_model(constant, split.test[:60])
    assert b.pinball == pytest.approx(a.pinball, rel=1e-9)


def test_the_volatility_baseline_widens_its_interval_when_volatility_rises(split):
    """The first opponent whose uncertainty is conditional."""
    model = B.build_baseline("volatility", horizon=HORIZON, window=20)
    model.fit(split.train)
    scales = [model._spread_scale(w.inputs) for w in split.test[:80]]
    assert max(scales) > min(scales) * 1.2, (
        "the spread scale barely moved, so it is not conditioning on anything")


def test_the_regime_baseline_says_when_it_has_degenerated(split):
    """A single regime across training makes it persistence, and it says so."""
    model = B.build_baseline("regime", horizon=HORIZON)
    notes = model.fit(split.train).notes
    assert notes, "the regime baseline reported nothing about its own fit"
    assert "degenerate" in notes or "regimes fitted" in notes


def test_autoregression_composes_its_multi_step_forecast(split):
    """The scheme the native model rejects, present so the pair is comparable."""
    model = B.build_baseline("autoregression", horizon=HORIZON, order=3)
    model.fit(split.train)
    projection = model._project(split.test[0].inputs)
    assert len(projection) == HORIZON
    # Cumulative, so consecutive differences are the composed one-step steps.
    assert projection[0] != projection[1]


def test_the_feature_baselines_only_use_features_present_in_every_window(split):
    """A column that is sometimes missing is a dependency serving cannot meet."""
    model = B.build_baseline("linear_regression", horizon=HORIZON)
    model.fit(split.train)
    assert model._feature_names, "no features were selected"
    assert model.required_bars <= LOOKBACK
    for window in split.train[:20]:
        assert model._vector(window.inputs) is not None


def test_a_feature_baseline_declines_when_its_features_are_uncomputable(split):
    model = B.build_baseline("linear_regression", horizon=HORIZON)
    model.fit(split.train)
    assert model.predict_from_bars(split.test[0].inputs[:2]) in (
        B.DECLINE_NO_FEATURES, B.DECLINE_SHORT_WINDOW)


def test_a_boosted_tree_is_a_piecewise_constant_function(split):
    """Cheap structural check that the trees are real trees."""
    model = B.build_baseline("gradient_boosted_trees", horizon=HORIZON,
                             estimators=8, depth=2)
    model.fit(split.train)
    assert len(model._trees) == HORIZON
    assert all(len(forest) == 8 for forest in model._trees)
    tree = model._trees[0][0]
    assert tree.nodes() >= 1
    assert tree.feature is None or tree.left is not None


@needs_torch
@pytest.mark.parametrize("key", ["recurrent", "temporal_transformer"])
def test_the_torch_baselines_are_deterministic_given_a_seed(key, split):
    subset = split.train[:200]
    first = B.build_baseline(key, horizon=HORIZON, epochs=3, seed=0)
    second = B.build_baseline(key, horizon=HORIZON, epochs=3, seed=0)
    third = B.build_baseline(key, horizon=HORIZON, epochs=3, seed=1)
    for model in (first, second, third):
        model.fit(subset)

    window = split.test[0].inputs
    assert first._project(window) == second._project(window)
    assert first._project(window) != third._project(window), (
        "changing the seed changed nothing, so the seed is not being used")


@needs_torch
def test_the_transformer_baseline_applies_a_causal_mask(split):
    """It could see the future and does not. The native trunk cannot.

    Changing only the *last* timestep must not change the representation of
    earlier ones. Only the final position is read for the head, so the check is
    made on the attention output directly.
    """
    torch = TU.require_torch()
    model = B.build_baseline("temporal_transformer", horizon=HORIZON, epochs=1)
    model.fit(split.train[:100])
    module = model._module

    base = torch.randn(1, 4, model.lookback,
                       generator=torch.Generator().manual_seed(0))
    altered = base.clone()
    altered[:, :, -1] += 10.0

    def hidden(x):
        h = module.project(x.transpose(1, 2))
        h = h + module.position[:h.shape[1]]
        mask = torch.triu(torch.ones(h.shape[1], h.shape[1], dtype=torch.bool),
                          diagonal=1)
        attended, _ = module.attention(h, h, h, attn_mask=mask,
                                       need_weights=False)
        return module.norm(h + attended)

    with torch.no_grad():
        before, after = hidden(base), hidden(altered)
    assert torch.allclose(before[:, :-1, :], after[:, :-1, :], atol=1e-5), (
        "changing the last timestep moved earlier positions, so the causal "
        "mask is not being applied")
    assert not torch.allclose(before[:, -1, :], after[:, -1, :], atol=1e-5)


# ===========================================================================
# the harness: same data, same costs, same metrics
# ===========================================================================

def test_every_model_is_scored_on_the_same_windows(report):
    """Checked, not assumed. The most easily rigged clause in a comparison."""
    offered = {score.offered for score in report.scores}
    assert len(offered) == 1, f"models were offered different test sets: {offered}"
    assert report.test_windows == offered.pop()


def test_a_model_that_fails_to_fit_is_recorded_rather_than_aborting_the_run(split):
    """The remaining comparisons are still valid, so they are still reported."""
    class Broken(B.PersistenceBaseline):
        KIND = "broken"

        def fit(self, windows):
            raise RuntimeError("deliberate")

    result = BM.run_benchmark(train=split.train[:200], test=split.test[:60],
                              horizon=HORIZON,
                              baselines=("persistence", "moving_average"),
                              models={"broken": Broken(horizon=HORIZON)})
    assert "broken" in result.failures
    assert "deliberate" in result.failures["broken"]
    assert {s.name for s in result.scores} == {"persistence", "moving_average"}


def test_a_supplied_model_may_not_shadow_a_baseline_name(split):
    with pytest.raises(ConfigurationError, match="shadows a baseline"):
        BM.run_benchmark(train=split.train[:100], test=split.test[:40],
                         horizon=HORIZON, baselines=("persistence",),
                         models={"persistence": B.PersistenceBaseline(horizon=HORIZON)})


def test_costs_suppress_a_trade_smaller_than_the_round_trip(split):
    """Without this a two-basis-point forecast on a ten-point spread pays."""
    model = B.build_baseline("persistence", horizon=HORIZON)
    model.fit(split.train)

    expensive = BM.CostModel(spread_bps=500.0, fee_bps=0.0)
    cheap = BM.FREE
    dear, _ = BM.score_model(model, split.test, costs=expensive)
    free, _ = BM.score_model(model, split.test, costs=cheap)

    assert dear.trades == 0, "a huge cost should suppress every trade"
    assert dear.net_return == 0.0
    assert free.trades > 0
    assert free.cost_drag == 0.0


def test_the_cost_model_reports_gross_and_net_separately(split):
    """A strategy that works gross and not net has a signal and no business."""
    model = B.build_baseline("autoregression", horizon=HORIZON)
    model.fit(split.train)
    score, _ = BM.score_model(model, split.test, costs=BM.CostModel())
    assert score.gross_return is not None and score.net_return is not None
    assert score.cost_drag >= 0.0


def test_a_negative_cost_is_refused():
    with pytest.raises(ConfigurationError, match="must be >= 0"):
        BM.CostModel(spread_bps=-1.0)


def test_abstentions_are_reported_and_not_scored_as_errors(split):
    class Choosy(B.PersistenceBaseline):
        KIND = "choosy"

        def predict_from_bars(self, window):
            if len(window) % 2 == 0:
                return B.DECLINE_SHORT_WINDOW
            return super().predict_from_bars(window)

    model = Choosy(horizon=HORIZON)
    model.fit(split.train)
    trimmed = [w for w in split.test[:40]]
    score, _ = BM.score_model(model, trimmed)
    if score.scored < score.offered:
        assert score.abstention_rate > 0
        assert sum(score.declines.values()) == score.offered - score.scored


def test_comparing_two_models_uses_only_the_windows_both_answered(split):
    """Zipping mismatched sets would pair a window with a different window."""
    persistence = B.build_baseline("persistence", horizon=HORIZON)
    persistence.fit(split.train)
    _, full = BM.score_model(persistence, split.test)

    partial = BM.ScoredWindows(name="partial", keys=full.keys[:20],
                               pinball=full.pinball[:20],
                               absolute_error=full.absolute_error[:20],
                               net_return=full.net_return[:20])
    comparison = BM.compare_scored(partial, full)
    assert comparison.common == 20
    assert comparison.pinball_gain == pytest.approx(0.0, abs=1e-12)


def test_comparing_models_with_no_common_windows_is_refused(split):
    persistence = B.build_baseline("persistence", horizon=HORIZON)
    persistence.fit(split.train)
    _, full = BM.score_model(persistence, split.test)
    disjoint = BM.ScoredWindows(name="other", keys=("x", "y"), pinball=(1.0, 1.0),
                                absolute_error=(1.0, 1.0), net_return=(0.0, 0.0))
    with pytest.raises(InsufficientDataError, match="fewer than two"):
        BM.compare_scored(disjoint, full)


def test_scored_series_of_different_lengths_are_refused():
    with pytest.raises(ConfigurationError, match="different lengths"):
        BM.ScoredWindows(name="x", keys=("a", "b"), pinball=(1.0,),
                         absolute_error=(1.0, 1.0), net_return=(0.0, 0.0))


def test_the_verdict_rests_on_the_proper_scoring_rule_alone(split):
    """Three metrics voting would let a model claim two out of three."""
    comparison = BM.PairedComparison(
        model="m", reference="r", common=100,
        pinball_gain=-0.001, pinball_significant=True, pinball_p_value=0.0,
        mae_gain=0.5, mae_significant=True,
        net_return_gain=0.5, net_return_significant=True)
    assert not comparison.better


def test_coverage_error_is_signed(split):
    model = B.build_baseline("persistence", horizon=HORIZON)
    model.fit(split.train)
    score, _ = BM.score_model(model, split.test)
    assert score.nominal_coverage == pytest.approx(0.8)
    assert score.interval_coverage is not None
    assert score.coverage_error == pytest.approx(
        (score.interval_coverage - 0.8) * 100.0)


# ===========================================================================
# the result the suite exists to be able to report
# ===========================================================================

def test_a_linear_process_is_won_by_the_linear_models(report):
    """The suite must be capable of reporting that the network lost.

    The generating process is AR(1) — linear by construction — so ridge
    regression and an autoregressive fit should beat everything, including a
    small neural network. Asserting it is what distinguishes this benchmark from
    one arranged to confirm the native model.
    """
    ranked = sorted((s for s in report.scores if s.pinball is not None),
                    key=lambda s: s.pinball)
    best = [s.name for s in ranked[:3]]
    assert "autoregression" in best or "linear_regression" in best, (
        f"a linear process was not won by a linear model: {best}")

    persistence = report.score("persistence")
    winner = ranked[0]
    assert winner.pinball < persistence.pinball


def test_something_beats_persistence_and_the_verdict_says_which(report):
    assert report.winners, "nothing beat persistence on a predictable process"
    for name in report.winners:
        comparison = report.comparison(name)
        assert comparison.pinball_gain > 0 and comparison.pinball_significant


def test_the_table_names_every_model_and_marks_the_reference(report):
    table = report.table()
    for score in report.scores:
        assert score.name in table
    assert "reference" in table
    assert "pinball" in table and "net" in table


@needs_torch
def test_the_native_model_enters_the_same_harness_as_its_opponents(split):
    """Adapter, not a special case. It gets no metric its opponents lack."""
    from olympus.trading.native import neural as N

    config = N.NeuralConfig(lookback=LOOKBACK, horizon=HORIZON, patch_len=4,
                            d_model=16, n_layers=2, epochs=25, batch_size=64,
                            learning_rate=1e-2)
    entrant = BM.NativeEntrant(N.NeuralQuantileModel(config), seed=0)
    result = BM.run_benchmark(train=split.train, test=split.test,
                              horizon=HORIZON,
                              baselines=("persistence", "autoregression"),
                              models={"olympus_native": entrant})
    assert result.failures == {}
    native = result.score("olympus_native")
    assert native is not None and native.parameters > 0
    assert native.offered == result.score("persistence").offered
    # Whether it wins is a result, not a requirement. That it is *measured* on
    # the same windows, with the same costs, is the requirement.
    assert result.comparison("olympus_native") is not None


def test_the_report_round_trips_to_a_dict(report):
    payload = report.to_dict()
    assert payload["reference"] == "persistence"
    assert len(payload["scores"]) == len(report.scores)
    assert payload["costs"]["round_trip_bps"] > 0
    assert set(payload["winners"]) == set(report.winners)
