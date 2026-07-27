"""Tests for `olympus.trading.signals`.

Two properties dominate: the difference between *silence* (no opinion) and
*FLAT* (a considered "no direction"), and the guarantee that fusion is
deterministic and never manufactures a direction no source held.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import (Candle, DataQuality, ForecastPath,
                                       ForecastResult, Regime, Signal,
                                       SignalDirection)
from olympus.trading.errors import ConfigurationError, DataValidationError
from olympus.trading.signals import (FusedSignal, KronosSignalGenerator,
                                     MajorityVoteFusion, RegimeSignalGenerator,
                                     SignalContext, TASignalGenerator,
                                     UnanimousFusion, VolatilitySignalGenerator,
                                     WeightedAverageFusion)

START = datetime(2026, 3, 2, tzinfo=timezone.utc)
NOW = START + timedelta(days=5)
KEY = "SIM:ABC"
TF = "1h"
LONG, SHORT, FLAT = (SignalDirection.LONG, SignalDirection.SHORT,
                     SignalDirection.FLAT)


def series(prices, *, key: str = KEY) -> list[Candle]:
    out = []
    for i, price in enumerate(prices):
        out.append(Candle(instrument_key=key, timeframe=TF,
                          ts_open=START + timedelta(hours=i),
                          ts_close=START + timedelta(hours=i + 1),
                          open=price, high=price * 1.02, low=price * 0.98,
                          close=price, volume=100.0))
    return out


def forecast(*, expected_return: float = 0.02, vol: float = 0.01,
             realised: float = 0.01, uncertainty: float = 0.2,
             quality: DataQuality = DataQuality.OK, abstained: bool = False,
             key: str = KEY, horizon: int = 3) -> ForecastResult:
    if abstained:
        return ForecastResult.abstain(
            instrument_key=key, timeframe=TF, input_start=START, input_end=NOW,
            created_at=NOW, horizon=horizon, reason="bad data",
            code="DATA_INSUFFICIENT", model_version="kronos-test")
    last = 100.0
    closes = tuple(last * (1 + expected_return * (i + 1) / horizon)
                   for i in range(horizon))
    return ForecastResult(
        instrument_key=key, timeframe=TF, input_start=START, input_end=NOW,
        created_at=NOW, horizon=horizon,
        paths=(ForecastPath(closes=closes),), mean_close=closes,
        median_close=closes, expected_return=expected_return,
        expected_return_path=tuple(c / last - 1 for c in closes),
        forecast_volatility=vol, realised_volatility=realised,
        uncertainty=uncertainty, data_quality=quality,
        model_version="kronos-test")


def signal(source: str, direction: SignalDirection, strength: float,
           confidence: float = 1.0, *, ts: datetime | None = None,
           key: str = KEY) -> Signal:
    return Signal(source=source, instrument_key=key, direction=direction,
                  strength=strength, confidence=confidence, ts=ts or NOW)


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

def test_context_coerces_the_pipelines_keyword_call():
    bars = series([100.0, 101.0])
    ctx = SignalContext.coerce(None, default_as_of=NOW, instrument=None,
                               timeframe=TF, candles=bars, forecasts={},
                               quote=None, portfolio="ignored-but-kept")
    assert ctx.instrument_key == KEY and ctx.timeframe == TF
    assert ctx.as_of == NOW
    # An unknown keyword is preserved rather than fatal: the pipeline may grow
    # arguments, and a generator refusing to run over one is the worse failure.
    assert ctx.extra["portfolio"] == "ignored-but-kept"


def test_context_accepts_a_mapping_an_object_and_itself():
    bars = series([100.0])
    from types import SimpleNamespace
    built = SignalContext(as_of=NOW, candles=tuple(bars))
    assert SignalContext.coerce(built) is built
    assert SignalContext.coerce({"as_of": NOW, "candles": bars}).instrument_key == KEY
    ns = SimpleNamespace(as_of=NOW, candles=bars, forecasts={}, quote=None)
    assert SignalContext.coerce(ns).instrument_key == KEY


def test_context_refuses_to_invent_a_time():
    with pytest.raises(ConfigurationError):
        SignalContext.coerce(None, candles=series([100.0]))


def test_context_to_dict_summarises_rather_than_dumps():
    ctx = SignalContext(as_of=NOW, candles=tuple(series([100.0] * 5)),
                        forecasts={"kronos": forecast()})
    payload = ctx.to_dict()
    assert payload["bars"] == 5 and payload["forecasts"] == ["kronos"]
    json.dumps(payload)


# ---------------------------------------------------------------------------
# Kronos signal generator
# ---------------------------------------------------------------------------

def make_ctx(**forecasts) -> SignalContext:
    return SignalContext(as_of=NOW, instrument_key=KEY, timeframe=TF,
                         candles=tuple(series([100.0] * 5)), forecasts=forecasts)


def test_abstained_forecast_produces_no_signal_at_all():
    # Not a FLAT: a FLAT would enter the vote and dilute the sources that do
    # have an opinion.
    gen = KronosSignalGenerator()
    assert gen.generate(make_ctx(kronos=forecast(abstained=True))) == []


def test_missing_forecast_produces_no_signal():
    assert KronosSignalGenerator().generate(make_ctx()) == []
    assert KronosSignalGenerator().generate(make_ctx(other=forecast())) == []


def test_unusable_data_quality_produces_no_signal():
    gen = KronosSignalGenerator()
    assert gen.generate(make_ctx(kronos=forecast(quality=DataQuality.UNUSABLE))) == []
    strict = KronosSignalGenerator(min_data_quality=DataQuality.OK)
    assert strict.generate(make_ctx(kronos=forecast(quality=DataQuality.DEGRADED))) == []


def test_uncertainty_gate_produces_no_signal():
    gen = KronosSignalGenerator(max_uncertainty=0.5)
    assert gen.generate(make_ctx(kronos=forecast(uncertainty=0.9))) == []
    assert gen.generate(make_ctx(kronos=forecast(uncertainty=0.4)))


def test_return_inside_the_noise_band_emits_a_flat_signal():
    # The generator looked and saw nothing: that is information, unlike silence.
    gen = KronosSignalGenerator(min_expected_return=0.01)
    produced = gen.generate(make_ctx(kronos=forecast(expected_return=0.001)))
    assert len(produced) == 1
    assert produced[0].direction is FLAT and produced[0].strength == 0.0


def test_direction_follows_the_threshold_in_both_directions():
    gen = KronosSignalGenerator(min_expected_return=0.005)
    up = gen.generate(make_ctx(kronos=forecast(expected_return=0.02)))[0]
    down = gen.generate(make_ctx(kronos=forecast(expected_return=-0.02)))[0]
    assert up.direction is LONG and up.strength > 0
    assert down.direction is SHORT and down.strength < 0
    assert up.strength == pytest.approx(-down.strength)


def test_strength_is_a_clamped_t_statistic():
    gen = KronosSignalGenerator(min_expected_return=0.0, t_scale=2.0)
    # t = 0.02 / 0.02 = 1.0 → strength 1.0/2.0 = 0.5
    half = gen.generate(make_ctx(kronos=forecast(expected_return=0.02, vol=0.02)))[0]
    assert half.strength == pytest.approx(0.5)
    # t = 0.2 / 0.001 = 200 → clamped, never out of contract range
    hot = gen.generate(make_ctx(kronos=forecast(expected_return=0.2, vol=0.001)))[0]
    assert hot.strength == 1.0
    cold = gen.generate(make_ctx(kronos=forecast(expected_return=-0.2, vol=0.001)))[0]
    assert cold.strength == -1.0


def test_realised_volatility_is_the_fallback_dispersion():
    gen = KronosSignalGenerator(min_expected_return=0.0, t_scale=2.0)
    produced = gen.generate(make_ctx(
        kronos=forecast(expected_return=0.02, vol=0.0, realised=0.02)))[0]
    assert produced.features["dispersion_basis"] == "realised_volatility"
    assert produced.strength == pytest.approx(0.5)


def test_no_dispersion_estimate_yields_a_deliberately_weak_conviction():
    gen = KronosSignalGenerator(min_expected_return=0.0, undispersed_strength=0.25)
    produced = gen.generate(make_ctx(
        kronos=forecast(expected_return=0.05, vol=0.0, realised=0.0)))[0]
    assert produced.direction is LONG
    assert produced.strength == pytest.approx(0.25)
    assert produced.features["dispersion_basis"] == "none"
    assert produced.features["t_stat"] is None


def test_confidence_is_the_forecasts_own_and_is_gated_by_data_quality():
    gen = KronosSignalGenerator(degraded_confidence_factor=0.5)
    clean = gen.generate(make_ctx(kronos=forecast(uncertainty=0.2)))[0]
    assert clean.confidence == pytest.approx(0.8)
    degraded = gen.generate(make_ctx(
        kronos=forecast(uncertainty=0.2, quality=DataQuality.DEGRADED)))[0]
    assert degraded.confidence == pytest.approx(0.4)
    assert degraded.data_quality is DataQuality.DEGRADED


def test_min_confidence_gate_produces_silence():
    gen = KronosSignalGenerator(min_confidence=0.9)
    assert gen.generate(make_ctx(kronos=forecast(uncertainty=0.5))) == []


def test_a_wiring_mistake_is_loud():
    gen = KronosSignalGenerator()
    with pytest.raises(ConfigurationError):
        gen.generate(make_ctx(kronos="not a forecast"))
    with pytest.raises(ConfigurationError):
        gen.generate(make_ctx(kronos=forecast(key="SIM:OTHER")))


def test_kronos_generator_configuration_is_validated():
    with pytest.raises(ConfigurationError):
        KronosSignalGenerator(t_scale=0.0)
    with pytest.raises(ConfigurationError):
        KronosSignalGenerator(max_uncertainty=1.5)
    with pytest.raises(ConfigurationError):
        KronosSignalGenerator(min_expected_return=-0.01)


# ---------------------------------------------------------------------------
# TA / regime / volatility generators
# ---------------------------------------------------------------------------

def test_ta_generator_is_silent_without_enough_history():
    gen = TASignalGenerator(rules=("ma_cross", "breakout"))
    assert gen.generate(candles=series([100.0] * 5), as_of=NOW) == []


def test_ma_cross_follows_the_trend_and_confirms_over_time():
    gen = TASignalGenerator(rules=("ma_cross",), confirm_bars=3)
    up = gen.generate(candles=series([100.0 + i for i in range(60)]), as_of=NOW)[0]
    down = gen.generate(candles=series([160.0 - i for i in range(60)]), as_of=NOW)[0]
    assert up.source == "ta.ma_cross" and up.direction is LONG
    assert down.direction is SHORT
    assert -1.0 <= down.strength <= 0.0
    assert up.features["bars_since_cross"] >= 3
    assert up.confidence == pytest.approx(0.5)      # the configured prior, saturated


def test_rsi_reversion_emits_flat_inside_the_band():
    gen = TASignalGenerator(rules=("rsi_reversion",))
    falling = gen.generate(candles=series([100.0 - i for i in range(40)]), as_of=NOW)[0]
    assert falling.direction is LONG and falling.strength > 0
    rising = gen.generate(candles=series([100.0 + i for i in range(40)]), as_of=NOW)[0]
    assert rising.direction is SHORT and rising.strength < 0
    # A flat market is neither overbought nor oversold (ta.rsi returns 50).
    quiet = gen.generate(candles=series([100.0] * 40), as_of=NOW)[0]
    assert quiet.direction is FLAT and quiet.strength == 0.0


def test_breakout_measures_the_break_in_atr_units():
    prices = [100.0] * 30 + [130.0]
    produced = TASignalGenerator(rules=("breakout",)).generate(
        candles=series(prices), as_of=NOW)[0]
    assert produced.direction is LONG and produced.strength > 0
    inside = TASignalGenerator(rules=("breakout",)).generate(
        candles=series([100.0] * 31), as_of=NOW)[0]
    assert inside.direction is FLAT


def test_ta_generator_configuration_is_validated():
    with pytest.raises(ConfigurationError):
        TASignalGenerator(rules=("nonsense",))
    with pytest.raises(ConfigurationError):
        TASignalGenerator(rules=())
    with pytest.raises(ConfigurationError):
        TASignalGenerator(fast=30, slow=10)
    with pytest.raises(ConfigurationError):
        TASignalGenerator(oversold=80.0, overbought=20.0)


class FakeRegimeState:
    def __init__(self, regime, confidence):
        self.regime = regime
        self.confidence = confidence


def test_regime_generator_maps_states_conservatively():
    gen = RegimeSignalGenerator()
    up = gen.generate(regime=FakeRegimeState(Regime.TRENDING_UP, 0.8),
                      as_of=NOW, instrument_key=KEY)[0]
    assert up.direction is LONG and up.strength == pytest.approx(0.8)
    down = gen.generate(regime=FakeRegimeState(Regime.TRENDING_DOWN, 0.6),
                        as_of=NOW, instrument_key=KEY)[0]
    assert down.direction is SHORT and down.strength == pytest.approx(-0.6)
    for regime in (Regime.RANGING, Regime.HIGH_VOLATILITY, Regime.CRISIS):
        state = gen.generate(regime=FakeRegimeState(regime, 0.9), as_of=NOW,
                             instrument_key=KEY)[0]
        assert state.direction is FLAT and state.strength == 0.0


def test_unknown_regime_is_silence_not_a_flat_opinion():
    gen = RegimeSignalGenerator()
    assert gen.generate(regime=FakeRegimeState(Regime.UNKNOWN, 0.0), as_of=NOW,
                        instrument_key=KEY) == []
    assert gen.generate(as_of=NOW, instrument_key=KEY) == []


def test_regime_generator_accepts_a_bare_enum_or_string():
    gen = RegimeSignalGenerator()
    assert gen.generate(regime=Regime.TRENDING_UP, as_of=NOW,
                        instrument_key=KEY)[0].direction is LONG
    assert gen.generate(regime="trending_down", as_of=NOW,
                        instrument_key=KEY)[0].direction is SHORT


class FakeVolState:
    def __init__(self, rank, label="extreme"):
        self.percentile_rank = rank
        self.regime_label = label
        self.realised = 0.42


def test_volatility_generator_is_silent_until_the_extreme():
    gen = VolatilitySignalGenerator(extreme_rank=0.95)
    assert gen.generate(volatility=FakeVolState(0.5), as_of=NOW,
                        instrument_key=KEY) == []
    produced = gen.generate(volatility=FakeVolState(0.98), as_of=NOW,
                            instrument_key=KEY)[0]
    assert produced.direction is FLAT and produced.strength == 0.0
    assert produced.confidence == pytest.approx(0.98)


def test_volatility_generator_configuration_is_validated():
    with pytest.raises(ConfigurationError):
        VolatilitySignalGenerator(extreme_rank=1.0)


# ---------------------------------------------------------------------------
# fusion: screening
# ---------------------------------------------------------------------------

def test_empty_input_fuses_to_flat_and_invents_nothing():
    fused = WeightedAverageFusion().fuse([], as_of=NOW)
    assert fused.direction is FLAT and fused.strength == 0.0
    assert fused.confidence == 0.0 and fused.agreement == 0.0
    assert fused.sources == () and not fused.is_actionable


def test_all_flat_inputs_fuse_to_flat_with_full_agreement():
    fused = WeightedAverageFusion().fuse(
        [signal("a", FLAT, 0.0), signal("b", FLAT, 0.0)], as_of=NOW)
    assert fused.direction is FLAT and fused.strength == 0.0
    assert fused.agreement == 1.0             # unanimously no view


def test_stale_signals_are_dropped_and_recorded():
    fusion = WeightedAverageFusion(max_age_s=3600)
    fresh = signal("fresh", LONG, 0.8, ts=NOW - timedelta(minutes=10))
    old = signal("old", SHORT, -0.9, ts=NOW - timedelta(hours=5))
    fused = fusion.fuse([fresh, old], as_of=NOW)
    assert fused.sources == ("fresh",)
    assert "old:stale" in fused.dropped
    assert fused.direction is LONG            # the stale dissent cannot vote


def test_signals_from_the_future_are_dropped():
    fusion = WeightedAverageFusion()
    fused = fusion.fuse([signal("now", LONG, 0.5),
                         signal("leak", SHORT, -1.0,
                                ts=NOW + timedelta(hours=1))], as_of=NOW)
    assert fused.sources == ("now",)
    assert "leak:from-the-future" in fused.dropped


def test_foreign_instruments_are_dropped():
    fusion = WeightedAverageFusion()
    fused = fusion.fuse([signal("mine", LONG, 0.5),
                         signal("theirs", SHORT, -1.0, key="SIM:OTHER")],
                        as_of=NOW, instrument_key=KEY)
    assert fused.sources == ("mine",)
    assert "theirs:foreign-instrument" in fused.dropped


def test_a_source_votes_only_once_and_the_newest_wins():
    older = signal("dup", SHORT, -0.9, ts=NOW - timedelta(hours=2))
    newer = signal("dup", LONG, 0.4, ts=NOW - timedelta(minutes=1))
    fused = WeightedAverageFusion().fuse([older, newer], as_of=NOW)
    assert fused.sources == ("dup",)
    assert fused.direction is LONG
    assert "dup:superseded" in fused.dropped


def test_non_signals_are_dropped_rather_than_crashing_the_loop():
    fused = WeightedAverageFusion().fuse(["nonsense", signal("a", LONG, 0.5)],
                                         as_of=NOW)
    assert fused.sources == ("a",)
    assert any("not-a-signal" in d for d in fused.dropped)


def test_min_confidence_screen():
    fusion = WeightedAverageFusion(min_confidence=0.5)
    fused = fusion.fuse([signal("sure", LONG, 0.6, 0.9),
                         signal("unsure", SHORT, -1.0, 0.1)], as_of=NOW)
    assert fused.sources == ("sure",)
    assert "unsure:below-min-confidence" in fused.dropped


# ---------------------------------------------------------------------------
# fusion: weighted average
# ---------------------------------------------------------------------------

def test_weights_are_normalised_over_the_present_sources():
    fusion = WeightedAverageFusion(weights={"a": 3.0, "b": 1.0})
    fused = fusion.fuse([signal("a", LONG, 1.0), signal("b", LONG, 1.0)],
                        as_of=NOW)
    assert fused.contributions == {"a": pytest.approx(0.75),
                                   "b": pytest.approx(0.25)}
    assert fused.strength == pytest.approx(1.0)
    assert sum(fused.contributions.values()) == pytest.approx(fused.strength)


def test_a_missing_source_does_not_dilute_the_present_ones():
    fusion = WeightedAverageFusion(weights={"a": 3.0, "b": 1.0})
    fused = fusion.fuse([signal("a", LONG, 0.6)], as_of=NOW)
    assert fused.contributions["a"] == pytest.approx(0.6)
    assert fused.strength == pytest.approx(0.6)


def test_conflicting_sources_reduce_both_strength_and_agreement():
    fusion = WeightedAverageFusion()
    aligned = fusion.fuse([signal("a", LONG, 0.8), signal("b", LONG, 0.8)],
                          as_of=NOW)
    conflicted = fusion.fuse([signal("a", LONG, 0.8), signal("b", SHORT, -0.8)],
                             as_of=NOW)
    assert aligned.strength == pytest.approx(0.8) and aligned.agreement == 1.0
    assert conflicted.direction is FLAT and conflicted.strength == 0.0
    assert conflicted.agreement == pytest.approx(0.0)
    partial = fusion.fuse([signal("a", LONG, 0.9), signal("b", LONG, 0.9),
                           signal("c", SHORT, -0.9)], as_of=NOW)
    assert partial.direction is LONG
    assert 0.0 < partial.strength < 0.9
    assert partial.agreement == pytest.approx(1 / 3)


def test_one_confident_dissenter_costs_more_agreement_than_a_hesitant_one():
    fusion = WeightedAverageFusion()
    hesitant = fusion.fuse([signal("a", LONG, 0.9, 1.0),
                            signal("b", SHORT, -0.9, 0.1)], as_of=NOW)
    confident = fusion.fuse([signal("a", LONG, 0.9, 1.0),
                             signal("b", SHORT, -0.9, 1.0)], as_of=NOW)
    assert hesitant.agreement > confident.agreement
    assert hesitant.direction is LONG and confident.direction is FLAT


def test_flat_sources_damp_strength_without_costing_agreement():
    fusion = WeightedAverageFusion()
    alone = fusion.fuse([signal("a", LONG, 0.8)], as_of=NOW)
    damped = fusion.fuse([signal("a", LONG, 0.8), signal("v", FLAT, 0.0)],
                         as_of=NOW)
    assert damped.strength < alone.strength
    assert damped.agreement == 1.0            # a FLAT abstains from the vote


def test_confidence_is_the_weighted_mean_not_the_agreement():
    fusion = WeightedAverageFusion()
    fused = fusion.fuse([signal("a", LONG, 1.0, 0.4), signal("b", LONG, 1.0, 0.8)],
                        as_of=NOW)
    assert fused.confidence == pytest.approx(0.6)
    assert fused.agreement == 1.0


def test_sources_with_no_confidence_move_nothing():
    # Exactly the case of the deterministic baselines: they may state a
    # direction but carry zero confidence, so fusion stays FLAT.
    fused = WeightedAverageFusion().fuse(
        [signal("baseline", LONG, 1.0, 0.0)], as_of=NOW)
    assert fused.direction is FLAT and fused.strength == 0.0
    assert fused.confidence == 0.0


def test_negative_weights_are_refused():
    with pytest.raises(ConfigurationError):
        WeightedAverageFusion(weights={"a": -1.0})
    with pytest.raises(ConfigurationError):
        WeightedAverageFusion(max_age_s=0)


def test_weight_prefix_matching_lets_a_family_be_weighted_at_once():
    fusion = WeightedAverageFusion(weights={"ta.": 0.5, "ta.rsi": 2.0},
                                   default_weight=1.0)
    assert fusion.weight_for("ta.ma_cross") == 0.5
    assert fusion.weight_for("ta.rsi") == 2.0        # exact beats prefix
    assert fusion.weight_for("kronos") == 1.0


# ---------------------------------------------------------------------------
# fusion: majority vote
# ---------------------------------------------------------------------------

def test_majority_vote_follows_the_side_with_more_weight():
    fusion = MajorityVoteFusion()
    fused = fusion.fuse([signal("a", LONG, 0.4), signal("b", LONG, 0.4),
                         signal("c", SHORT, -1.0)], as_of=NOW)
    assert fused.direction is LONG            # magnitude does not buy the vote
    assert fused.strength > 0


def test_majority_vote_ties_to_flat():
    fused = MajorityVoteFusion().fuse(
        [signal("a", LONG, 0.9), signal("b", SHORT, -0.9)], as_of=NOW)
    assert fused.direction is FLAT and fused.strength == 0.0


def test_majority_vote_min_margin_gate():
    signals = [signal("a", LONG, 0.9), signal("b", LONG, 0.9),
               signal("c", SHORT, -0.9)]
    assert MajorityVoteFusion().fuse(signals, as_of=NOW).direction is LONG
    strict = MajorityVoteFusion(min_margin=0.9).fuse(signals, as_of=NOW)
    assert strict.direction is FLAT and "margin" in strict.rationale


def test_majority_vote_flat_voters_reduce_the_margin():
    with_flat = MajorityVoteFusion().fuse(
        [signal("a", LONG, 0.8), signal("v", FLAT, 0.0)], as_of=NOW)
    without = MajorityVoteFusion().fuse([signal("a", LONG, 0.8)], as_of=NOW)
    assert with_flat.strength < without.strength


def test_majority_vote_with_no_confidence_anywhere_is_flat():
    fused = MajorityVoteFusion().fuse(
        [signal("a", LONG, 1.0, 0.0), signal("b", LONG, 1.0, 0.0)], as_of=NOW)
    assert fused.direction is FLAT


# ---------------------------------------------------------------------------
# fusion: unanimous
# ---------------------------------------------------------------------------

def test_unanimous_agreement_is_held_to_the_weakest_member():
    fused = UnanimousFusion().fuse(
        [signal("a", LONG, 0.9, 0.9), signal("b", LONG, 0.3, 0.4)], as_of=NOW)
    assert fused.direction is LONG
    assert fused.strength == pytest.approx(0.3)
    assert fused.confidence == pytest.approx(0.4)


def test_unanimous_returns_flat_on_any_disagreement():
    fused = UnanimousFusion().fuse(
        [signal("a", LONG, 0.9), signal("b", SHORT, -0.1)], as_of=NOW)
    assert fused.direction is FLAT and fused.strength == 0.0
    assert fused.confidence == 0.0
    assert "disagree" in fused.rationale


def test_unanimous_treats_a_flat_as_dissent_by_default():
    signals = [signal("a", LONG, 0.9), signal("b", LONG, 0.8),
               signal("v", FLAT, 0.0)]
    assert UnanimousFusion().fuse(signals, as_of=NOW).direction is FLAT
    lenient = UnanimousFusion(treat_flat_as_dissent=False).fuse(signals, as_of=NOW)
    assert lenient.direction is LONG


def test_unanimity_of_one_is_not_unanimity():
    single = [signal("a", LONG, 0.9)]
    assert UnanimousFusion().fuse(single, as_of=NOW).direction is FLAT
    assert UnanimousFusion(min_sources=1).fuse(single, as_of=NOW).direction is LONG


# ---------------------------------------------------------------------------
# fused signal contract + determinism
# ---------------------------------------------------------------------------

def test_fused_signal_cannot_smuggle_conviction_through_a_flat():
    with pytest.raises(DataValidationError):
        FusedSignal(instrument_key=KEY, direction=FLAT, strength=0.4,
                    confidence=0.5, ts=NOW)
    with pytest.raises(DataValidationError):
        FusedSignal(instrument_key=KEY, direction=LONG, strength=-0.4,
                    confidence=0.5, ts=NOW)
    with pytest.raises(DataValidationError):
        FusedSignal(instrument_key=KEY, direction=LONG, strength=1.4,
                    confidence=0.5, ts=NOW)
    with pytest.raises(DataValidationError):
        FusedSignal(instrument_key=KEY, direction=LONG, strength=0.4,
                    confidence=0.5, ts=NOW, agreement=2.0)


def test_fused_signal_is_json_serialisable_for_the_audit_trail():
    fused = WeightedAverageFusion().fuse(
        [signal("a", LONG, 0.5), signal("b", FLAT, 0.0)], as_of=NOW)
    payload = fused.to_dict()
    json.dumps(payload)
    assert payload["direction"] == "long"
    assert payload["ts"].endswith("+00:00")


@pytest.mark.parametrize("fusion_cls", [WeightedAverageFusion,
                                        MajorityVoteFusion, UnanimousFusion])
def test_fusion_is_order_independent_to_the_last_bit(fusion_cls):
    signals = [signal("kronos", LONG, 0.7, 0.9),
               signal("ta.ma_cross", LONG, 0.4, 0.5),
               signal("ta.rsi_reversion", SHORT, -0.2, 0.5),
               signal("regime", LONG, 0.6, 0.8)]
    fusion = fusion_cls(weights={"kronos": 2.0, "ta.": 0.5})
    reference = fusion.fuse(signals, as_of=NOW).to_dict()
    shuffler = random.Random(20260327)
    for _ in range(10):
        shuffled = list(signals)
        shuffler.shuffle(shuffled)
        assert fusion.fuse(shuffled, as_of=NOW).to_dict() == reference


def test_fusion_uses_the_injected_clock_when_no_as_of_is_given():
    clock = FixedClock(NOW)
    fusion = WeightedAverageFusion(clock=clock, max_age_s=60)
    fresh = signal("a", LONG, 0.5, ts=NOW - timedelta(seconds=30))
    assert fusion.fuse([fresh]).direction is LONG
    clock.advance(timedelta(minutes=5))
    assert fusion.fuse([fresh]).direction is FLAT      # now stale


# ---------------------------------------------------------------------------
# end to end through the pipeline's calling convention
# ---------------------------------------------------------------------------

def test_generators_and_fusion_compose_under_the_pipeline_call_shape():
    bars = series([100.0 + i for i in range(60)])
    when = bars[-1].ts_close
    generators = [KronosSignalGenerator(min_expected_return=0.001),
                  TASignalGenerator(rules=("ma_cross", "rsi_reversion")),
                  RegimeSignalGenerator(),
                  VolatilitySignalGenerator()]
    produced = []
    for gen in generators:
        produced.extend(gen.generate(
            instrument=None, timeframe=TF, candles=bars,
            forecasts={"kronos": forecast(expected_return=0.03, vol=0.02)},
            as_of=when, quote=None))
    sources = {s.source for s in produced}
    assert sources == {"kronos", "ta.ma_cross", "ta.rsi_reversion"}
    fused = WeightedAverageFusion(weights={"kronos": 2.0, "ta.": 1.0}).fuse(
        produced, as_of=when)
    assert fused.instrument_key == KEY
    assert set(fused.sources) == sources
    assert -1.0 <= fused.strength <= 1.0
    json.dumps(fused.to_dict())
