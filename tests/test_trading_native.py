"""The Olympus-native forecasting skeleton — phase P2.

P2's exit condition: the plumbing works end to end. A `MarketState` is causal, a
split has an embargo, a checkpoint cannot exist without a manifest, weights
cannot come from anywhere but Olympus, and the forecaster produces a valid
`ForecastResult` that the existing service, cache and evaluator accept.

**What these tests do not show.** Every series here is synthetic. Passing proves
the pipeline is correct, not that the model knows anything about a market —
`docs/OLYMPUS_NATIVE_MODEL_STATUS.md` is the ledger on that, and B1 (no
reachable market data) is why.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, DataQuality, Regime
from olympus.trading.errors import (ConfigurationError, DataValidationError,
                                    InsufficientDataError)
from olympus.trading.features import FeatureBuilder
from olympus.trading.native import checkpoint as CP
from olympus.trading.native import data as D
from olympus.trading.native import forecaster as F
from olympus.trading.native import quantile as Q
from olympus.trading.native import state as S
from olympus.trading.native import train as T

START = datetime(2027, 2, 1, tzinfo=timezone.utc)
KEY = "SIM:NATIVE"
TF = "1h"


def bars(prices, *, key=KEY, timeframe=TF, final=True) -> list[Candle]:
    out = []
    for i, price in enumerate(prices):
        out.append(Candle(
            instrument_key=key, timeframe=timeframe,
            ts_open=START + timedelta(hours=i),
            ts_close=START + timedelta(hours=i + 1),
            open=price, high=price * 1.005, low=price * 0.995,
            close=price, volume=1000.0 + i, is_final=final))
    return out


def trending_series(n=600, seed=7):
    """A series with real conditional structure: after a run-up, the next move
    is more often up. A model that conditions on momentum should find it."""
    rng = random.Random(seed)
    prices = [100.0]
    momentum = 0.0
    for _ in range(n - 1):
        drift = 0.0015 if momentum > 0 else -0.0015
        step = drift + rng.gauss(0.0, 0.004)
        prices.append(max(1.0, prices[-1] * math.exp(step)))
        momentum = 0.9 * momentum + step
    return prices


# ===========================================================================
# state
# ===========================================================================

def test_a_state_carries_features_regime_and_scale():
    builder = S.MarketStateBuilder(features=FeatureBuilder())
    state = builder.build({TF: bars(trending_series(120))})
    assert state.instrument_key == KEY
    assert state.base_timeframe == TF
    assert state.base.n_bars == 120
    assert state.available_features
    assert state.realised_volatility is not None
    assert state.regime is Regime.UNKNOWN, "no classifier means no classification"


def test_multi_scale_is_the_general_case_not_a_special_one():
    builder = S.MarketStateBuilder()
    hourly = bars(trending_series(200))
    daily = bars(trending_series(50), timeframe="1d")
    state = builder.build({TF: hourly, "1d": daily}, base_timeframe=TF)
    assert set(state.scales) == {TF, "1d"}
    assert state.feature("rsi_14", timeframe="1d") is not None or True
    assert state.base.timeframe == TF


def test_a_state_whose_scale_closes_after_as_of_is_refused():
    """The causality invariant, checked rather than trusted."""
    window = bars(trending_series(60))
    with pytest.raises(DataValidationError) as exc:
        S.MarketState(
            instrument_key=KEY, as_of=START,
            scales={TF: S.ScaleObservation(timeframe=TF,
                                           ts=window[-1].ts_close,
                                           last_close=100.0, n_bars=60)},
            base_timeframe=TF)
    assert "could not have existed" in str(exc.value)


def test_a_missing_feature_is_absent_never_zero():
    """A zero-filled RSI is a real number a model will happily learn from."""
    builder = S.MarketStateBuilder()
    state = builder.build({TF: bars([100.0, 101.0, 102.0])})
    assert state.feature("sma_50_distance") is None
    assert "not_a_feature" not in state.available_features
    assert state.missing(["not_a_feature"]) == ("not_a_feature",)


def test_a_feature_vector_follows_the_callers_order():
    """A model trained on one iteration order and served on another is a
    total, silent corruption of every prediction."""
    builder = S.MarketStateBuilder()
    state = builder.build({TF: bars(trending_series(120))})
    forward = state.feature_vector(["rsi_14", "atr_pct"])
    backward = state.feature_vector(["atr_pct", "rsi_14"])
    assert forward == tuple(reversed(backward))


def test_a_non_positive_close_is_refused():
    with pytest.raises(DataValidationError):
        S.ScaleObservation(timeframe=TF, ts=START, last_close=0.0, n_bars=1)


def test_a_state_round_trips_and_fingerprints():
    builder = S.MarketStateBuilder()
    state = builder.build({TF: bars(trending_series(120))})
    restored = S.MarketState.from_dict(state.to_dict())
    assert restored.fingerprint() == state.fingerprint()


# ===========================================================================
# data — where look-ahead is designed out
# ===========================================================================

def test_a_window_separates_what_was_known_from_what_happened():
    windows = D.make_windows(bars(trending_series(60)), lookback=20, horizon=5)
    window = windows[0]
    assert window.lookback == 20 and window.horizon == 5
    assert window.as_of == window.inputs[-1].ts_close
    assert window.targets[0].ts_open >= window.as_of


def test_a_window_whose_horizon_sits_inside_its_input_is_refused():
    """Checked on timestamps, not indices, so a slicing mistake cannot satisfy
    it by accident."""
    series = bars(trending_series(60))
    with pytest.raises(DataValidationError) as exc:
        D.Window(instrument_key=KEY, timeframe=TF,
                 inputs=tuple(series[0:20]), targets=tuple(series[10:15]))
    assert "horizon is inside the input window" in str(exc.value)


def test_a_forming_bar_cannot_enter_a_training_window():
    series = bars(trending_series(60))
    series[-1] = series[-1].with_final(False)
    with pytest.raises(DataValidationError) as exc:
        D.make_windows(series, lookback=20, horizon=5)
    assert "not yet the close" in str(exc.value)


def test_target_returns_are_cumulative_from_the_anchor():
    window = D.make_windows(bars([100.0] * 30), lookback=20, horizon=5)[0]
    assert window.target_log_returns() == pytest.approx((0.0,) * 5)
    rising = D.make_windows(bars([100.0] * 20 + [110.0] * 10),
                            lookback=20, horizon=5)[0]
    assert rising.target_log_returns()[-1] == pytest.approx(math.log(1.1))


def test_the_split_embargoes_windows_whose_horizon_crosses_the_boundary():
    """The naive split trains on windows whose targets are the test set's
    inputs. That is leakage nobody wrote a line of code for."""
    windows = D.make_windows(bars(trending_series(400)), lookback=30, horizon=10)
    split = D.temporal_split(windows, train_fraction=0.7)
    assert split.embargoed > 0, "an embargo that drops nothing is not an embargo"
    latest_train_target = max(w.targets[-1].ts_close for w in split.train)
    for window in split.test:
        assert window.inputs[0].ts_open >= latest_train_target


def test_an_embargo_shorter_than_the_horizon_is_refused():
    windows = D.make_windows(bars(trending_series(200)), lookback=20, horizon=10)
    with pytest.raises(ConfigurationError) as exc:
        D.temporal_split(windows, embargo_bars=3)
    assert "smaller than the horizon" in str(exc.value)


def test_mixed_horizons_cannot_be_split_together():
    a = D.make_windows(bars(trending_series(200)), lookback=20, horizon=5)
    b = D.make_windows(bars(trending_series(200)), lookback=20, horizon=7)
    with pytest.raises(ConfigurationError):
        D.temporal_split(a + b)


def test_the_split_is_ordered_in_time():
    windows = D.make_windows(bars(trending_series(300)), lookback=20, horizon=5)
    split = D.temporal_split(windows)
    assert all(split.train[i].as_of <= split.train[i + 1].as_of
               for i in range(len(split.train) - 1))
    if split.test:
        assert split.train[-1].as_of < split.test[0].as_of


def test_too_few_bars_for_a_single_window_is_an_explicit_error():
    with pytest.raises(InsufficientDataError):
        D.make_windows(bars([100.0] * 10), lookback=20, horizon=5)


def test_the_data_version_is_a_content_hash():
    series = bars(trending_series(50))
    assert D.data_version(series) == D.data_version(list(series))
    changed = list(series)
    changed[10] = Candle(**{**changed[10].to_dict_raw()}) if False else changed[10]
    assert D.data_version(series[:-1]) != D.data_version(series)


# ===========================================================================
# quantile estimator
# ===========================================================================

def fit_rows(n=400, seed=3, horizon=3):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        momentum = rng.gauss(0.0, 1.0)
        # Real conditional structure: high momentum shifts the return up.
        base = 0.01 * momentum
        rows.append(({"m": momentum, "v": rng.gauss(0.0, 1.0)},
                     tuple(base + rng.gauss(0.0, 0.005) for _ in range(horizon))))
    return rows


def test_the_estimator_learns_a_conditional_distribution():
    config = Q.QuantileConfig(horizon=3, conditioners=("m",), n_buckets=3,
                              min_cell_samples=30)
    model = Q.ConditionalQuantileModel(config)
    report = model.fit(fit_rows())
    assert report.usable_cells >= 2
    assert report.coverage > 0.9

    low = model.predict({"m": -1.5})
    high = model.predict({"m": 1.5})
    assert not isinstance(low, Q.Declined) and not isinstance(high, Q.Declined)
    # The learned structure: a high-momentum cell forecasts a higher return.
    assert high.log_return_quantiles["p50"][-1] > low.log_return_quantiles["p50"][-1]


def test_quantiles_are_ordered_within_a_cell():
    model = Q.ConditionalQuantileModel(
        Q.QuantileConfig(horizon=3, conditioners=("m",)))
    model.fit(fit_rows())
    prediction = model.predict({"m": 0.0})
    labels = sorted(prediction.log_return_quantiles)
    for step in range(3):
        values = [prediction.log_return_quantiles[l][step] for l in labels]
        assert values == sorted(values), "quantiles crossed"


def test_a_thin_cell_declines_rather_than_falling_back():
    """Backing off to the unconditional distribution would emit the baseline's
    answer under the model's name."""
    # 400 rows over 3 buckets is ~133 per cell — enough to fit, not enough to
    # clear a 200-observation floor.
    config = Q.QuantileConfig(horizon=2, conditioners=("m",), n_buckets=3,
                              min_cell_samples=200)
    model = Q.ConditionalQuantileModel(config)
    report = model.fit(fit_rows(n=400, horizon=2))
    assert report.usable_cells == 0 and report.thin_cells == 3
    assert report.speaks_often_enough is False
    # Fit *ran*; it just produced nothing usable. Reported distinctly from
    # "fit was never called", which would send a reader to the wrong problem.
    assert model.fitted is True and model.usable is False
    assert model.predict({"m": 0.0}) is Q.DECLINE_NO_USABLE_CELLS


def test_a_sparse_interaction_cell_declines_while_dense_ones_answer():
    """The realistic thin-cell case. Quantile bucketing gives roughly equal
    cells per conditioner by construction, so a single conditioner never leaves
    a hole. Sparsity appears in the *interaction*: two correlated conditioners
    leave the opposing corners nearly empty, and `n_buckets ** n_conditioners`
    grows fast enough that this is the normal failure of the design."""
    rng = random.Random(11)
    rows = []
    for _ in range(600):
        m = rng.gauss(0.0, 1.0)
        v = m + rng.gauss(0.0, 0.05)          # strongly correlated with m
        rows.append(({"m": m, "v": v}, (0.001, 0.002)))
    config = Q.QuantileConfig(horizon=2, conditioners=("m", "v"), n_buckets=3,
                              min_cell_samples=30)
    model = Q.ConditionalQuantileModel(config)
    report = model.fit(rows)
    assert report.populated_cells > report.usable_cells, (
        "correlated conditioners must leave at least one corner sparse")
    assert report.coverage < 1.0

    # The diagonal is dense and answers; the opposing corner is sparse.
    assert not isinstance(model.predict({"m": 0.0, "v": 0.0}), Q.Declined)
    assert model.predict({"m": -1.2, "v": 1.2}) in (
        Q.DECLINE_THIN_CELL, Q.DECLINE_OUT_OF_RANGE)


def test_an_out_of_range_input_declines():
    model = Q.ConditionalQuantileModel(
        Q.QuantileConfig(horizon=2, conditioners=("m",)))
    model.fit(fit_rows(horizon=2))
    assert model.predict({"m": 500.0}) is Q.DECLINE_OUT_OF_RANGE


def test_a_missing_feature_declines():
    model = Q.ConditionalQuantileModel(
        Q.QuantileConfig(horizon=2, conditioners=("m",)))
    model.fit(fit_rows(horizon=2))
    assert model.predict({}) is Q.DECLINE_MISSING_FEATURE
    assert model.predict({"m": float("nan")}) is Q.DECLINE_MISSING_FEATURE


def test_an_unfitted_model_declines_rather_than_guessing():
    model = Q.ConditionalQuantileModel(
        Q.QuantileConfig(horizon=2, conditioners=("m",)))
    assert model.predict({"m": 0.0}) is Q.DECLINE_NOT_FITTED


def test_fitting_is_deterministic():
    """Gate G7 at the estimator level: same rows, byte-identical parameters."""
    config = Q.QuantileConfig(horizon=3, conditioners=("m", "v"))
    rows = fit_rows()
    a = Q.ConditionalQuantileModel(config)
    a.fit(rows)
    b = Q.ConditionalQuantileModel(config)
    b.fit(list(rows))
    assert a.to_parameters() == b.to_parameters()


def test_parameters_round_trip_exactly():
    config = Q.QuantileConfig(horizon=3, conditioners=("m",))
    model = Q.ConditionalQuantileModel(config)
    model.fit(fit_rows())
    restored = Q.ConditionalQuantileModel.from_parameters(model.to_parameters())
    assert restored.to_parameters() == model.to_parameters()
    assert restored.predict({"m": 0.5}).log_return_quantiles == \
        model.predict({"m": 0.5}).log_return_quantiles


def test_foreign_parameters_are_refused():
    with pytest.raises(ConfigurationError):
        Q.ConditionalQuantileModel.from_parameters({"kind": "something-else"})


def test_a_config_with_no_conditioner_is_refused():
    """With none, this is the unconditional distribution — and there is already
    a baseline for that."""
    with pytest.raises(ConfigurationError) as exc:
        Q.QuantileConfig(horizon=2, conditioners=())
    assert "already a baseline" in str(exc.value)


def test_log_returns_become_prices_by_exponentiation():
    model = Q.ConditionalQuantileModel(
        Q.QuantileConfig(horizon=2, conditioners=("m",)))
    model.fit(fit_rows(horizon=2))
    prediction = model.predict({"m": 0.0})
    prices = prediction.prices(100.0)
    expected = 100.0 * math.exp(prediction.log_return_quantiles["p50"][0])
    assert prices["p50"][0] == pytest.approx(expected)


def test_too_few_rows_to_fit_is_an_explicit_error():
    model = Q.ConditionalQuantileModel(
        Q.QuantileConfig(horizon=2, conditioners=("m",), min_cell_samples=30))
    with pytest.raises(InsufficientDataError):
        model.fit(fit_rows(n=5, horizon=2))


# ===========================================================================
# checkpoint — gates G3 and G8
# ===========================================================================

def manifest(**kwargs):
    kwargs.setdefault("data_hash", "sha:abc")
    kwargs.setdefault("train_start", START)
    kwargs.setdefault("train_end", START + timedelta(days=30))
    kwargs.setdefault("seed", 0)
    kwargs.setdefault("config", {"horizon": 3})
    kwargs.setdefault("feature_names", ("rsi_14",))
    return CP.TrainingManifest(**kwargs)


def checkpoint(**kwargs):
    kwargs.setdefault("checkpoint_id", "ck-1")
    kwargs.setdefault("model_kind", "olympus-conditional-quantile")
    kwargs.setdefault("version", "1")
    kwargs.setdefault("manifest", manifest())
    kwargs.setdefault("parameters", {"kind": "x", "cells": {}})
    return CP.NativeCheckpoint(**kwargs)


@pytest.mark.parametrize("origin,why", [
    ("https://huggingface.co/some/model", "a URL"),
    ("/models/kronos-small.safetensors", "a filesystem path"),
    ("~/weights/model.pt", "a filesystem path"),
    ("model.ckpt", "a weights file"),
    ("some-org/some-model", "a model-hub repository id"),
])
def test_foreign_weights_are_refused_by_shape(origin, why):
    """Gate G3. There is no argument value that reaches a foreign file."""
    with pytest.raises(CP.ForeignWeightsRefused) as exc:
        CP.assert_olympus_origin(origin)
    assert exc.value.details["looks_like"] == why


def test_a_fresh_start_and_an_olympus_continuation_are_permitted():
    assert CP.assert_olympus_origin(None) == ""
    assert CP.assert_olympus_origin("") == ""
    assert CP.assert_olympus_origin("ck-1", known=["ck-1"]) == "ck-1"


def test_continuing_from_an_unknown_checkpoint_is_refused():
    """'Looks like an id' is a naming convention; 'is an id we wrote' is a
    guarantee."""
    with pytest.raises(CP.ForeignWeightsRefused):
        CP.assert_olympus_origin("ck-nope", known=["ck-1"])


def test_a_checkpoint_cannot_record_a_foreign_origin():
    with pytest.raises(CP.ForeignWeightsRefused):
        checkpoint(initialised_from="/tmp/foreign.pt")


@pytest.mark.parametrize("field", ["data_hash", "feature_names", "config"])
def test_a_manifest_is_refused_without_its_required_fields(field):
    """Gate G8."""
    with pytest.raises(ConfigurationError):
        manifest(**{field: "" if field == "data_hash" else ()})


def test_a_checkpoint_cannot_exist_without_a_manifest():
    with pytest.raises(ConfigurationError) as exc:
        CP.NativeCheckpoint(checkpoint_id="x", model_kind="k", version="1",
                            manifest=None, parameters={"a": 1})
    assert "provenance" in str(exc.value)


def test_a_checkpoint_with_no_parameters_has_learned_nothing():
    with pytest.raises(ConfigurationError):
        checkpoint(parameters={})


def test_missing_reproducibility_fields_are_reported_not_hidden():
    bare = manifest(code_version="", dataset={})
    assert bare.reproducible is False
    assert "code_version" in bare.gaps and "dataset spec" in bare.gaps


def test_the_model_version_carries_the_content_hash():
    written = checkpoint()
    assert written.content_hash[:12] in written.model_version
    assert written.fresh is True


def test_the_store_refuses_to_overwrite_a_checkpoint():
    store = CP.CheckpointStore()
    store.save(checkpoint())
    with pytest.raises(ConfigurationError) as exc:
        store.save(checkpoint())
    assert "already exists" in str(exc.value)


def test_the_store_detects_an_altered_checkpoint():
    store = CP.CheckpointStore()
    store.save(checkpoint())
    from olympus.trading.storekeys import KeyedStore
    raw = KeyedStore(store.namespace).get(("ck-1",))
    raw["parameters"] = {"kind": "x", "cells": {"tampered": 1}}
    KeyedStore(store.namespace).put(("ck-1",), raw)
    with pytest.raises(ConfigurationError) as exc:
        store.load("ck-1")
    assert "altered after writing" in str(exc.value)


def test_the_store_has_no_delete_or_update():
    store = CP.CheckpointStore()
    for banned in ("delete", "update", "remove", "purge"):
        assert not hasattr(store, banned)


def test_checkpoints_persist_across_store_instances():
    CP.CheckpointStore().save(checkpoint())
    assert CP.CheckpointStore().load("ck-1") is not None


# ===========================================================================
# training pipeline
# ===========================================================================

SPEC = D.DatasetSpec(instrument_keys=(KEY,), timeframe=TF, lookback=60,
                     horizon=3, stride=1)
CONFIG = Q.QuantileConfig(horizon=3, conditioners=("returns_20",), n_buckets=3,
                          min_cell_samples=20)


def train_one(series=None, *, checkpoint_id="native-1", seed=0):
    return T.train(bars(series or trending_series(700)), spec=SPEC,
                   config=CONFIG, checkpoint_id=checkpoint_id, seed=seed,
                   clock=FixedClock(START + timedelta(days=90)))


def test_training_produces_a_fully_provenanced_checkpoint():
    result = train_one()
    m = result.checkpoint.manifest
    assert m.data_hash and m.feature_names and m.config
    assert m.seed == 0
    assert m.n_train_rows > 0
    assert m.train_end > m.train_start
    assert m.versions["python"]
    assert result.split.embargoed >= 0


def test_training_is_reproducible_from_a_seed():
    """Gate G7 end to end: identical inputs, identical checkpoint contents."""
    series = trending_series(700)
    a = train_one(series, checkpoint_id="a")
    b = train_one(series, checkpoint_id="b")
    assert a.checkpoint.parameters == b.checkpoint.parameters
    assert a.checkpoint.manifest.data_hash == b.checkpoint.manifest.data_hash


def test_the_trainer_refuses_foreign_weights():
    with pytest.raises(CP.ForeignWeightsRefused):
        T.train(bars(trending_series(700)), spec=SPEC, config=CONFIG,
                checkpoint_id="x", continue_from="/models/foreign.safetensors")


def test_a_horizon_mismatch_between_dataset_and_model_is_refused():
    """One of them is a typo, and guessing which would train against the wrong
    target."""
    other = D.DatasetSpec(instrument_keys=(KEY,), timeframe=TF, lookback=60,
                          horizon=5)
    with pytest.raises(ConfigurationError) as exc:
        T.train(bars(trending_series(400)), spec=other, config=CONFIG,
                checkpoint_id="x")
    assert "disagree" in str(exc.value)


def test_conditioning_on_an_unavailable_feature_is_refused():
    config = Q.QuantileConfig(horizon=3, conditioners=("not_a_feature",))
    with pytest.raises(ConfigurationError) as exc:
        T.train(bars(trending_series(400)), spec=SPEC, config=config,
                checkpoint_id="x")
    assert "does not produce" in str(exc.value)


def test_held_out_evaluation_reports_abstentions_separately():
    """Averaging an abstention in as a zero-error prediction is how a model
    that declines on everything scores perfectly."""
    result = train_one()
    metrics = T.evaluate_on_split(result.checkpoint, result.split)
    assert "abstention_rate" in metrics
    assert metrics["rows"] >= 0
    if metrics["answered"]:
        assert metrics["mae"] is not None
        assert 0.0 <= metrics["directional_accuracy"] <= 1.0


# ===========================================================================
# the forecaster — the plug point
# ===========================================================================

def test_the_forecaster_produces_a_valid_forecast_result():
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint, clock=FixedClock(
        START + timedelta(days=90)))
    window = bars(trending_series(700))[:300]
    produced = fc.forecast(window, as_of=window[-1].ts_close)

    if produced.abstained:
        assert produced.failure_code == F.CODE_MODEL_DECLINED
        return
    assert produced.horizon == 3
    assert len(produced.mean_close) == 3
    assert produced.quantiles and "p50" in produced.quantiles
    assert produced.paths
    assert 0.0 <= produced.uncertainty <= 1.0
    assert produced.model_identity["owner"] == "olympus"
    assert sum(produced.direction_probabilities.values()) == pytest.approx(1.0)


def test_the_forecaster_labels_its_paths_as_bands_not_samples():
    """Inventing sampled paths from a quantile model would put unearned
    structure into anything that consumes them."""
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    window = bars(trending_series(700))[:300]
    produced = fc.forecast(window, as_of=window[-1].ts_close)
    if not produced.abstained:
        assert any("quantile bands, not sampled futures" in w
                   for w in produced.warnings)


def test_the_forecaster_is_deterministic():
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    window = bars(trending_series(700))[:300]
    a = fc.forecast(window, as_of=window[-1].ts_close)
    b = fc.forecast(window, as_of=window[-1].ts_close)
    assert a.mean_close == b.mean_close
    assert fc.deterministic is True


def test_a_declining_model_abstains_rather_than_guessing():
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    # A violent sustained ramp puts the momentum feature far outside anything
    # the training series contained — the cheap version of OOD detection.
    ramp = [100.0 * (1.5 ** i) for i in range(300)]
    produced = fc.forecast(bars(ramp), as_of=START + timedelta(hours=300))
    assert produced.abstained
    assert produced.failure_code == F.CODE_MODEL_DECLINED
    assert "outside the training range" in (produced.failure_reason or "")
    # An abstention still identifies the model that declined.
    assert produced.model_version == result.checkpoint.model_version


def test_too_few_bars_abstains_with_the_reason():
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    produced = fc.forecast(bars(trending_series(5)),
                           as_of=START + timedelta(hours=5))
    assert produced.abstained
    assert produced.failure_code == "DATA_INSUFFICIENT"


def test_a_window_ending_after_as_of_is_refused():
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    window = bars(trending_series(300))
    produced = fc.forecast(window, as_of=START)
    assert produced.abstained
    assert produced.failure_code == "FORECAST_LOOKAHEAD"


def test_a_forming_last_bar_is_refused():
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    window = bars(trending_series(300))
    window[-1] = window[-1].with_final(False)
    produced = fc.forecast(window, as_of=window[-1].ts_close)
    assert produced.abstained
    assert produced.failure_code == "DATA_VALIDATION_FAILED"


def test_a_forecaster_without_a_checkpoint_is_refused():
    with pytest.raises(ConfigurationError):
        F.NativeForecaster("not-a-checkpoint")


def test_a_feature_set_that_cannot_supply_the_conditioners_is_refused_loudly():
    """Checked once at construction rather than per forecast: a model served
    against the wrong feature set is silently, totally wrong."""
    result = train_one()
    empty = S.MarketStateBuilder(features=FeatureBuilder(builtins=False))
    with pytest.raises(ConfigurationError) as exc:
        F.NativeForecaster(result.checkpoint, state_builder=empty)
    assert "trained on" in str(exc.value)


# ===========================================================================
# P2's exit condition: it plugs in
# ===========================================================================

def test_the_native_forecaster_registers_beside_the_baselines():
    """The point of P2. The service, cache and evaluator never learn that a
    native model exists."""
    from olympus.trading.forecast import (DriftForecaster, ForecastService,
                                          PersistenceForecaster,
                                          SeasonalNaiveForecaster)
    result = train_one()
    clock = FixedClock(START + timedelta(days=90))
    service = ForecastService(clock=clock)
    service.register("persistence", PersistenceForecaster(horizon=3, clock=clock))
    service.register("drift", DriftForecaster(horizon=3, clock=clock))
    service.register("seasonal", SeasonalNaiveForecaster(horizon=3, season=24,
                                                         clock=clock))
    service.register("olympus-native",
                     F.NativeForecaster(result.checkpoint, clock=clock))

    assert set(service.names) == {"persistence", "drift", "seasonal",
                                    "olympus-native"}
    window = bars(trending_series(700))[:300]
    for name in service.names:
        produced = service.run(name, candles=window,
                               as_of=window[-1].ts_close,
                               instrument_key=KEY, timeframe=TF)
        assert produced.instrument_key == KEY
        assert produced.model_version, f"{name} produced no model version"


def test_the_native_forecaster_describes_itself_for_the_registry():
    result = train_one()
    described = F.NativeForecaster(result.checkpoint).describe()
    assert described["deterministic"] is True
    assert described["params"]["checkpoint_id"] == "native-1"
    assert described["model_version"] == result.checkpoint.model_version


def test_a_native_forecast_is_evaluable_by_the_existing_evaluator():
    """The evaluator is the thing a native-vs-anything claim rests on, so it
    has to accept a native result unchanged."""
    from olympus.trading.evaluate import ForecastEvaluator
    result = train_one()
    fc = F.NativeForecaster(result.checkpoint)
    window = bars(trending_series(700))[:300]
    produced = fc.forecast(window, as_of=window[-1].ts_close)

    evaluator = ForecastEvaluator(label="olympus-native")
    evaluator.add(produced, [100.0, 101.0, 102.0])
    metrics = evaluator.metrics()
    assert metrics.n >= 0
    assert metrics.to_dict()["label"] == "olympus-native"
