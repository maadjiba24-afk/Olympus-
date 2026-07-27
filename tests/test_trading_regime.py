"""Tests for olympus.trading.regime.

Two things are being pinned here. First, that synthetic markets with an
unambiguous character get the label a human would give them. Second — and more
important for a live system — that the classifier is *stable*: it does not flap
bar to bar, it is idempotent on a repeated bar, and it refuses to guess when the
history is too short.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from olympus.trading.clock import FixedClock
from olympus.trading.contracts import Candle, Regime
from olympus.trading.errors import ConfigurationError, DataValidationError, InsufficientDataError
from olympus.trading import regime as R

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
KEY = "SIM:BTCUSD"
CLOCK = FixedClock(START)


def from_closes(closes, timeframe="1h", key=KEY, wick=0.001):
    """Candles tracing a close path, with small symmetric wicks."""
    step = timedelta(seconds=3600)
    out = []
    prev = closes[0]
    for i, close in enumerate(closes):
        out.append(Candle(
            instrument_key=key, timeframe=timeframe,
            ts_open=START + i * step, ts_close=START + (i + 1) * step,
            open=prev, high=max(prev, close) * (1 + wick),
            low=min(prev, close) * (1 - wick), close=close, volume=1.0))
        prev = close
    return out


def trending_up(n=220):
    return [100.0 * (1.003 ** i) * (1 + 0.0005 * math.sin(i * 1.7)) for i in range(n)]


def trending_down(n=220):
    return [100.0 * (0.997 ** i) * (1 + 0.0005 * math.sin(i * 1.7)) for i in range(n)]


def ranging(n=220):
    return [100.0 * (1 + 0.01 * math.sin(i * 0.4)) for i in range(n)]


def calm(n=150):
    return [100.0 * (1 + 0.001 * math.sin(i * 0.5)) for i in range(n)]


def vol_explosion(n=30):
    closes = calm()
    price = closes[-1]
    for i in range(n):
        price *= 1 + (0.05 if i % 2 else -0.05)
        closes.append(price)
    return closes


def crash(n=30):
    closes = calm()
    price = closes[-1]
    for i in range(n):
        price *= (0.98 + 0.03 * (i % 2)) * 0.99
        closes.append(price)
    return closes


def classifier(**config_kwargs):
    cfg = R.RegimeConfig(**config_kwargs) if config_kwargs else None
    return R.RegimeClassifier(clock=CLOCK, config=cfg)


# ---------------------------------------------------------------------------
# the four synthetic markets
# ---------------------------------------------------------------------------

def test_trending_up_series_classifies_as_trending_up():
    state = classifier().classify(from_closes(trending_up()))
    assert state.regime is Regime.TRENDING_UP
    assert state.confidence > 0.5
    assert state.evidence["slope_sigma"] > 0
    assert state.evidence["ma_separation"] > 0


def test_trending_down_series_classifies_as_trending_down():
    state = classifier().classify(from_closes(trending_down()))
    assert state.regime is Regime.TRENDING_DOWN
    assert state.confidence > 0.5
    assert state.evidence["slope_sigma"] < 0


def test_ranging_series_classifies_as_ranging():
    state = classifier().classify(from_closes(ranging()))
    assert state.regime is Regime.RANGING
    assert state.evidence["drawdown"] < 0.1


def test_volatility_explosion_classifies_as_high_volatility():
    state = classifier().classify(from_closes(vol_explosion()))
    assert state.regime is Regime.HIGH_VOLATILITY
    assert state.evidence["vol_percentile"] >= 0.85
    assert state.evidence["vol_ratio"] >= 1.5


def test_crash_classifies_as_crisis():
    state = classifier().classify(from_closes(crash()))
    assert state.regime is Regime.CRISIS
    assert state.evidence["drawdown"] >= 0.15
    assert state.confidence > 0.5


def test_crisis_outranks_high_volatility_and_trend():
    # A crash is simultaneously a downtrend and a vol explosion; precedence
    # must resolve it to CRISIS, because that is what changes the sizing rule.
    state = classifier().classify(from_closes(crash()))
    assert state.regime is Regime.CRISIS
    assert state.evidence["vol_ratio"] >= 1.5      # HIGH_VOLATILITY would fire too
    assert state.evidence["slope_sigma"] < 0       # so would TRENDING_DOWN


# ---------------------------------------------------------------------------
# refusal to guess
# ---------------------------------------------------------------------------

def test_insufficient_history_returns_unknown_with_zero_confidence():
    clf = classifier()
    state = clf.classify(from_closes(trending_up(30)))
    assert state.regime is Regime.UNKNOWN
    assert state.confidence == 0.0
    assert state.is_known is False
    assert state.evidence["bars"] == 30.0
    assert state.evidence["bars_required"] == float(clf.required_bars)


def test_exactly_one_bar_short_is_still_unknown():
    clf = classifier()
    need = clf.required_bars
    assert clf.classify(from_closes(trending_up(need - 1))).regime is Regime.UNKNOWN
    clf.reset()
    assert clf.classify(from_closes(trending_up(need))).regime is not Regime.UNKNOWN


def test_empty_candles_raises():
    with pytest.raises(InsufficientDataError):
        classifier().classify([])


def test_required_bars_tracks_the_configured_windows():
    wide = classifier(slow_ma=200, fast_ma=50, slope_window=40)
    assert wide.required_bars >= 240


# ---------------------------------------------------------------------------
# hysteresis, in isolation
# ---------------------------------------------------------------------------

def test_hysteresis_adopts_the_first_opinion_immediately():
    h = R.Hysteresis(confirm_bars=3)
    assert h.current is Regime.UNKNOWN
    assert h.update(Regime.RANGING) is Regime.RANGING


def test_hysteresis_requires_n_consecutive_bars_to_switch():
    h = R.Hysteresis(confirm_bars=3)
    h.update(Regime.RANGING)
    assert h.update(Regime.TRENDING_UP) is Regime.RANGING       # 1
    assert h.pending_count == 1
    assert h.update(Regime.TRENDING_UP) is Regime.RANGING       # 2
    assert h.update(Regime.TRENDING_UP) is Regime.TRENDING_UP   # 3 -> switch
    assert h.pending is None and h.pending_count == 0


def test_hysteresis_resets_the_counter_on_an_interrupted_run():
    h = R.Hysteresis(confirm_bars=3)
    h.update(Regime.RANGING)
    h.update(Regime.TRENDING_UP)
    h.update(Regime.TRENDING_UP)
    assert h.update(Regime.RANGING) is Regime.RANGING           # run broken
    assert h.pending_count == 0
    assert h.update(Regime.TRENDING_UP) is Regime.RANGING       # counting again
    assert h.pending_count == 1


def test_hysteresis_switches_pending_candidate_cleanly():
    h = R.Hysteresis(confirm_bars=3)
    h.update(Regime.RANGING)
    h.update(Regime.TRENDING_UP)
    h.update(Regime.TRENDING_DOWN)      # a different candidate restarts at 1
    assert h.pending is Regime.TRENDING_DOWN
    assert h.pending_count == 1
    assert h.current is Regime.RANGING


def test_hysteresis_holds_the_last_label_when_evidence_vanishes():
    h = R.Hysteresis(confirm_bars=2)
    h.update(Regime.TRENDING_UP)
    h.update(Regime.RANGING)
    assert h.pending_count == 1
    assert h.update(Regime.UNKNOWN) is Regime.TRENDING_UP
    assert h.pending_count == 0          # UNKNOWN does not count toward a switch


def test_hysteresis_reset_and_validation():
    h = R.Hysteresis(confirm_bars=2)
    h.update(Regime.RANGING)
    h.reset()
    assert h.current is Regime.UNKNOWN
    with pytest.raises(ConfigurationError):
        R.Hysteresis(confirm_bars=0)


def test_confirm_bars_one_switches_instantly():
    h = R.Hysteresis(confirm_bars=1)
    h.update(Regime.RANGING)
    assert h.update(Regime.TRENDING_UP) is Regime.TRENDING_UP


# ---------------------------------------------------------------------------
# hysteresis, on real series
# ---------------------------------------------------------------------------

def noisy_boundary_series(seed=7, n=300, sigma=0.004):
    """A driftless random walk — the pathological case for a trend filter.

    Seeded explicitly: this suite has no unseeded randomness anywhere.
    """
    rng = random.Random(seed)
    price = 100.0
    out = []
    for _ in range(n):
        price *= 1 + rng.gauss(0.0, sigma)
        out.append(price)
    return out


def _flips(labels):
    return sum(1 for a, b in zip(labels, labels[1:]) if a is not b)


def test_hysteresis_suppresses_flapping_on_a_noisy_boundary_series():
    # Fast windows deliberately: they make the raw label flap, which is the
    # condition hysteresis exists to survive.
    clf = classifier(fast_ma=3, slow_ma=5, slope_window=2, adx_window=5,
                     confirm_bars=3)
    states = [s for s in clf.history(from_closes(noisy_boundary_series()))
              if s.regime is not Regime.UNKNOWN]
    raw_flips = _flips([s.raw_regime for s in states])
    confirmed_flips = _flips([s.regime for s in states])
    assert raw_flips > 40                       # the series really does flap
    assert confirmed_flips < raw_flips / 2      # and hysteresis halves it


def test_every_confirmed_switch_is_backed_by_confirm_bars_of_evidence():
    """The exact invariant: a label only changes after N identical raw bars."""
    confirm = 3
    clf = classifier(fast_ma=3, slow_ma=5, slope_window=2, adx_window=5,
                     confirm_bars=confirm)
    states = [s for s in clf.history(from_closes(noisy_boundary_series()))
              if s.regime is not Regime.UNKNOWN]
    switches = 0
    for i in range(1, len(states)):
        if states[i].regime is states[i - 1].regime:
            continue
        switches += 1
        new = states[i].regime
        window = states[i - confirm + 1:i + 1]
        assert len(window) == confirm
        assert all(s.raw_regime is new for s in window), (
            f"switch to {new} at {i} without {confirm} consecutive raw bars")
    assert switches > 0                          # the invariant was exercised


def test_a_single_contrary_bar_never_switches_the_label():
    clf = classifier(confirm_bars=3)
    closes = trending_up(200)
    clf.classify(from_closes(closes))
    assert clf._hysteresis.current is Regime.TRENDING_UP
    # One violently contrary bar appended: raw evidence may change, the held
    # label must not.
    closes = closes + [closes[-1] * 0.90]
    state = clf.classify(from_closes(closes))
    assert state.regime is Regime.TRENDING_UP
    if state.raw_regime is not Regime.TRENDING_UP:
        assert state.pending_bars == 1
        # Conviction drops even though the label holds — the early warning.
        assert state.confidence < 1.0


def test_confidence_decays_as_contrary_evidence_accumulates():
    clf = classifier(fast_ma=3, slow_ma=5, slope_window=2, adx_window=5,
                     confirm_bars=4)
    states = clf.history(from_closes(noisy_boundary_series(seed=3)))
    pending = [s for s in states if s.pending_bars > 0]
    assert pending, "expected some bars with a pending switch"
    for a, b in zip(pending, pending[1:]):
        if b.pending_bars > a.pending_bars and b.regime is a.regime:
            assert b.confidence <= a.confidence + 1e-9


# ---------------------------------------------------------------------------
# statefulness and idempotency
# ---------------------------------------------------------------------------

def test_reclassifying_the_same_bar_is_idempotent():
    clf = classifier(confirm_bars=3)
    candles = from_closes(trending_up(200))
    first = clf.classify(candles)
    for _ in range(5):
        again = clf.classify(candles)
        assert again == first
        assert again.pending_bars == first.pending_bars


def test_out_of_order_bars_are_rejected_rather_than_absorbed():
    clf = classifier()
    candles = from_closes(trending_up(200))
    clf.classify(candles)
    with pytest.raises(DataValidationError):
        clf.classify(candles[:-5])
    clf.reset()
    assert clf.classify(candles[:-5]).regime is not Regime.UNKNOWN


def test_history_matches_a_live_classifier_fed_bar_by_bar():
    candles = from_closes(trending_up(140))
    walked = classifier().history(candles)
    live = classifier()
    streamed = [live.classify(candles[:i + 1]) for i in range(len(candles))]
    assert [s.to_dict() for s in walked] == [s.to_dict() for s in streamed]


def test_history_returns_one_state_per_bar_and_warms_up_as_unknown():
    clf = classifier()
    candles = from_closes(trending_up(140))
    states = clf.history(candles)
    assert len(states) == len(candles)
    assert all(s.regime is Regime.UNKNOWN for s in states[:clf.required_bars - 1])
    assert states[-1].regime is Regime.TRENDING_UP
    assert [s.ts for s in states] == [c.ts_close for c in candles]


def test_history_does_not_disturb_the_live_hysteresis_state():
    clf = classifier()
    candles = from_closes(trending_up(200))
    before = clf.classify(candles)
    clf.history(from_closes(crash()))
    # Re-asking for the same bar must still give the cached answer, proving
    # history() ran on its own private state machine.
    assert clf.classify(candles) == before


# ---------------------------------------------------------------------------
# contracts and configuration
# ---------------------------------------------------------------------------

def test_regime_state_is_json_serialisable():
    state = classifier().classify(from_closes(trending_up()))
    payload = json.loads(json.dumps(state.to_dict()))
    assert payload["regime"] == "trending_up"
    assert payload["instrument_key"] == KEY
    assert isinstance(payload["evidence"]["slope_sigma"], float)
    assert payload["ts"].endswith("+00:00")


def test_regime_state_validates_its_fields():
    with pytest.raises(ConfigurationError):
        R.RegimeState(ts=START, instrument_key=KEY, regime=Regime.RANGING,
                      confidence=1.5)
    with pytest.raises(ConfigurationError):
        R.RegimeState(ts=START, instrument_key=KEY, regime="ranging",
                      confidence=0.5)
    with pytest.raises(DataValidationError):
        R.RegimeState(ts=datetime(2024, 1, 1), instrument_key=KEY,
                      regime=Regime.RANGING, confidence=0.5)


def test_regime_config_rejects_incoherent_thresholds():
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(fast_ma=30, slow_ma=10)
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(confirm_bars=0)
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(high_vol_percentile=1.5)
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(high_vol_ratio=3.0, crisis_vol_ratio=2.0)
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(crisis_drawdown=0.0)
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(slope_sigma=0.0)
    with pytest.raises(ConfigurationError):
        R.RegimeConfig(base_confidence=1.0)


def test_classification_is_deterministic_across_instances():
    candles = from_closes(trending_up())
    a = classifier().classify(candles)
    b = classifier().classify(candles)
    assert a.to_dict() == b.to_dict()


def test_classifier_never_reads_the_wall_clock():
    # The state's timestamp comes from the bar, not from the clock — otherwise
    # a backtest would stamp every regime with today's date.
    clf = R.RegimeClassifier(clock=FixedClock(datetime(2030, 1, 1, tzinfo=timezone.utc)))
    candles = from_closes(trending_up())
    assert clf.classify(candles).ts == candles[-1].ts_close


# ---------------------------------------------------------------------------
# the local indicators
# ---------------------------------------------------------------------------

def test_simple_moving_average_is_aligned_and_correct():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    got = R.simple_moving_average(values, 3)
    assert got[:2] == [None, None]
    assert got[2] == pytest.approx(2.0)
    assert got[3] == pytest.approx(3.0)
    assert got[4] == pytest.approx(4.0)
    assert len(got) == len(values)
    with pytest.raises(ConfigurationError):
        R.simple_moving_average(values, 0)


def test_adx_is_none_until_warm_and_high_in_a_clean_trend():
    candles = from_closes(trending_up(120))
    got = R.adx(candles, 14)
    assert len(got) == len(candles)
    assert got[:2 * 14 - 1] == [None] * (2 * 14 - 1)
    assert got[-1] > 50.0
    # A driftless random walk has no directional persistence, so ADX is far
    # lower than in the clean trend above.
    choppy = R.adx(from_closes(noisy_boundary_series(seed=5, n=120)), 14)
    assert choppy[-1] < 40.0 < got[-1]


def test_adx_returns_all_none_when_too_short():
    assert R.adx(from_closes([100.0 + i for i in range(10)]), 14) == [None] * 10


def test_rolling_drawdown_measures_from_the_window_high():
    closes = [100.0, 120.0, 90.0]
    assert R.rolling_drawdown(closes, 3) == pytest.approx(0.25)
    assert R.rolling_drawdown([100.0, 110.0], 5) == pytest.approx(0.0)
    assert R.rolling_drawdown([], 5) == 0.0
    # Window shorter than the history forgets the older peak.
    assert R.rolling_drawdown([200.0, 100.0, 99.0], 2) == pytest.approx(0.01)
