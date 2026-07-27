"""Tests for feature engineering — causality and leakage above all.

The two failures this module exists to prevent do not raise. A peeking feature
produces a backtest that looks excellent; a scaler fitted on the test window
produces a validation curve that looks excellent. Both are only detectable by
asserting the property directly, which is what most of these tests do:

* `assert_causal` must *catch* a deliberately peeking transform and must pass a
  legitimate one — a causality checker that never fires is worse than none.
* `fit_scaler` statistics must come from the training rows only, and applying
  them to a shifted test window must produce shifted output, not centred
  output. Centred test output is the signature of the leak.
* `causal_window_normalise` must reproduce the fix that KRONOS_TEARDOWN §12.9
  says was never ported to `finetune_csv`.
"""

from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading import ta
from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle
from olympus.trading.errors import ConfigurationError, DataValidationError
from olympus.trading.features import (
    BUILTIN_FEATURES,
    DEFAULT_FEATURE_NAMES,
    FeatureBuilder,
    FeatureSet,
    ScalerStats,
    assert_builder_causal,
    assert_causal,
    causal_window_normalise,
    fit_scaler,
    fit_transform,
    split_by_time,
)

KEY = "SIM:BTCUSD"
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
CLOCK = FixedClock(LATER)


def bar(index: int, close: float, *, open_: float = None, volume: float = 100.0,
        key: str = KEY, timeframe: str = "1m", is_final: bool = True) -> Candle:
    ts_open = T0 + timedelta(minutes=index)
    open_ = close if open_ is None else open_
    return Candle(instrument_key=key, timeframe=timeframe, ts_open=ts_open,
                  ts_close=ts_open + timedelta(minutes=1), open=open_,
                  high=max(open_, close) * 1.01, low=min(open_, close) * 0.99,
                  close=close, volume=volume, amount=close * volume,
                  source="test")


def ramp(n: int, start: float = 100.0, step: float = 0.5):
    """A gently rising, slightly wobbly series — enough variation that a
    volatility or z-score feature is not divided by zero."""
    closes = [start + i * step + (0.3 if i % 3 == 0 else -0.2) for i in range(n)]
    return [bar(i, close, open_=closes[i - 1] if i else closes[0],
                volume=100.0 + (i % 7) * 10.0)
            for i, close in enumerate(closes)]


# --- FeatureSet ------------------------------------------------------------

def test_featureset_roundtrips_to_a_json_safe_dict():
    fs = FeatureSet(instrument_key=KEY, timeframe="1m", ts=T0,
                    values={"rsi_14": 55.5})
    payload = fs.to_dict()
    assert payload["instrument_key"] == KEY
    assert payload["ts"] == T0.isoformat()
    assert payload["values"] == {"rsi_14": 55.5}
    import json
    json.dumps(payload)


def test_featureset_get_returns_the_default_for_an_absent_feature():
    fs = FeatureSet(instrument_key=KEY, timeframe="1m", ts=T0, values={"a": 1.0})
    assert fs.get("a") == 1.0
    assert fs.get("missing") is None
    assert fs.get("missing", -1.0) == -1.0
    assert "a" in fs and "missing" not in fs


def test_featureset_copies_its_values_so_the_caller_cannot_mutate_history():
    source = {"a": 1.0}
    fs = FeatureSet(instrument_key=KEY, timeframe="1m", ts=T0, values=source)
    source["a"] = 999.0
    assert fs.get("a") == 1.0


def test_featureset_rejects_nan_rather_than_dropping_it():
    with pytest.raises(DataValidationError):
        FeatureSet(instrument_key=KEY, timeframe="1m", ts=T0,
                   values={"a": float("nan")})


def test_featureset_requires_aware_timestamps():
    with pytest.raises(DataValidationError):
        FeatureSet(instrument_key=KEY, timeframe="1m",
                   ts=datetime(2026, 3, 2, 12, 0), values={})


def test_featureset_vector_uses_the_callers_order_not_the_dicts():
    fs = FeatureSet(instrument_key=KEY, timeframe="1m", ts=T0,
                    values={"b": 2.0, "a": 1.0})
    assert fs.vector(["a", "b"]) == [1.0, 2.0]
    assert fs.vector(["b", "a"]) == [2.0, 1.0]
    assert fs.vector(["a", "zzz"], default=0.0) == [1.0, 0.0]


# --- FeatureBuilder --------------------------------------------------------

def test_builder_registers_every_documented_builtin():
    builder = FeatureBuilder()
    expected = {"returns_1", "returns_5", "returns_20", "log_return",
                "realised_vol_20", "atr_pct", "rsi_14", "macd_hist",
                "bb_position", "volume_zscore", "range_pct", "gap_pct",
                "trend_strength", "distance_from_sma_50"}
    assert set(builder.names()) == expected
    assert set(DEFAULT_FEATURE_NAMES) == expected
    assert builder.warmup_bars == 50


def test_build_produces_every_feature_once_the_window_is_long_enough():
    builder = FeatureBuilder()
    bars = ramp(80)
    fs = builder.build(bars, CLOCK)
    assert set(fs.values) == set(builder.names())
    assert fs.instrument_key == KEY
    assert fs.timeframe == "1m"
    assert fs.ts == bars[-1].ts_close


def test_missing_features_are_absent_not_zero_filled():
    builder = FeatureBuilder()
    fs = builder.build(ramp(10), CLOCK)
    assert "returns_1" in fs.values
    assert "distance_from_sma_50" not in fs.values   # needs 50 bars
    assert fs.get("distance_from_sma_50") is None


def test_builtin_values_match_the_underlying_indicators():
    builder = FeatureBuilder()
    bars = ramp(80)
    fs = builder.build(bars, CLOCK)
    closes = [c.close for c in bars]
    assert fs.get("rsi_14") == pytest.approx(ta.rsi(closes, 14)[-1])
    assert fs.get("macd_hist") == pytest.approx(ta.macd(closes, 12, 26, 9)[2][-1])
    assert fs.get("trend_strength") == pytest.approx(
        ta.trend_strength(closes, 20)[-1])
    assert fs.get("returns_1") == pytest.approx(closes[-1] / closes[-2] - 1.0)
    assert fs.get("distance_from_sma_50") == pytest.approx(
        closes[-1] / ta.sma(closes, 50)[-1] - 1.0)
    assert fs.get("atr_pct") == pytest.approx(
        ta.atr(bars, 14)[-1] / closes[-1])


def test_register_rejects_a_silent_overwrite():
    builder = FeatureBuilder(builtins=False)
    builder.register("x", lambda w: 1.0, 1)
    with pytest.raises(ConfigurationError):
        builder.register("x", lambda w: 2.0, 1)
    builder.register("x", lambda w: 2.0, 1, replace=True)
    assert builder.build(ramp(3), CLOCK).get("x") == 2.0


def test_register_validates_its_arguments():
    builder = FeatureBuilder(builtins=False)
    with pytest.raises(ConfigurationError):
        builder.register("", lambda w: 1.0, 1)
    with pytest.raises(ConfigurationError):
        builder.register("x", "not callable", 1)
    with pytest.raises(ConfigurationError):
        builder.register("x", lambda w: 1.0, 0)


def test_unregister_and_min_bars():
    builder = FeatureBuilder()
    assert builder.min_bars("rsi_14") == 15
    builder.unregister("rsi_14")
    assert "rsi_14" not in builder.names()
    with pytest.raises(ConfigurationError):
        builder.min_bars("rsi_14")


def test_build_refuses_a_non_final_candle():
    builder = FeatureBuilder(builtins=False).register("x", lambda w: 1.0, 1)
    bars = ramp(5)
    bars[-1] = bars[-1].with_final(False)
    with pytest.raises(DataValidationError):
        builder.build(bars, CLOCK)


def test_build_refuses_mixed_instruments_timeframes_and_disordered_bars():
    builder = FeatureBuilder(builtins=False).register("x", lambda w: 1.0, 1)
    mixed = ramp(3)
    mixed[1] = bar(1, 100.0, key="SIM:ETHUSD")
    with pytest.raises(DataValidationError):
        builder.build(mixed, CLOCK)

    frames = ramp(3)
    frames[1] = bar(1, 100.0, timeframe="5m")
    with pytest.raises(DataValidationError):
        builder.build(frames, CLOCK)

    reversed_bars = list(reversed(ramp(3)))
    with pytest.raises(DataValidationError):
        builder.build(reversed_bars, CLOCK)


def test_build_refuses_a_window_the_clock_has_not_reached():
    """A backtest that hands the builder bars from the future is the bug this
    check exists to make loud."""
    builder = FeatureBuilder(builtins=False).register("x", lambda w: 1.0, 1)
    bars = ramp(5)
    early = FixedClock(bars[-1].ts_close - timedelta(minutes=1))
    with pytest.raises(DataValidationError):
        builder.build(bars, early)
    exact = FixedClock(bars[-1].ts_close)
    assert builder.build(bars, exact).get("x") == 1.0


def test_build_rejects_an_empty_window_and_non_candles():
    builder = FeatureBuilder()
    with pytest.raises(DataValidationError):
        builder.build([], CLOCK)
    with pytest.raises(DataValidationError):
        builder.build([1.0, 2.0], CLOCK)


def test_build_rejects_a_feature_that_returns_a_non_number():
    builder = FeatureBuilder(builtins=False).register("x", lambda w: "up", 1)
    with pytest.raises(DataValidationError):
        builder.build(ramp(3), CLOCK)


def test_build_rejects_a_feature_that_returns_nan():
    builder = FeatureBuilder(builtins=False)
    builder.register("x", lambda w: float("nan"), 1)
    with pytest.raises(DataValidationError):
        builder.build(ramp(3), CLOCK)


def test_series_is_aligned_and_respects_min_bars():
    builder = FeatureBuilder()
    bars = ramp(60)
    series = builder.series("rsi_14", bars, clock=CLOCK)
    assert len(series) == len(bars)
    assert series[:14] == [None] * 14
    assert series[14] is not None
    assert series[-1] == pytest.approx(builder.build(bars, CLOCK).get("rsi_14"))


def test_build_all_rows_equal_what_build_would_have_produced_live():
    builder = FeatureBuilder(builtins=False)
    builder.register("returns_1", BUILTIN_FEATURES[0][1], 2)
    bars = ramp(12)
    rows = builder.build_all(bars, clock=CLOCK)
    assert len(rows) == len(bars) - 1
    for offset, row in enumerate(rows):
        live = builder.build(bars[:offset + 2], CLOCK)
        assert row.values == live.values
        assert row.ts == live.ts


# --- causality -------------------------------------------------------------

def _causal_sma(candles):
    """A legitimate transform: trailing 5-bar mean of close."""
    return ta.sma([c.close for c in candles], 5)


def _peeking_centred_average(candles):
    """A *peeking* transform: averages this bar with the NEXT one.

    This is the classic centred-window bug. It never raises; it just makes
    every backtest signal arrive one bar before the information it uses.
    """
    closes = [c.close for c in candles]
    out = []
    for i in range(len(closes)):
        out.append((closes[i] + closes[i + 1]) / 2.0
                   if i + 1 < len(closes) else None)
    return out


def _peeking_full_sample_zscore(candles):
    """A subtler peek: z-scored against the mean and stdev of the WHOLE series.

    Prefix truncation hides this one (the prefix is its own whole series), so
    only the future-perturbation check catches it. It is the same shape as the
    Kronos finetune_csv normalisation leak.
    """
    closes = [c.close for c in candles]
    mean = sum(closes) / len(closes)
    spread = (sum((x - mean) ** 2 for x in closes) / len(closes)) ** 0.5 or 1.0
    return [(x - mean) / spread for x in closes]


def _misaligned(candles):
    """Returns only the defined region — the short-return bug."""
    return [c.close for c in candles][4:]


def test_assert_causal_passes_a_legitimate_trailing_indicator():
    assert_causal(_causal_sma, ramp(15)) is None


def test_assert_causal_catches_a_feature_that_reads_the_next_bar():
    with pytest.raises(DataValidationError) as excinfo:
        assert_causal(_peeking_centred_average, ramp(12))
    assert "not causal" in str(excinfo.value)


def test_assert_causal_catches_a_transform_fitted_on_the_whole_series():
    with pytest.raises(DataValidationError) as excinfo:
        assert_causal(_peeking_full_sample_zscore, ramp(12))
    assert "leaking backwards" in str(excinfo.value)


def test_assert_causal_catches_a_misaligned_output():
    with pytest.raises(DataValidationError) as excinfo:
        assert_causal(_misaligned, ramp(12))
    assert "one value per candle" in str(excinfo.value)


def test_assert_causal_needs_at_least_two_candles():
    with pytest.raises(ConfigurationError):
        assert_causal(_causal_sma, ramp(1))


def test_every_builtin_feature_is_causal():
    """The expensive, end-to-end version: each built-in evaluated over
    expanding windows must be unaffected by anything after its bar."""
    assert_builder_causal(FeatureBuilder(), ramp(40), clock=CLOCK) is None


def test_assert_builder_causal_catches_a_peeking_custom_feature():
    """A registered feature can still peek if it closes over history it was
    not handed. This is the realistic version of the bug."""
    history = ramp(20)

    def peeking(window):
        # Ignores its own window and reads the last bar of the full history.
        return history[len(window)].close if len(window) < len(history) else None

    builder = FeatureBuilder(builtins=False).register("peek", peeking, 1)
    with pytest.raises(DataValidationError):
        assert_builder_causal(builder, history, clock=CLOCK)


# --- normalisation: fit on train, transform later --------------------------

def rows_at(values, *, start_minute: int = 0):
    """FeatureSets one minute apart carrying a single feature ``x``."""
    return [FeatureSet(instrument_key=KEY, timeframe="1m",
                       ts=T0 + timedelta(minutes=start_minute + i),
                       values={"x": float(v)})
            for i, v in enumerate(values)]


def test_fit_scaler_uses_only_the_rows_it_was_given():
    train = rows_at([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    stats = fit_scaler(train, method="zscore")
    assert stats.center["x"] == pytest.approx(5.5)
    assert stats.scale["x"] == pytest.approx(8.25 ** 0.5)
    assert stats.n_samples == 10
    assert stats.columns == ("x",)


def test_transform_uses_train_statistics_on_a_shifted_test_window():
    """The leakage test. If the scaler were refitted on the test rows their
    z-scores would centre on zero; with train statistics they must land far
    from zero, because the test window really did move."""
    train = rows_at([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    test = rows_at([1001, 1002, 1003], start_minute=100)
    stats = fit_scaler(train, method="zscore")

    transformed = stats.transform_many(test)
    assert all(row["x"] > 300.0 for row in transformed)

    # And the statistics themselves must not have moved.
    refitted_on_everything = fit_scaler(train + test, method="zscore")
    assert refitted_on_everything.center["x"] != pytest.approx(stats.center["x"])
    assert stats.center["x"] == pytest.approx(5.5)


def test_fit_transform_only_ever_touches_the_training_rows():
    train = rows_at([1, 2, 3, 4, 5])
    stats, transformed = fit_transform(train, method="minmax")
    assert len(transformed) == 5
    assert transformed[0]["x"] == pytest.approx(0.0)
    assert transformed[-1]["x"] == pytest.approx(1.0)
    assert stats.method == "minmax"


def test_robust_scaler_is_not_dragged_by_a_single_crash_bar():
    train = rows_at([1, 2, 3, 4, 5, 6, 7, 8, 9, 1000])
    robust = fit_scaler(train, method="robust")
    zscore = fit_scaler(train, method="zscore")
    assert robust.center["x"] == pytest.approx(5.5)
    assert zscore.center["x"] > 100.0


def test_degenerate_columns_scale_by_one_instead_of_exploding():
    stats = fit_scaler(rows_at([7, 7, 7, 7]), method="zscore")
    assert stats.degenerate == ("x",)
    assert stats.scale["x"] == 1.0
    assert stats.transform({"x": 9.0})["x"] == pytest.approx(2.0)


def test_transform_omits_a_missing_column_and_rejects_an_unknown_one():
    stats = fit_scaler(rows_at([1, 2, 3]), method="zscore")
    assert stats.transform({}) == {}
    with pytest.raises(DataValidationError):
        stats.transform({"x": 1.0, "surprise": 2.0})
    assert "surprise" not in stats.transform({"x": 1.0, "surprise": 2.0},
                                             strict=False)


def test_inverse_transform_round_trips():
    stats = fit_scaler(rows_at([1, 2, 3, 4, 5]), method="zscore")
    scaled = stats.transform({"x": 4.0})
    assert stats.inverse_transform(scaled)["x"] == pytest.approx(4.0)
    with pytest.raises(DataValidationError):
        stats.inverse_transform({"nope": 1.0})


def test_scaler_stats_are_frozen_and_serialisable():
    stats = fit_scaler(rows_at([1, 2, 3]), method="zscore")
    with pytest.raises(Exception):
        stats.center = {}                      # frozen dataclass
    payload = stats.to_dict()
    import json
    json.dumps(payload)
    assert payload["method"] == "zscore"
    assert payload["fitted_from"] == T0.isoformat()


def test_fit_scaler_validates_its_inputs():
    with pytest.raises(DataValidationError):
        fit_scaler([])
    with pytest.raises(ConfigurationError):
        fit_scaler(rows_at([1, 2]), method="magic")
    with pytest.raises(DataValidationError):
        fit_scaler([{"x": float("inf")}])
    with pytest.raises(DataValidationError):
        fit_scaler([object()])


def test_fit_scaler_accepts_plain_mappings_as_well_as_featuresets():
    stats = fit_scaler([{"x": 1.0}, {"x": 3.0}], method="minmax")
    assert stats.center["x"] == 1.0 and stats.scale["x"] == 2.0
    assert stats.fitted_from is None


def test_split_by_time_is_chronological():
    rows = rows_at([1, 2, 3, 4, 5])
    train, test = split_by_time(rows, split_at=T0 + timedelta(minutes=3))
    assert [r.get("x") for r in train] == [1.0, 2.0, 3.0]
    assert [r.get("x") for r in test] == [4.0, 5.0]


def test_split_by_time_requires_featuresets():
    with pytest.raises(DataValidationError):
        split_by_time([{"x": 1.0}], split_at=T0)


# --- the Kronos §12.9 fix --------------------------------------------------

def test_causal_window_normalise_uses_lookback_statistics_only():
    """KRONOS_TEARDOWN §12.9: `finetune_csv` normalises across lookback +
    horizon, so the horizon's own mean and spread scale the model's input.

    Here the lookback statistics (mean 5.5, population stdev sqrt(8.25)) must
    scale the whole sequence, leaving the horizon far from zero. The buggy
    full-window version would pull it back towards the middle.
    """
    lookback_values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    horizon_values = [1000, 1001, 1002]
    series = lookback_values + horizon_values

    out = causal_window_normalise(series, lookback=10, method="zscore")
    assert len(out) == len(series)

    expected_scale = 8.25 ** 0.5
    assert out[0] == pytest.approx((1 - 5.5) / expected_scale)
    assert out[9] == pytest.approx((10 - 5.5) / expected_scale)
    assert out[10] == pytest.approx((1000 - 5.5) / expected_scale)
    assert out[10] > 300.0

    # The leaky version: statistics over the full window drag the horizon in.
    full_mean = sum(series) / len(series)
    full_spread = (sum((x - full_mean) ** 2 for x in series) / len(series)) ** 0.5
    leaky_horizon = (1000 - full_mean) / full_spread
    assert leaky_horizon < 2.0
    assert out[10] > leaky_horizon * 100


def test_causal_window_normalise_handles_a_flat_lookback():
    out = causal_window_normalise([5.0] * 4 + [9.0], lookback=4)
    assert out[:4] == [0.0, 0.0, 0.0, 0.0]
    assert out[4] == pytest.approx(4.0)      # spread 0 -> scale 1, not infinity


def test_causal_window_normalise_validates_inputs():
    with pytest.raises(ConfigurationError):
        causal_window_normalise([1.0, 2.0], lookback=0)
    with pytest.raises(ConfigurationError):
        causal_window_normalise([1.0, 2.0], lookback=1, method="magic")
    with pytest.raises(DataValidationError):
        causal_window_normalise([1.0, 2.0], lookback=5)
    with pytest.raises(DataValidationError):
        causal_window_normalise([1.0, float("nan")], lookback=1)


@pytest.mark.parametrize("method", ["zscore", "minmax", "robust"])
def test_causal_window_normalise_supports_every_method(method):
    out = causal_window_normalise([1, 2, 3, 4, 5, 99], lookback=5, method=method)
    assert len(out) == 6
    assert out[-1] > out[-2]
