"""Tests for the Olympus-owned Kronos forecasting interface.

Everything here runs against a deterministic fake `ModelBackend`, so the whole
pipeline — validation, look-ahead, normalisation, path handling, OHLC repair,
aggregation, uncertainty, abstention — is exercised offline with no torch, no
network, and no weights.

The fake lives here rather than in the product on purpose: upstream ships a
"backtester" whose predictions are a random walk (teardown §12.14), and a fake
that can be imported from the library is a fake that eventually reaches a
decision.

Each defect-regression test is named after the teardown section it pins.
"""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, DataQuality
from olympus.trading.errors import (
    ConfigurationError,
    DataValidationError,
    ForecastError,
)
from olympus.trading.instruments import parse_timeframe
from olympus.trading.kronos_adapter import KronosConfig, KronosForecaster
from olympus.trading.kronos_runtime import (
    KRONOS_MINI,
    KRONOS_SMALL,
    TOKENIZER_BASE,
    CheckpointPin,
    ModelBackend,
    SamplingParams,
)

KEY = "SIM:BTCUSD"
TF = "1m"
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)      # a Monday
STEP = parse_timeframe(TF)

_OPEN, _HIGH, _LOW, _CLOSE, _VOLUME, _AMOUNT = range(6)


# --- the deterministic fake -------------------------------------------------

class FakeBackend(ModelBackend):
    """A backend whose output is a pure function of its inputs and the seed.

    `spread` controls how much the paths disagree, which is the only lever the
    uncertainty tests need. `emit` replaces the whole generator when a test
    wants to inject pathological output (incoherent bars, NaNs, wrong shapes).
    """

    def __init__(self, *, max_context=512, spread=0.05, drift=0.0,
                 identity=None, emit=None):
        self._max_context = int(max_context)
        self._spread = float(spread)
        self._drift = float(drift)
        self._identity = dict(identity or {
            "model_version": "fake-kronos/v1", "device": "cpu",
            "torch_version": None})
        self._emit = emit
        self.calls = []

    @property
    def identity(self):
        return dict(self._identity)

    @property
    def max_context(self):
        return self._max_context

    def generate(self, context, *, context_stamps, future_stamps, horizon,
                 n_samples, params):
        self.calls.append({
            "context": tuple(tuple(row) for row in context),
            "context_stamps": tuple(tuple(s) for s in context_stamps),
            "future_stamps": tuple(tuple(s) for s in future_stamps),
            "horizon": horizon, "n_samples": n_samples, "params": params})
        if self._emit is not None:
            return self._emit(context, horizon, n_samples, params)
        paths = []
        for index in range(n_samples):
            rng = random.Random(int(params.seed) * 1_000_003 + index)
            level = float(context[-1][_CLOSE])
            rows = []
            for _ in range(horizon):
                level += self._drift + self._spread * (rng.random() - 0.5)
                rows.append((level, level, level, level, 1.0, 1.0))
            paths.append(tuple(rows))
        return tuple(paths)


def emit_rows(rows):
    """An `emit` that returns the same fixed rows for every path."""
    def emit(context, horizon, n_samples, params):
        return tuple(tuple(tuple(rows[i % len(rows)]) for i in range(horizon))
                     for _ in range(n_samples))
    return emit


# --- fixtures ---------------------------------------------------------------

def make_candles(count, *, start=T0, key=KEY, tf=TF, base=100.0,
                 skip=(), amplitude=0.004):
    """A gently oscillating window: enough variation for a realised volatility,
    no jumps that would trip the outlier detector."""
    step = parse_timeframe(tf)
    out = []
    for i in range(count):
        if i in skip:
            continue
        close = base * (1.0 + amplitude * math.sin(i / 3.0))
        open_ = base * (1.0 + amplitude * math.sin((i - 1) / 3.0))
        high = max(open_, close) * 1.0005
        low = min(open_, close) * 0.9995
        ts_open = start + step * i
        out.append(Candle(
            instrument_key=key, timeframe=tf, ts_open=ts_open,
            ts_close=ts_open + step, open=open_, high=high, low=low,
            close=close, volume=1000.0 + i, amount=(1000.0 + i) * close,
            source="test"))
    return out


def as_of_for(candles):
    return max(c.ts_close for c in candles if isinstance(c, Candle))


def build(config=None, backend=None, *, candles=None, clock=None,
          registry=None):
    candles = candles if candles is not None else make_candles(64)
    config = config or KronosConfig(context_length=32, horizon=4, n_samples=8,
                                    seed=11)
    backend = backend or FakeBackend()
    as_of = as_of_for(candles)
    clock = clock or FixedClock(as_of)
    forecaster = KronosForecaster(config, backend, clock,
                                  instrument_registry=registry)
    return forecaster, candles, as_of


def run(**kwargs):
    forecaster, candles, as_of = build(**kwargs)
    return forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                               timeframe=TF)


# ===========================================================================
# configuration
# ===========================================================================

def test_teardown_12_6_horizon_must_be_less_than_context():
    """§12.6 — upstream caps the decode window at max_context and then slices
    `-pred_len`, so `pred_len >= max_context` dies with
    `ValueError: Shape of passed values is (16, 6), indices imply (20, 6)`,
    which names neither number."""
    with pytest.raises(ConfigurationError) as excinfo:
        KronosConfig(context_length=32, horizon=32)
    assert "12.6" in str(excinfo.value)
    assert excinfo.value.details["context_length"] == 32
    assert excinfo.value.details["horizon"] == 32

    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=32, horizon=64)


def test_teardown_12_6_horizon_bounded_by_the_pin_max_context():
    with pytest.raises(ConfigurationError) as excinfo:
        KronosConfig(context_length=2000, horizon=600,
                     predictor_pin=KRONOS_SMALL)
    # context_length is checked first; both are named.
    assert excinfo.value.details["max_context"] == 512


def test_context_length_must_fit_the_pin():
    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=1024, horizon=8,
                     tokenizer_pin=TOKENIZER_BASE)
    # The 2k checkpoints legitimately accept it.
    KronosConfig(context_length=1024, horizon=8,
                 predictor_pin=KRONOS_MINI)


def test_teardown_12_1_top_k_and_top_p_are_rejected_together():
    """§12.1 — `top_k_top_p_filtering` returns right after the top-k branch
    (model/kronos.py:347-352), so `top_p` is silently ignored whenever
    `top_k > 0`. Olympus refuses the combination rather than honouring a
    sampling policy the model would discard."""
    with pytest.raises(ConfigurationError) as excinfo:
        KronosConfig(context_length=32, horizon=4, top_k=20, top_p=0.9)
    assert "12.1" in str(excinfo.value)

    # Either alone is fine.
    assert KronosConfig(context_length=32, horizon=4, top_k=20).top_p is None
    assert KronosConfig(context_length=32, horizon=4, top_p=0.9).top_k is None


def test_disabled_sentinels_do_not_count_as_set():
    """`top_k=0` and `top_p=1.0` are upstream's "off" values, so they normalise
    to None and must not trip the mutual-exclusion rule."""
    config = KronosConfig(context_length=32, horizon=4, top_k=0, top_p=1.0)
    assert config.top_k is None and config.top_p is None


@pytest.mark.parametrize("kwargs", [
    {"n_samples": 0}, {"n_samples": -1},
    {"temperature": 0.0}, {"temperature": -0.5},
    {"clip": 0.0},
    {"seed": -1},
    {"context_length": 2},
    {"horizon": 0},
    {"max_age_s": 0.0},
    {"min_completeness": 0.0}, {"min_completeness": 1.5},
    {"abstain_uncertainty_above": 1.5}, {"abstain_uncertainty_above": -0.1},
    {"top_k": -1}, {"top_p": 0.0}, {"top_p": 1.5},
    {"device": "gpu0"},
])
def test_config_rejects_unsupported_values(kwargs):
    base = {"context_length": 32, "horizon": 4}
    base.update(kwargs)
    with pytest.raises(ConfigurationError):
        KronosConfig(**base)


@pytest.mark.parametrize("quantiles", [(), (0.0, 0.5), (0.5, 1.0),
                                       (0.5, 0.5), (-0.1, 0.5), (1.5,)])
def test_config_rejects_bad_quantiles(quantiles):
    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=32, horizon=4, quantiles=quantiles)


def test_config_sorts_quantiles_and_is_json_safe():
    import json
    config = KronosConfig(context_length=32, horizon=4,
                          quantiles=(0.9, 0.1, 0.5),
                          predictor_pin=KRONOS_SMALL)
    assert config.quantiles == (0.1, 0.5, 0.9)
    json.dumps(config.to_dict())


def test_a_private_fine_tune_pin_bounds_the_config_the_same_way():
    """Operators running their own fine-tune get the same guards as the public
    checkpoints — the rules live on the pin, not on a hardcoded list."""
    private = CheckpointPin(repo_id="acme/kronos-5m", kind="predictor",
                            revision="b" * 40, expected_s1_bits=10,
                            expected_s2_bits=10, max_context=64)
    KronosConfig(context_length=64, horizon=8, predictor_pin=private)
    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=128, horizon=8, predictor_pin=private)
    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=64, horizon=64, predictor_pin=private)


def test_config_rejects_a_pin_of_the_wrong_kind():
    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=32, horizon=4, tokenizer_pin=KRONOS_SMALL)
    with pytest.raises(ConfigurationError):
        KronosConfig(context_length=32, horizon=4,
                     predictor_pin=TOKENIZER_BASE)


def test_forecaster_rejects_a_context_longer_than_the_loaded_checkpoint():
    config = KronosConfig(context_length=64, horizon=4)
    with pytest.raises(ConfigurationError):
        KronosForecaster(config, FakeBackend(max_context=32),
                         FixedClock(T0))


def test_forecaster_rejects_a_non_backend():
    config = KronosConfig(context_length=32, horizon=4)
    with pytest.raises(ConfigurationError):
        KronosForecaster(config, object(), FixedClock(T0))


# ===========================================================================
# the happy path
# ===========================================================================

def test_forecast_is_complete_and_self_describing():
    result = run()
    assert result.abstained is False
    assert result.usable is True
    assert result.instrument_key == KEY and result.timeframe == TF
    assert result.horizon == 4
    assert len(result.mean_close) == 4
    assert len(result.forecast_timestamps) == 4
    assert result.model_version == "fake-kronos/v1"
    assert result.model_identity["model_version"] == "fake-kronos/v1"
    assert result.latency_ms is not None and result.latency_ms >= 0.0
    # Provenance of the exact window used, not of everything handed in.
    assert result.input_end == as_of_for(make_candles(64))
    assert result.schema_version >= 1
    import json
    json.dumps(result.to_dict())


def test_teardown_12_7_every_sampled_path_is_kept():
    """§12.7 — `auto_regressive_inference` averages `sample_count` draws before
    returning (model/kronos.py:467), destroying the dispersion a risk engine
    needs. The adapter keeps all of them; the mean is a view."""
    config = KronosConfig(context_length=32, horizon=4, n_samples=16, seed=3)
    result = run(config=config)
    assert result.n_paths == 16
    assert all(len(p.closes) == 4 for p in result.paths)
    assert all(len(p.highs) == 4 and len(p.lows) == 4 for p in result.paths)
    # The mean really is the mean of the kept paths, not a separate draw.
    for step in range(4):
        expected = sum(p.closes[step] for p in result.paths) / 16
        assert result.mean_close[step] == pytest.approx(expected)


def test_quantiles_are_ordered_and_bracket_the_median():
    config = KronosConfig(context_length=32, horizon=4, n_samples=40, seed=5,
                          quantiles=(0.05, 0.25, 0.5, 0.75, 0.95))
    result = run(config=config)
    labels = ["p05", "p25", "p50", "p75", "p95"]
    assert set(result.quantiles) == set(labels)
    for step in range(4):
        values = [result.quantiles[label][step] for label in labels]
        assert values == sorted(values)
        assert values[0] <= result.median_close[step] <= values[-1]
        assert result.quantiles["p50"][step] == pytest.approx(
            result.median_close[step])


def test_expected_return_is_measured_from_the_last_observed_close():
    candles = make_candles(64)
    result = run(candles=candles)
    last_close = candles[-1].close
    assert result.expected_return == pytest.approx(
        result.mean_close[-1] / last_close - 1.0)
    assert len(result.expected_return_path) == 4
    assert result.expected_return_path[-1] == pytest.approx(
        result.expected_return)


def test_direction_probabilities_sum_to_one():
    result = run(config=KronosConfig(context_length=32, horizon=4,
                                     n_samples=25, seed=9))
    probabilities = result.direction_probabilities
    assert set(probabilities) == {"up", "down", "flat"}
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_realised_and_forecast_volatility_are_both_reported():
    result = run()
    assert result.realised_volatility > 0.0
    assert result.forecast_volatility > 0.0


def test_forecast_timestamps_follow_the_timeframe_grid():
    candles = make_candles(64)
    result = run(candles=candles)
    assert result.forecast_timestamps[0] == candles[-1].ts_close + STEP
    for previous, current in zip(result.forecast_timestamps,
                                 result.forecast_timestamps[1:]):
        assert current - previous == STEP


def test_supplied_future_timestamps_are_used_and_validated():
    forecaster, candles, as_of = build()
    stamps = [candles[-1].ts_close + STEP * i for i in range(1, 5)]
    result = forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                                 timeframe=TF, future_timestamps=stamps)
    assert list(result.forecast_timestamps) == stamps

    with pytest.raises(ConfigurationError):
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF, future_timestamps=stamps[:2])
    backwards = [stamps[0], stamps[0], stamps[2], stamps[3]]
    with pytest.raises(ConfigurationError):
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF, future_timestamps=backwards)
    inside = [candles[-1].ts_close] + stamps[1:]
    with pytest.raises(ConfigurationError):
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF, future_timestamps=inside)


def test_inference_params_make_the_run_reproducible_from_its_own_record():
    result = run()
    params = result.inference_params
    for key in ("context_length", "horizon", "n_samples", "temperature",
                "top_k", "top_p", "seed", "clip", "quantiles",
                "feature_columns", "quantile_interpolation", "context_bars",
                "n_paths", "context_mean", "context_std", "last_close",
                "std_epsilon"):
        assert key in params, key
    assert params["context_bars"] == 32
    assert params["n_paths"] == 8
    assert len(params["context_mean"]) == 6


def test_backend_receives_the_configured_sampling_params():
    backend = FakeBackend()
    config = KronosConfig(context_length=32, horizon=4, n_samples=8, seed=42,
                          temperature=0.7, top_p=0.85, clip=4.0)
    run(config=config, backend=backend)
    params = backend.calls[0]["params"]
    assert isinstance(params, SamplingParams)
    assert params.seed == 42 and params.temperature == 0.7
    assert params.top_p == 0.85 and params.top_k is None
    assert params.clip == 4.0
    assert len(backend.calls[0]["context"]) == 32
    assert len(backend.calls[0]["future_stamps"]) == 4


# ===========================================================================
# abstention
# ===========================================================================

def test_abstains_on_stale_data_with_the_stale_code():
    candles = make_candles(64)
    as_of = as_of_for(candles) + timedelta(hours=6)
    config = KronosConfig(context_length=32, horizon=4, max_age_s=300.0)
    forecaster = KronosForecaster(config, FakeBackend(), FixedClock(as_of))
    result = forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                                 timeframe=TF)
    assert result.abstained is True
    assert result.failure_code == "DATA_STALE"
    assert result.usable is False
    assert result.uncertainty == 1.0


def test_abstains_on_too_few_bars_with_the_insufficient_code():
    candles = make_candles(10)
    result = run(candles=candles)
    assert result.abstained is True
    assert result.failure_code == "DATA_INSUFFICIENT"


def test_abstains_on_unusable_quality_with_the_insufficient_code():
    """A window riddled with gaps falls below `min_completeness`, which makes it
    UNUSABLE. Gaps are counted, never filled — so the forecaster refuses rather
    than forecasting off interpolated bars."""
    candles = make_candles(64, skip=tuple(range(10, 40)))
    result = run(candles=candles)
    assert result.abstained is True
    assert result.failure_code == "DATA_INSUFFICIENT"
    assert "DATA_INCOMPLETE" in result.failure_reason


def test_abstains_on_malformed_input_with_the_validation_code():
    candles = make_candles(64)
    candles[5] = {"close": 100.0}                  # a dict, not a Candle
    result = run(candles=candles)
    assert result.abstained is True
    assert result.failure_code == "DATA_VALIDATION_FAILED"
    assert "not a Candle" in result.failure_reason


def test_abstains_on_an_empty_window():
    config = KronosConfig(context_length=32, horizon=4)
    forecaster = KronosForecaster(config, FakeBackend(), FixedClock(T0))
    result = forecaster.forecast([], as_of=T0, instrument_key=KEY,
                                 timeframe=TF)
    assert result.abstained is True
    assert result.failure_code == "DATA_INSUFFICIENT"


def test_abstains_when_uncertainty_exceeds_the_configured_limit():
    config = KronosConfig(context_length=32, horizon=4, n_samples=12, seed=1,
                          abstain_uncertainty_above=0.05)
    result = run(config=config, backend=FakeBackend(spread=5.0))
    assert result.abstained is True
    assert result.failure_code == "FORECAST_UNCERTAIN"
    assert "uncertainty" in result.failure_reason


def test_does_not_abstain_when_the_paths_agree():
    config = KronosConfig(context_length=32, horizon=4, n_samples=12, seed=1,
                          abstain_uncertainty_above=0.9)
    result = run(config=config, backend=FakeBackend(spread=1e-6))
    assert result.abstained is False


def test_abstains_rather_than_reporting_a_non_finite_forecast():
    nan_rows = [(float("nan"),) * 6]
    result = run(backend=FakeBackend(emit=emit_rows(nan_rows)))
    assert result.abstained is True
    assert result.failure_code == "FORECAST_ERROR"


def test_degraded_data_forecasts_but_records_the_quality():
    candles = make_candles(64)
    candles.append(candles[-1])                    # a duplicate bar
    result = run(candles=candles)
    assert result.abstained is False
    assert result.data_quality is DataQuality.DEGRADED
    assert any("duplicate" in w for w in result.warnings)


# ===========================================================================
# look-ahead
# ===========================================================================

def test_a_candle_closing_after_as_of_is_rejected():
    """The loudest check in the file. Validation would silently drop a future
    bar; that hides the caller's slicing bug behind a completeness warning, and
    a backtest that reads an unclosed bar prints an excellent Sharpe ratio."""
    candles = make_candles(64)
    as_of = candles[-2].ts_close                   # the last bar is in the future
    forecaster = KronosForecaster(
        KronosConfig(context_length=32, horizon=4), FakeBackend(),
        FixedClock(as_of))
    with pytest.raises(DataValidationError) as excinfo:
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF)
    assert "look-ahead" in str(excinfo.value)


def test_an_in_progress_bar_is_a_look_ahead_error_not_a_silent_drop():
    candles = make_candles(64)
    last = candles[-1]
    candles.append(Candle(
        instrument_key=KEY, timeframe=TF, ts_open=last.ts_close,
        ts_close=last.ts_close + STEP, open=last.close, high=last.close * 1.01,
        low=last.close * 0.99, close=last.close, volume=1.0, amount=1.0,
        is_final=False, source="test"))
    as_of = last.ts_close
    forecaster = KronosForecaster(
        KronosConfig(context_length=32, horizon=4), FakeBackend(),
        FixedClock(as_of))
    with pytest.raises(DataValidationError):
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF)


def test_as_of_ahead_of_the_clock_is_rejected():
    candles = make_candles(64)
    as_of = as_of_for(candles)
    forecaster = KronosForecaster(
        KronosConfig(context_length=32, horizon=4), FakeBackend(),
        FixedClock(as_of - timedelta(minutes=5)))
    with pytest.raises(DataValidationError) as excinfo:
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF)
    assert "future" in str(excinfo.value)


# ===========================================================================
# normalisation
# ===========================================================================

def test_teardown_12_9_normalisation_uses_context_stats_only():
    """§12.9 — `finetune_csv`'s `CustomKlineDataset` computes mean/std over
    `lookback + predict + 1` bars (finetune_base_model.py:125-127), leaking
    future statistics into the input scaling; `finetune/dataset.py` was fixed
    for exactly this and the fix was never ported.

    The inference-side statement of the same property: only the bars inside the
    context window may influence the normalisation. History at a wildly
    different scale, sitting immediately before the context, must change
    nothing at all.
    """
    tail = make_candles(32, start=T0 + STEP * 200, base=100.0)
    head = make_candles(200, start=T0, base=100.0)
    # Rebuild the head at a 10x price level, still on the same grid.
    scaled = [Candle(
        instrument_key=c.instrument_key, timeframe=c.timeframe,
        ts_open=c.ts_open, ts_close=c.ts_close, open=c.open * 10,
        high=c.high * 10, low=c.low * 10, close=c.close * 10,
        volume=c.volume * 10, amount=c.amount * 10, source=c.source)
        for c in head]

    config = KronosConfig(context_length=32, horizon=4, n_samples=8, seed=11)
    as_of = as_of_for(tail)
    clock = FixedClock(as_of)

    short_backend = FakeBackend()
    long_backend = FakeBackend()
    short = KronosForecaster(config, short_backend, clock).forecast(
        tail, as_of=as_of, instrument_key=KEY, timeframe=TF)
    long = KronosForecaster(config, long_backend, clock).forecast(
        scaled + tail, as_of=as_of, instrument_key=KEY, timeframe=TF)

    # The normalised context handed to the model is byte-identical...
    assert short_backend.calls[0]["context"] == long_backend.calls[0]["context"]
    # ...and so is every number that comes out.
    assert short.mean_close == long.mean_close
    assert short.median_close == long.median_close
    assert short.quantiles == long.quantiles
    assert short.expected_return == long.expected_return
    assert short.uncertainty == long.uncertainty
    assert short.inference_params["context_mean"] == \
        long.inference_params["context_mean"]


def test_normalisation_is_scale_free():
    """The same shape at a different price level normalises to the same input —
    that is the property the pretrained weights depend on.

    Only *approximately*, and deliberately so: the `std + 1e-5` guard is a fixed
    absolute epsilon (upstream's, kept byte-identical because the weights were
    trained on inputs scaled exactly that way), so it perturbs a 10-unit series
    marginally more than a 10,000-unit one. The residual is ~1e-4 relative,
    which is the epsilon's footprint and nothing else.
    """
    cheap = make_candles(64, base=10.0)
    dear = make_candles(64, base=10_000.0)
    config = KronosConfig(context_length=32, horizon=4, n_samples=4, seed=2)
    clock = FixedClock(as_of_for(cheap))
    a, b = FakeBackend(), FakeBackend()
    KronosForecaster(config, a, clock).forecast(
        cheap, as_of=as_of_for(cheap), instrument_key=KEY, timeframe=TF)
    KronosForecaster(config, b, clock).forecast(
        dear, as_of=as_of_for(dear), instrument_key=KEY, timeframe=TF)
    for left, right in zip(a.calls[0]["context"], b.calls[0]["context"]):
        for x, y in zip(left, right):
            assert x == pytest.approx(y, abs=1e-3)


def test_normalised_context_is_clipped_to_the_configured_bound():
    config = KronosConfig(context_length=32, horizon=4, clip=1.0, n_samples=2)
    backend = FakeBackend()
    run(config=config, backend=backend)
    for row in backend.calls[0]["context"]:
        assert all(-1.0 - 1e-9 <= v <= 1.0 + 1e-9 for v in row)


# ===========================================================================
# OHLC repair
# ===========================================================================

def test_teardown_12_7_incoherent_bars_are_repaired_and_reported():
    """§12.7 — nothing upstream enforces `high >= max(open, close)`,
    `low <= min(open, close)`, or `volume >= 0`, and downstream code compensates
    ad hoc (a ±10% clamp in one example, a "validation pass" in another).
    Olympus repairs at the boundary and records that it did."""
    # In normalised space: close well above open, high *below* both, low above
    # both, and a hugely negative volume/amount.
    broken = [(0.0, -5.0, 5.0, 5.0, -1000.0, -1000.0)]
    result = run(backend=FakeBackend(emit=emit_rows(broken)))

    assert result.abstained is False
    for path in result.paths:
        for step in range(result.horizon):
            o, h, l, c = (path.opens[step], path.highs[step],
                          path.lows[step], path.closes[step])
            assert h >= max(o, c) - 1e-9
            assert l <= min(o, c) + 1e-9
            assert h >= l
            assert path.volumes[step] >= 0.0
    assert any("repaired OHLC coherence" in w for w in result.warnings)


def test_coherent_output_produces_no_repair_warning():
    result = run()
    assert not any("repaired OHLC" in w for w in result.warnings)


def test_averaging_paths_never_produces_an_incoherent_mean_candle():
    """Upstream's §12.7 note is that averaging in feature space can break
    coherence even when every path is valid. Repairing per path first makes the
    mean coherent by construction (means preserve pointwise domination); this
    pins it against a deliberately dispersed backend."""
    config = KronosConfig(context_length=32, horizon=4, n_samples=32, seed=4)
    result = run(config=config, backend=FakeBackend(spread=2.0))
    for step in range(result.horizon):
        mean_open = sum(p.opens[step] for p in result.paths) / result.n_paths
        mean_high = sum(p.highs[step] for p in result.paths) / result.n_paths
        mean_low = sum(p.lows[step] for p in result.paths) / result.n_paths
        mean_close = result.mean_close[step]
        assert mean_high >= max(mean_open, mean_close) - 1e-9
        assert mean_low <= min(mean_open, mean_close) + 1e-9


# ===========================================================================
# uncertainty
# ===========================================================================

def test_uncertainty_is_one_for_a_single_sample():
    """One path carries no dispersion information. Reporting anything lower
    would manufacture confidence out of an empty sample — which is exactly what
    upstream's default `sample_count=1` invites (teardown 14.2)."""
    config = KronosConfig(context_length=32, horizon=4, n_samples=1, seed=1)
    result = run(config=config)
    assert result.n_paths == 1
    assert result.uncertainty == 1.0
    assert result.confidence == 0.0


def test_uncertainty_falls_as_the_paths_agree():
    config = KronosConfig(context_length=32, horizon=4, n_samples=24, seed=7)
    wide = run(config=config, backend=FakeBackend(spread=4.0))
    narrow = run(config=config, backend=FakeBackend(spread=0.01))
    tight = run(config=config, backend=FakeBackend(spread=1e-9))
    assert wide.uncertainty > narrow.uncertainty > tight.uncertainty
    assert 0.0 <= tight.uncertainty <= wide.uncertainty <= 1.0


def test_uncertainty_is_bounded_even_for_absurd_dispersion():
    config = KronosConfig(context_length=32, horizon=4, n_samples=16, seed=7)
    result = run(config=config, backend=FakeBackend(spread=1e9))
    assert 0.0 <= result.uncertainty <= 1.0
    assert result.uncertainty > 0.99


def test_identical_paths_give_zero_uncertainty():
    flat = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)]
    config = KronosConfig(context_length=32, horizon=4, n_samples=8)
    result = run(config=config, backend=FakeBackend(emit=emit_rows(flat)))
    assert result.uncertainty == 0.0


# ===========================================================================
# determinism
# ===========================================================================

def test_same_seed_and_input_produce_identical_numbers():
    config = KronosConfig(context_length=32, horizon=4, n_samples=16, seed=99)
    first = run(config=config)
    second = run(config=config)
    assert first.mean_close == second.mean_close
    assert first.median_close == second.median_close
    assert first.quantiles == second.quantiles
    assert first.expected_return == second.expected_return
    assert first.expected_return_path == second.expected_return_path
    assert first.forecast_volatility == second.forecast_volatility
    assert first.realised_volatility == second.realised_volatility
    assert first.direction_probabilities == second.direction_probabilities
    assert first.uncertainty == second.uncertainty
    assert [p.closes for p in first.paths] == [p.closes for p in second.paths]


def test_a_different_seed_produces_a_different_forecast():
    a = run(config=KronosConfig(context_length=32, horizon=4, n_samples=16,
                                seed=1))
    b = run(config=KronosConfig(context_length=32, horizon=4, n_samples=16,
                                seed=2))
    assert a.mean_close != b.mean_close


# ===========================================================================
# backend contract
# ===========================================================================

def test_teardown_12_7_a_backend_that_collapses_paths_is_a_hard_error():
    """Averaging inside inference is the upstream defect; a backend that does it
    is a bug in the backend, and the adapter is where wrong shapes stop."""
    def collapse(context, horizon, n_samples, params):
        return (tuple((0.0,) * 6 for _ in range(horizon)),)

    with pytest.raises(ForecastError) as excinfo:
        run(backend=FakeBackend(emit=collapse))
    assert "12.7" in str(excinfo.value)


def test_a_backend_with_the_wrong_horizon_is_a_hard_error():
    def short(context, horizon, n_samples, params):
        return tuple(tuple((0.0,) * 6 for _ in range(horizon - 1))
                     for _ in range(n_samples))

    with pytest.raises(ForecastError):
        run(backend=FakeBackend(emit=short))


def test_a_backend_with_the_wrong_feature_width_is_a_hard_error():
    def narrow(context, horizon, n_samples, params):
        return tuple(tuple((0.0,) * 4 for _ in range(horizon))
                     for _ in range(n_samples))

    with pytest.raises(ForecastError):
        run(backend=FakeBackend(emit=narrow))


# ===========================================================================
# wiring
# ===========================================================================

def test_instrument_registry_refuses_an_unknown_instrument():
    from olympus.trading.instruments import build_default_registry
    registry = build_default_registry()
    forecaster, candles, as_of = build(registry=registry)
    with pytest.raises(ConfigurationError):
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe=TF)


def test_identity_and_timeframe_are_derived_from_the_candles():
    forecaster, candles, as_of = build()
    result = forecaster.forecast(candles, as_of=as_of)
    assert result.instrument_key == KEY
    assert result.timeframe == TF


def test_an_empty_window_without_an_explicit_identity_is_a_wiring_error():
    config = KronosConfig(context_length=32, horizon=4)
    forecaster = KronosForecaster(config, FakeBackend(), FixedClock(T0))
    with pytest.raises(ConfigurationError):
        forecaster.forecast([], as_of=T0)


def test_a_malformed_timeframe_is_a_wiring_error():
    forecaster, candles, as_of = build()
    with pytest.raises(ConfigurationError):
        forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                            timeframe="1 fortnight")


def test_the_forecaster_cannot_reach_execution():
    """Structural, not stylistic: a forecasting component that can import the
    OMS is a forecasting component that can put an order on a venue without
    passing risk. Checked against the module's real import graph rather than a
    substring scan, so prose about execution in the docstring does not count and
    an aliased import cannot slip past."""
    import ast
    import pathlib
    import olympus.trading.kronos_adapter as adapter

    tree = ast.parse(pathlib.Path(adapter.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
            imported.update(alias.name for alias in node.names)

    forbidden = {"oms", "execution", "brokers", "risk", "strategy", "reconcile",
                 "signals", "audit", "modes", "killswitch",
                 "Order", "Fill", "TradeIntent", "RiskDecision", "Position"}
    assert not (imported & forbidden), sorted(imported & forbidden)
    # And nothing execution-shaped leaks into the module namespace either.
    for name in ("Order", "Fill", "TradeIntent", "RiskDecision"):
        assert not hasattr(adapter, name), name


def test_the_forecaster_is_reusable_across_calls():
    forecaster, candles, as_of = build()
    first = forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                                timeframe=TF)
    second = forecaster.forecast(candles, as_of=as_of, instrument_key=KEY,
                                 timeframe=TF)
    assert first.mean_close == second.mean_close
