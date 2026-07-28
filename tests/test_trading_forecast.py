"""Tests for `olympus.trading.forecast`.

The properties under test are the ones the rest of the stack relies on rather
than the arithmetic of any one baseline: a forecaster can never break the
trading loop, a baseline produces a *comparable* result, and the cache cannot
serve a forecast that was made under different sampling settings.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, DataQuality, ForecastResult
from olympus.trading.errors import (ConfigurationError, ModelNotAvailable,
                                    TradingError)
from olympus.trading.forecast import (CODE_CONTRACT, CODE_INSUFFICIENT,
                                      CODE_INVALID, CODE_LOOKAHEAD, CODE_STALE,
                                      CODE_UNEXPECTED, BaselineForecaster,
                                      DriftForecaster, ForecastCache,
                                      Forecaster, ForecastService,
                                      PersistenceForecaster,
                                      SeasonalNaiveForecaster, params_hash)

START = datetime(2026, 3, 2, tzinfo=timezone.utc)
KEY = "SIM:ABC"
TF = "1h"


def candle(i: int, price: float, *, key: str = KEY, timeframe: str = TF,
           is_final: bool = True) -> Candle:
    return Candle(instrument_key=key, timeframe=timeframe,
                  ts_open=START + timedelta(hours=i),
                  ts_close=START + timedelta(hours=i + 1),
                  open=price, high=price * 1.01, low=price * 0.99,
                  close=price, volume=100.0 + i, is_final=is_final)


def series(prices, *, key: str = KEY, timeframe: str = TF) -> list[Candle]:
    return [candle(i, p, key=key, timeframe=timeframe)
            for i, p in enumerate(prices)]


def rising(n: int = 60, start: float = 100.0, step: float = 1.0) -> list[Candle]:
    return series([start + step * i for i in range(n)])


def as_of_of(bars) -> datetime:
    return bars[-1].ts_close


# ---------------------------------------------------------------------------
# baselines produce valid, comparable results
# ---------------------------------------------------------------------------

def test_persistence_repeats_the_last_close():
    bars = rising(30)
    result = PersistenceForecaster(horizon=3).forecast(
        bars, as_of=as_of_of(bars), instrument_key=KEY, timeframe=TF)
    assert not result.abstained and result.usable
    assert result.mean_close == (bars[-1].close,) * 3
    assert result.expected_return == 0.0
    assert result.direction_probabilities["flat"] == 1.0


def test_single_path_forecast_pins_uncertainty_to_one():
    # The documented rule: one path is one sample, so dispersion is unmeasured
    # and the honest uncertainty is the maximum, not the minimum.
    bars = rising(30)
    result = PersistenceForecaster(horizon=2).forecast(bars, as_of=as_of_of(bars))
    assert result.n_paths == 1
    assert result.uncertainty == 1.0
    assert result.confidence == 0.0
    assert result.forecast_volatility == 0.0
    assert any("uncertainty pinned" in w for w in result.warnings)


def test_baseline_result_is_structurally_complete_and_serialisable():
    bars = rising(40)
    result = DriftForecaster(horizon=4, window=10).forecast(
        bars, as_of=as_of_of(bars), instrument_key=KEY, timeframe=TF)
    assert len(result.mean_close) == result.horizon == 4
    assert len(result.median_close) == 4
    assert len(result.expected_return_path) == 4
    assert len(result.forecast_timestamps) == 4
    # Stamped forward from the last bar's close, one timeframe apart.
    assert result.forecast_timestamps[0] == bars[-1].ts_close + timedelta(hours=1)
    assert result.forecast_timestamps[-1] == bars[-1].ts_close + timedelta(hours=4)
    assert result.input_start == bars[0].ts_open
    assert result.input_end == bars[-1].ts_close
    json.dumps(result.to_dict())          # every result lands in the audit trail


def test_baseline_path_is_ohlc_coherent():
    bars = rising(40)
    path = DriftForecaster(horizon=5, window=10).forecast(
        bars, as_of=as_of_of(bars)).paths[0]
    assert len(path.closes) == len(path.opens) == len(path.highs) == 5
    for i in range(5):
        assert path.highs[i] >= max(path.opens[i], path.closes[i])
        assert path.lows[i] <= min(path.opens[i], path.closes[i])
    assert path.opens[0] == bars[-1].close        # continuous at the join


def test_all_three_baselines_are_directly_comparable():
    bars = rising(60)
    when = as_of_of(bars)
    results = [f.forecast(bars, as_of=when, instrument_key=KEY, timeframe=TF)
               for f in (PersistenceForecaster(horizon=3),
                         DriftForecaster(horizon=3, window=20),
                         SeasonalNaiveForecaster(horizon=3, season=24))]
    assert {r.instrument_key for r in results} == {KEY}
    assert {r.horizon for r in results} == {3}
    assert len({r.forecast_timestamps for r in results}) == 1
    assert len({r.model_version for r in results}) == 3     # distinguishable


def test_drift_extrapolates_the_trend_and_damping_degenerates_to_persistence():
    bars = rising(40)
    when = as_of_of(bars)
    trend = DriftForecaster(horizon=3, window=20).forecast(bars, as_of=when)
    assert trend.mean_close[0] > bars[-1].close
    assert trend.mean_close[2] > trend.mean_close[0]
    assert trend.expected_return > 0

    flatlined = DriftForecaster(horizon=3, window=20, damping=0.0).forecast(
        bars, as_of=when)
    assert flatlined.mean_close == pytest.approx(
        PersistenceForecaster(horizon=3).forecast(bars, as_of=when).mean_close)


def test_seasonal_naive_return_mode_is_continuous_at_the_join():
    bars = rising(40)
    when = as_of_of(bars)
    result = SeasonalNaiveForecaster(horizon=2, season=10).forecast(bars, as_of=when)
    closes = [c.close for c in bars]
    expected = closes[-1] * (closes[-10] / closes[-11])
    assert result.mean_close[0] == pytest.approx(expected)
    # Continuity: the first forecast bar is a small step from the last close,
    # not a teleport back to the level of a season ago.
    assert abs(result.mean_close[0] - closes[-1]) < abs(closes[-10] - closes[-1])


def test_seasonal_naive_level_mode_replays_levels():
    bars = rising(40)
    result = SeasonalNaiveForecaster(horizon=2, season=10, use_returns=False
                                     ).forecast(bars, as_of=as_of_of(bars))
    closes = [c.close for c in bars]
    assert result.mean_close == (closes[-10], closes[-9])


def test_seasonal_naive_wraps_when_horizon_exceeds_the_season():
    bars = rising(40)
    result = SeasonalNaiveForecaster(horizon=5, season=3, use_returns=False
                                     ).forecast(bars, as_of=as_of_of(bars))
    closes = [c.close for c in bars]
    assert result.mean_close == (closes[-3], closes[-2], closes[-1],
                                 closes[-3], closes[-2])


def test_baseline_configuration_is_validated_eagerly():
    with pytest.raises(ConfigurationError):
        PersistenceForecaster(horizon=0)
    with pytest.raises(ConfigurationError):
        DriftForecaster(window=0)
    with pytest.raises(ConfigurationError):
        DriftForecaster(damping=-1.0)
    with pytest.raises(ConfigurationError):
        SeasonalNaiveForecaster(season=0)
    with pytest.raises(ConfigurationError):
        PersistenceForecaster(max_data_age_s=0)


# ---------------------------------------------------------------------------
# abstention rules
# ---------------------------------------------------------------------------

def test_abstains_on_no_candles():
    clock = FixedClock(START)
    result = PersistenceForecaster(clock=clock).forecast([], instrument_key=KEY)
    assert result.abstained and result.failure_code == CODE_INSUFFICIENT
    assert result.failure_reason
    assert not result.usable
    json.dumps(result.to_dict())


def test_abstains_when_the_window_is_shorter_than_required():
    bars = rising(5)
    result = DriftForecaster(window=20).forecast(bars, as_of=as_of_of(bars))
    assert result.abstained and result.failure_code == CODE_INSUFFICIENT


def test_abstains_on_a_bar_that_closes_after_as_of():
    # Look-ahead is the defect that makes a backtest a lie: refuse it loudly
    # rather than silently trimming the window.
    bars = rising(30)
    result = PersistenceForecaster().forecast(bars, as_of=bars[-2].ts_close)
    assert result.abstained and result.failure_code == CODE_LOOKAHEAD


def test_non_final_bars_are_dropped_with_a_warning():
    bars = rising(30)
    bars[-1] = bars[-1].with_final(False)
    result = PersistenceForecaster().forecast(bars, as_of=as_of_of(bars))
    assert not result.abstained
    assert result.mean_close[0] == bars[-2].close      # the settled bar
    assert any("non-final" in w for w in result.warnings)


def test_abstains_when_every_bar_is_unfinished():
    bars = [c.with_final(False) for c in rising(5)]
    result = PersistenceForecaster().forecast(bars, as_of=as_of_of(bars))
    assert result.abstained and result.failure_code == CODE_INSUFFICIENT


def test_abstains_on_out_of_order_or_duplicated_bars():
    bars = rising(10)
    bars[3], bars[4] = bars[4], bars[3]
    result = PersistenceForecaster().forecast(bars, as_of=as_of_of(bars))
    assert result.abstained and result.failure_code == CODE_INVALID


def test_abstains_when_the_window_mixes_instruments():
    bars = rising(10)
    bars[0] = candle(0, 100.0, key="SIM:OTHER")
    result = PersistenceForecaster().forecast(bars, as_of=as_of_of(bars))
    assert result.abstained and result.failure_code == CODE_INVALID


def test_abstains_on_stale_data_when_a_limit_is_configured():
    bars = rising(10)
    late = as_of_of(bars) + timedelta(hours=5)
    result = PersistenceForecaster(max_data_age_s=3600).forecast(bars, as_of=late)
    assert result.abstained and result.failure_code == CODE_STALE
    # Without the limit the same window is perfectly forecastable.
    assert not PersistenceForecaster().forecast(bars, as_of=late).abstained


def test_abstention_carries_model_identity_for_the_audit_trail():
    result = DriftForecaster(window=20).forecast(rising(3), as_of=as_of_of(rising(3)))
    assert result.model_version == "drift-1"
    assert result.inference_params["window"] == 20
    assert result.data_quality is DataQuality.UNUSABLE


# ---------------------------------------------------------------------------
# the service: containment
# ---------------------------------------------------------------------------

class ExplodingForecaster(Forecaster):
    MODEL_VERSION = "boom-1"

    def forecast(self, candles, *, as_of=None, instrument_key="", timeframe=""):
        raise RuntimeError("inference kernel died")


class TypedFailureForecaster(Forecaster):
    MODEL_VERSION = "typed-1"

    def forecast(self, candles, *, as_of=None, instrument_key="", timeframe=""):
        raise ModelNotAvailable("checkpoint not downloaded", repo="acme/kronos")


class LiarForecaster(Forecaster):
    """Returns something that is not a ForecastResult."""
    MODEL_VERSION = "liar-1"

    def forecast(self, candles, *, as_of=None, instrument_key="", timeframe=""):
        return {"mean_close": [1.0]}


class WrongInstrumentForecaster(BaselineForecaster):
    MODEL_VERSION = "wrong-1"

    def _project(self, closes):
        return [closes[-1]] * self.horizon

    def forecast(self, candles, *, as_of=None, instrument_key="", timeframe=""):
        return super().forecast(candles, as_of=as_of,
                                instrument_key="SIM:SOMETHING_ELSE",
                                timeframe=timeframe)


def test_service_turns_an_exploding_forecaster_into_an_abstention():
    bars = rising(10)
    clock = FixedClock(as_of_of(bars))
    service = ForecastService({"boom": ExplodingForecaster()}, clock=clock)
    result = service.run("boom", candles=bars, as_of=as_of_of(bars))
    assert isinstance(result, ForecastResult)
    assert result.abstained
    assert result.failure_code == CODE_UNEXPECTED
    assert "inference kernel died" in result.failure_reason


def test_service_preserves_the_code_of_a_typed_trading_error():
    bars = rising(10)
    service = ForecastService({"typed": TypedFailureForecaster()},
                              clock=FixedClock(as_of_of(bars)))
    result = service.run("typed", candles=bars, as_of=as_of_of(bars))
    assert result.abstained
    assert result.failure_code == ModelNotAvailable.code == "MODEL_UNAVAILABLE"


def test_service_refuses_a_result_that_is_not_a_forecast_result():
    bars = rising(10)
    service = ForecastService({"liar": LiarForecaster()},
                              clock=FixedClock(as_of_of(bars)))
    result = service.run("liar", candles=bars, as_of=as_of_of(bars))
    assert result.abstained and result.failure_code == CODE_CONTRACT


def test_service_refuses_a_result_for_a_different_instrument():
    bars = rising(10)
    service = ForecastService({"wrong": WrongInstrumentForecaster()},
                              clock=FixedClock(as_of_of(bars)))
    result = service.run("wrong", candles=bars, as_of=as_of_of(bars),
                         instrument_key=KEY)
    assert result.abstained and result.failure_code == CODE_CONTRACT


def test_service_never_raises_for_any_forecaster_failure():
    bars = rising(10)
    when = as_of_of(bars)
    service = ForecastService(
        {"boom": ExplodingForecaster(), "typed": TypedFailureForecaster(),
         "liar": LiarForecaster(), "ok": PersistenceForecaster()},
        clock=FixedClock(when))
    results = service.run_all(candles=bars, as_of=when)
    assert list(results) == ["boom", "typed", "liar", "ok"]   # registration order
    assert [r.abstained for r in results.values()] == [True, True, True, False]
    # Isolation: the working forecaster is unaffected by its neighbours.
    assert results["ok"].mean_close == (bars[-1].close,)


def test_service_raises_only_for_a_wiring_mistake():
    service = ForecastService({"ok": PersistenceForecaster()})
    with pytest.raises(ConfigurationError):
        service.run("typo", candles=rising(5))
    with pytest.raises(ConfigurationError):
        service.register("ok", PersistenceForecaster())
    with pytest.raises(ConfigurationError):
        ForecastService({"bad": object()})


def test_service_stamps_latency_and_describes_itself():
    bars = rising(10)
    service = ForecastService({"p": PersistenceForecaster()},
                              clock=FixedClock(as_of_of(bars)))
    result = service.run("p", candles=bars, as_of=as_of_of(bars))
    assert result.latency_ms is not None and result.latency_ms >= 0.0
    described = service.describe()["p"]
    assert described["model_version"] == "persistence-1"
    assert described["params_hash"] == PersistenceForecaster().params_hash()


def test_service_derives_instrument_and_timeframe_from_the_candles():
    bars = rising(10)
    service = ForecastService({"p": PersistenceForecaster()},
                              clock=FixedClock(as_of_of(bars)))
    result = service.run("p", candles=bars, as_of=as_of_of(bars))
    assert result.instrument_key == KEY and result.timeframe == TF


# ---------------------------------------------------------------------------
# the service: audit
# ---------------------------------------------------------------------------

class RecordingAudit:
    def __init__(self):
        self.records = []

    def record(self, event_type, payload, *, actor="", subject="",
               correlation_id=""):
        self.records.append((event_type, actor, subject, payload))


class BrokenAudit:
    def record(self, *args, **kwargs):
        raise RuntimeError("audit sink is down")


def test_service_records_every_forecast_including_abstentions():
    bars = rising(10)
    audit = RecordingAudit()
    service = ForecastService({"p": PersistenceForecaster(),
                               "boom": ExplodingForecaster()},
                              clock=FixedClock(as_of_of(bars)), audit=audit)
    service.run_all(candles=bars, as_of=as_of_of(bars))
    assert [r[0] for r in audit.records] == ["FORECAST_PRODUCED"] * 2
    assert [r[1] for r in audit.records] == ["p", "boom"]
    assert audit.records[0][2] == KEY
    json.dumps(audit.records[0][3])


def test_a_broken_audit_sink_cannot_stop_the_loop():
    bars = rising(10)
    service = ForecastService({"p": PersistenceForecaster()},
                              clock=FixedClock(as_of_of(bars)), audit=BrokenAudit())
    result = service.run("p", candles=bars, as_of=as_of_of(bars))
    assert not result.abstained
    assert service.audit_failures == 1


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def test_params_hash_is_order_independent_and_change_sensitive():
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})
    assert params_hash({"a": 1}) != params_hash({"a": 2})


def test_cache_key_includes_every_component():
    base = dict(instrument_key=KEY, timeframe=TF, as_of=START,
                model_version="v1", params_hash="deadbeef")
    key = ForecastCache.key(**base)
    for field, other in (("instrument_key", "SIM:XYZ"), ("timeframe", "5m"),
                         ("as_of", START + timedelta(hours=1)),
                         ("model_version", "v2"), ("params_hash", "cafebabe")):
        assert ForecastCache.key(**{**base, field: other}) != key


def test_cache_hit_returns_the_same_result_without_rerunning():
    bars = rising(20)
    when = as_of_of(bars)
    clock = FixedClock(when)
    cache = ForecastCache(ttl_s=300, clock=clock)
    service = ForecastService({"p": PersistenceForecaster()}, clock=clock,
                              cache=cache)
    first = service.run("p", candles=bars, as_of=when)
    second = service.run("p", candles=bars, as_of=when)
    assert second is first
    assert cache.stats()["hits"] == 1 and len(cache) == 1


def test_cache_distinguishes_different_sampling_parameters():
    # The whole point of the params hash: two forecasters of the same family,
    # same as_of, different settings must never share a cache entry.
    bars = rising(40)
    when = as_of_of(bars)
    clock = FixedClock(when)
    cache = ForecastCache(ttl_s=300, clock=clock)
    service = ForecastService({"a": DriftForecaster(horizon=2, window=20),
                               "b": DriftForecaster(horizon=2, window=20,
                                                    damping=0.5)},
                              clock=clock, cache=cache)
    results = service.run_all(candles=bars, as_of=when)
    assert len(cache) == 2
    assert results["a"].mean_close != results["b"].mean_close
    # And the second run of each is served from its own entry, not the other's.
    again = service.run_all(candles=bars, as_of=when)
    assert again["a"] is results["a"] and again["b"] is results["b"]


def test_cache_respects_its_ttl_against_the_injected_clock():
    bars = rising(20)
    when = as_of_of(bars)
    clock = FixedClock(when)
    cache = ForecastCache(ttl_s=60, clock=clock)
    key = ForecastCache.key(instrument_key=KEY, timeframe=TF, as_of=when,
                            model_version="persistence-1", params_hash="x")
    result = PersistenceForecaster().forecast(bars, as_of=when)
    cache.put(key, result)
    assert key in cache
    clock.advance(timedelta(seconds=59))
    assert cache.get(key) is result
    clock.advance(timedelta(seconds=2))
    assert cache.get(key) is None
    assert key not in cache
    assert len(cache) == 0                    # the expired entry is evicted


def test_cache_refuses_to_store_an_abstention():
    clock = FixedClock(START)
    cache = ForecastCache(ttl_s=60, clock=clock)
    abstention = PersistenceForecaster(clock=clock).forecast([], instrument_key=KEY)
    cache.put("k", abstention)
    assert len(cache) == 0
    # …and the service therefore retries rather than serving the refusal.
    service = ForecastService({"boom": ExplodingForecaster()}, clock=clock,
                              cache=cache)
    bars = rising(5)
    service.run("boom", candles=bars, as_of=as_of_of(bars))
    assert len(cache) == 0


def test_cache_evicts_oldest_first_and_deterministically():
    clock = FixedClock(START)
    cache = ForecastCache(ttl_s=600, clock=clock, max_entries=2)
    bars = rising(20)
    result = PersistenceForecaster().forecast(bars, as_of=as_of_of(bars))
    for name in ("k1", "k2", "k3"):
        cache.put(name, result)
    assert "k1" not in cache and "k2" in cache and "k3" in cache
    assert cache.stats()["evictions"] == 1


def test_cache_purge_and_clear():
    clock = FixedClock(START)
    cache = ForecastCache(ttl_s=30, clock=clock)
    bars = rising(20)
    result = PersistenceForecaster().forecast(bars, as_of=as_of_of(bars))
    cache.put("a", result)
    clock.advance(timedelta(seconds=31))
    cache.put("b", result)
    assert cache.purge_expired() == 1 and len(cache) == 1
    cache.clear()
    assert len(cache) == 0


def test_cache_configuration_is_validated():
    with pytest.raises(ConfigurationError):
        ForecastCache(ttl_s=0)
    with pytest.raises(ConfigurationError):
        ForecastCache(max_entries=0)


def test_an_undescribable_forecaster_simply_is_not_cached():
    class Minimal:
        """Duck-typed forecaster with no model_version/params at all."""

        def forecast(self, candles, *, as_of=None, instrument_key="",
                     timeframe=""):
            return PersistenceForecaster().forecast(
                candles, as_of=as_of, instrument_key=instrument_key,
                timeframe=timeframe)

    bars = rising(10)
    when = as_of_of(bars)
    clock = FixedClock(when)
    cache = ForecastCache(ttl_s=60, clock=clock)
    service = ForecastService({"m": Minimal()}, clock=clock, cache=cache)
    first = service.run("m", candles=bars, as_of=when)
    second = service.run("m", candles=bars, as_of=when)
    assert not first.abstained and first is not second       # ran twice
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def test_identical_inputs_produce_identical_results():
    bars = rising(60)
    when = as_of_of(bars)
    def run():
        service = ForecastService(
            {"p": PersistenceForecaster(horizon=3),
             "d": DriftForecaster(horizon=3, window=20),
             "s": SeasonalNaiveForecaster(horizon=3, season=24)},
            clock=FixedClock(when))
        return {k: v.to_dict() for k, v in
                service.run_all(candles=bars, as_of=when).items()}
    assert run() == run()


def test_forecaster_rejects_an_unsupported_interface_at_construction():
    class NotAForecaster:
        forecast = "not callable"

    with pytest.raises(ConfigurationError):
        ForecastService({"x": NotAForecaster()})
    with pytest.raises(ConfigurationError):
        ForecastService({"": PersistenceForecaster()})


def test_trading_error_subclasses_carry_stable_codes():
    # Guards the abstention codes this module reuses from the taxonomy.
    assert issubclass(ModelNotAvailable, TradingError)
    assert CODE_INSUFFICIENT == "DATA_INSUFFICIENT"
    assert CODE_INVALID == "DATA_VALIDATION_FAILED"
    assert CODE_STALE == "DATA_STALE"
