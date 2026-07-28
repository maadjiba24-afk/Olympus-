"""The event-driven backtester.

Organised around the ways a backtest lies rather than around its API surface.
Each test names the specific self-deception it closes off, because "the
backtester works" is not a claim anyone can check — "the backtester cannot show
a strategy a bar that has not closed" is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading import perf
from olympus.trading import risk as R
from olympus.trading.backtest import (BacktestConfig, BacktestEngine,
                                      StrategyContext, WalkForward,
                                      WalkForwardWindow, anchored_windows,
                                      rolling_windows, walk_forward)
from olympus.trading.brokers.paper import FeeModel, SlippageModel
from olympus.trading.contracts import (Candle, Instrument, Mode, OrderType,
                                       Regime, Side, TradeIntent)
from olympus.trading.errors import ConfigurationError, DataValidationError
from olympus.trading.killswitch import KillSwitchRegistry

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")
OTHER = Instrument(symbol="MSFT", exchange="NASDAQ")

ZERO_FEES = FeeModel(per_share=Decimal("0"), bps=Decimal("0"),
                     fixed=Decimal("0"), minimum=Decimal("0"))
ZERO_SLIPPAGE = SlippageModel(bps=Decimal("0"))


def series(n=120, start=100.0, step=0.5, instrument=INST, first=T0, skip=()):
    """A gently rising series. `skip` removes bars to punch a hole in it."""
    return [Candle(instrument_key=instrument.key, timeframe="1d",
                   ts_open=first + timedelta(days=i),
                   ts_close=first + timedelta(days=i + 1),
                   open=start + i * step, high=start + i * step + 1,
                   low=start + i * step - 1, close=start + i * step + 0.5,
                   volume=10_000, amount=1_000_000)
            for i in range(n) if i not in skip]


def gapped_series(n=60, gap_low=30.0, gap_high=40.0, instrument=INST):
    """Bars whose open sits far below their close, so a fill at the next open
    is unmistakably distinguishable from a fill at the current close."""
    return [Candle(instrument_key=instrument.key, timeframe="1d",
                   ts_open=T0 + timedelta(days=i),
                   ts_close=T0 + timedelta(days=i + 1),
                   open=gap_low + i, high=gap_high + i + 1,
                   low=gap_low + i - 1, close=gap_high + i,
                   volume=10_000, amount=1_000_000)
            for i in range(n)]


class Alternating:
    """Buys then sells every 20 bars — enough activity to produce trades."""
    id = "alt"
    version = "1"

    def __init__(self, instruments=(INST,)):
        # Counted per instrument, not globally: a shared counter would make a
        # two-instrument run trade only one of them, which is exactly the kind
        # of accident that makes a breakdown test pass vacuously.
        self.counts: dict[str, int] = {}
        self.n = 0
        self.seen_windows = []
        self.contexts = []
        self.clock_agreed = []
        self.instruments = {i.key for i in instruments}

    def on_bar(self, ctx):
        self.n += 1
        key = ctx.instrument.key
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        self.seen_windows.append((ctx.as_of, tuple(c.ts_close for c in ctx.candles)))
        self.contexts.append(ctx)
        self.clock_agreed.append(ctx.clock.now() == ctx.as_of)
        if count % 20:
            return []
        buy = (count // 20) % 2 == 1
        px = round(ctx.bar.close, 2)
        return [TradeIntent(
            intent_id=f"i{key}{count}", strategy_id="alt", strategy_version="1",
            instrument_key=ctx.instrument.key, side=Side.BUY if buy else Side.SELL,
            order_type=OrderType.MARKET, quantity=Decimal("10"),
            created_at=ctx.as_of, intended_entry=Decimal(str(px)),
            stop_loss=Decimal(str(round(px * (0.9 if buy else 1.1), 2))),
            confidence=0.7)]


class BuyOnce:
    """Emits exactly one market buy, on the bar with index `at`."""
    id = "once"
    version = "1"

    def __init__(self, at=10):
        self.at = at
        self.n = 0
        self.signal_bar = None

    def on_bar(self, ctx):
        self.n += 1
        if self.n != self.at:
            return []
        self.signal_bar = ctx.bar
        px = round(ctx.bar.close, 2)
        return [TradeIntent(
            intent_id="once", strategy_id="once", strategy_version="1",
            instrument_key=ctx.instrument.key, side=Side.BUY,
            order_type=OrderType.MARKET, quantity=Decimal("10"),
            created_at=ctx.as_of, intended_entry=Decimal(str(px)),
            stop_loss=Decimal(str(round(px * 0.5, 2))), confidence=0.9)]


class Never:
    id = "never"
    version = "1"

    def on_bar(self, ctx):
        return []


def _limits(*instruments):
    return R.RiskLimits(
        approved_instruments=frozenset(i.key for i in (instruments or (INST,))),
        allowed_modes=frozenset({Mode.BACKTEST}),
        max_order_value=Decimal("50000"), require_stop_loss=True,
        require_broker_connected=False, max_daily_loss=None,
        max_data_age_s=None)


def _engine(strategy=None, data=None, cfg=None, instruments=None, limits=None,
            **kw):
    instruments = instruments or {INST.key: INST}
    engine = R.RiskEngine(limits=limits or _limits(*instruments.values()),
                          killswitches=KillSwitchRegistry())
    return BacktestEngine(
        config=cfg or BacktestConfig(timeframe="1d", warmup_bars=5),
        data=data or {INST.key: series()}, instruments=instruments,
        strategy=strategy or Alternating(), risk_engine=engine, **kw)


# --- look-ahead: the lie the whole design exists to prevent -----------------

def test_a_strategy_never_sees_a_bar_that_has_not_closed():
    strategy = Alternating()
    _engine(strategy).run()
    assert strategy.seen_windows
    for as_of, closes in strategy.seen_windows:
        assert all(c <= as_of for c in closes), (
            "a strategy was handed a bar closing after its decision time")


def test_a_bar_the_clock_has_not_reached_is_refused():
    """The guard the loop runs on every bar, exercised directly. If it were
    ever removed, every other look-ahead test would still pass by accident
    because the loop happens to advance the clock first."""
    engine = _engine()
    future = series(n=1, first=T0 + timedelta(days=500))[0]
    with pytest.raises(DataValidationError):
        engine.assert_deliverable(future)


def test_a_non_final_bar_is_refused_and_never_replayed():
    """An in-progress bar's close has not happened yet."""
    engine = _engine()
    partial = series(n=1)[0].with_final(False)
    engine.clock.set(partial.ts_close)
    with pytest.raises(DataValidationError):
        engine.assert_deliverable(partial)
    # ... and it never enters the stream in the first place.
    only_partial = [c.with_final(False) for c in series(n=10)]
    with pytest.raises(ConfigurationError):
        _engine(data={INST.key: only_partial})


def test_the_last_bar_a_strategy_sees_is_the_one_that_just_closed():
    """The off-by-one that makes a backtest prophetic: handing over the bar
    that is *about* to close rather than the one that has."""
    strategy = Alternating()
    _engine(strategy).run()
    for as_of, closes in strategy.seen_windows:
        assert closes[-1] == as_of


def test_a_duplicate_bar_is_refused_rather_than_doubling_a_position():
    bars = series(n=30)
    with pytest.raises(DataValidationError):
        _engine(data={INST.key: bars + [bars[10]]})


def test_the_stream_must_be_strictly_increasing_in_time():
    """Defence in depth: the stream is sorted at construction, so this guard can
    only fire if something later corrupts the ordering. It is tested anyway,
    because a guard nobody has ever seen fire is a guard nobody knows works."""
    engine = _engine()
    engine._slices = list(reversed(engine._slices))
    with pytest.raises((DataValidationError, ValueError)):
        engine.run()


def test_zero_latency_is_refused_at_configuration():
    """latency_bars=0 means filling on the bar that produced the signal."""
    with pytest.raises(ConfigurationError):
        BacktestConfig(latency_bars=0)


# --- fills ------------------------------------------------------------------

def test_a_market_order_fills_at_the_next_bars_open_not_this_bars_close():
    """The classic backtest lie, priced. The data is built so the two answers
    are ~10 apart, well outside any slippage."""
    bars = gapped_series()
    strategy = BuyOnce(at=10)
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                         slippage_model=ZERO_SLIPPAGE, fee_model=ZERO_FEES)
    result = _engine(strategy, data={INST.key: bars}, cfg=cfg).run()

    assert len(result.fills) == 1
    fill = result.fills[0]
    signal_index = bars.index(strategy.signal_bar)
    next_bar = bars[signal_index + 1]
    assert fill.price == pytest.approx(Decimal(str(next_bar.open)))
    assert fill.price != pytest.approx(Decimal(str(strategy.signal_bar.close)))
    assert fill.ts == next_bar.ts_close


def test_extra_latency_delays_the_fill_by_exactly_that_many_bars():
    bars = gapped_series()
    results = {}
    for latency in (1, 3):
        strategy = BuyOnce(at=10)
        cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                             latency_bars=latency,
                             slippage_model=ZERO_SLIPPAGE, fee_model=ZERO_FEES)
        result = _engine(strategy, data={INST.key: bars}, cfg=cfg).run()
        signal_index = bars.index(strategy.signal_bar)
        results[latency] = (result.fills[0], bars[signal_index + latency])
    for latency, (fill, expected_bar) in results.items():
        assert fill.price == pytest.approx(Decimal(str(expected_bar.open))), latency
        assert fill.ts == expected_bar.ts_close


def test_warmup_bars_produce_no_trades():
    cfg = BacktestConfig(timeframe="1d", warmup_bars=1000)
    result = _engine(cfg=cfg).run()
    assert result.intents == () and result.orders == ()
    assert result.fills == ()


def test_warmup_is_counted_per_instrument_not_globally():
    strategy = Alternating(instruments=(INST, OTHER))
    data = {INST.key: series(n=40), OTHER.key: series(n=40, instrument=OTHER)}
    cfg = BacktestConfig(timeframe="1d", warmup_bars=10)
    _engine(strategy, data=data, cfg=cfg,
            instruments={INST.key: INST, OTHER.key: OTHER}).run()
    per_key: dict[str, int] = {}
    for ctx in strategy.contexts:
        per_key[ctx.instrument.key] = per_key.get(ctx.instrument.key, 0) + 1
    assert per_key[INST.key] == per_key[OTHER.key] == 30


# --- the strategy's view is immutable ---------------------------------------

def test_the_strategy_context_is_frozen():
    class Vandal:
        id = "vandal"

        def __init__(self):
            self.errors = []

        def on_bar(self, ctx):
            try:
                ctx.candles = ()
            except Exception as exc:                 # noqa: BLE001
                self.errors.append(type(exc).__name__)
            try:
                ctx.candles.append(ctx.bar)          # tuple has no append
            except Exception as exc:                 # noqa: BLE001
                self.errors.append(type(exc).__name__)
            return []

    strategy = Vandal()
    _engine(strategy, data={INST.key: series(n=20)}).run()
    assert strategy.errors, "a strategy was able to mutate its own context"
    assert isinstance(strategy.errors[0], str)


def test_mutating_the_handed_out_window_cannot_affect_later_bars():
    """A strategy that corrupted the engine's history would look like alpha."""
    class Mutator:
        id = "mut"

        def __init__(self):
            self.lengths = []

        def on_bar(self, ctx):
            copy = list(ctx.candles)
            copy.clear()
            copy.append(ctx.bar)
            self.lengths.append(len(ctx.candles))
            return []

    strategy = Mutator()
    _engine(strategy, data={INST.key: series(n=30)},
            cfg=BacktestConfig(timeframe="1d", warmup_bars=0)).run()
    assert strategy.lengths == list(range(1, 31)), (
        "the engine's history was affected by what the strategy did to its copy")


def test_the_window_is_bounded_by_max_bars_per_strategy_call():
    class Watcher:
        id = "watch"

        def __init__(self):
            self.max_seen = 0

        def on_bar(self, ctx):
            self.max_seen = max(self.max_seen, len(ctx.candles))
            return []

    strategy = Watcher()
    cfg = BacktestConfig(timeframe="1d", warmup_bars=0,
                         max_bars_per_strategy_call=15)
    _engine(strategy, data={INST.key: series(n=60)}, cfg=cfg).run()
    assert strategy.max_seen == 15
    assert cfg.max_lookback == 15                    # historical spelling


def test_the_context_is_the_declared_contract():
    strategy = Alternating()
    _engine(strategy, data={INST.key: series(n=20)}).run()
    ctx = strategy.contexts[0]
    assert isinstance(ctx, StrategyContext)
    assert isinstance(ctx.candles, tuple)
    assert ctx.timeframe == "1d"
    assert all(strategy.clock_agreed), (
        "the clock disagreed with the decision time the strategy was given")


# --- costs ------------------------------------------------------------------

def test_costs_are_always_modelled_and_reported_both_ways():
    result = _engine().run()
    assert result.report.total_fees > 0
    assert result.report.total_return_gross > result.report.total_return_net, (
        "gross must exceed net whenever costs were charged; a report that can "
        "show only one of them will eventually show only the flattering one")


def test_fees_and_slippage_reduce_returns_against_a_zero_cost_run():
    data = {INST.key: series()}
    free = _engine(cfg=BacktestConfig(timeframe="1d", warmup_bars=5,
                                      fee_model=ZERO_FEES,
                                      slippage_model=ZERO_SLIPPAGE),
                   data=data).run()
    costly = _engine(cfg=BacktestConfig(
        timeframe="1d", warmup_bars=5,
        fee_model=FeeModel(bps=Decimal("10")),
        slippage_model=SlippageModel(bps=Decimal("25"))), data=data).run()

    assert free.report.total_fees == 0
    assert costly.report.total_fees > 0
    assert costly.report.total_slippage > 0
    assert costly.final_equity < free.final_equity
    assert costly.report.total_return_net < free.report.total_return_net


def test_the_cost_curve_is_cumulative_and_never_decreases():
    result = _engine().run()
    values = [c for _, c in result.cost_curve]
    assert values == sorted(values)
    assert values[-1] >= result.report.total_fees


def test_a_zero_cost_run_still_reports_both_bases():
    """The structural guarantee holds even when the two numbers are equal."""
    result = _engine(cfg=BacktestConfig(timeframe="1d", warmup_bars=5,
                                        fee_model=ZERO_FEES,
                                        slippage_model=ZERO_SLIPPAGE)).run()
    assert result.report.gross is not None and result.report.net is not None
    assert result.report.total_return_gross == pytest.approx(
        result.report.total_return_net)


# --- reproducibility --------------------------------------------------------

def test_the_run_is_deterministic():
    """Regression: two identical runs once disagreed because the second reused
    the first's persisted orders and never reached the broker."""
    a, b = _engine().run(), _engine().run()
    assert a.equity_curve == b.equity_curve
    assert a.config_hash == b.config_hash
    assert a.data_hash == b.data_hash
    assert len(a.orders) == len(b.orders)
    assert [f.price for f in a.fills] == [f.price for f in b.fills]


def test_the_same_seed_gives_the_same_result_twice():
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5, seed=7)
    a = _engine(cfg=cfg).run()
    b = _engine(cfg=cfg).run()
    assert a.equity_curve == b.equity_curve
    assert a.report.to_dict() == b.report.to_dict()


def test_config_and_data_hashes_change_with_their_inputs():
    base = _engine().run()
    assert _engine(cfg=BacktestConfig(timeframe="1d", warmup_bars=7)
                   ).run().config_hash != base.config_hash
    assert _engine(data={INST.key: series(n=80)}
                   ).run().data_hash != base.data_hash


@pytest.mark.parametrize("changed", [
    {"warmup_bars": 9}, {"latency_bars": 2}, {"seed": 3},
    {"starting_cash": Decimal("50000")}, {"continuous_market": True},
    {"max_bars_per_strategy_call": 64}, {"risk_free_rate": 0.03},
    {"instruments": ("NASDAQ:AAPL",)}, {"max_mark_staleness_bars": 9},
    {"survivorship_warn_days": 30}, {"start": T0 + timedelta(days=1)},
    {"fee_model": FeeModel(bps=Decimal("7"))},
    {"slippage_model": SlippageModel(bps=Decimal("9"))},
])
def test_every_field_that_changes_a_result_changes_the_config_hash(changed):
    """A knob that moved results without entering the hash would make two
    incomparable runs look comparable — worse than having no hash at all."""
    base = BacktestConfig(timeframe="1d")
    assert BacktestConfig(timeframe="1d", **changed).hash() != base.hash()


def test_the_data_hash_covers_prices_not_just_timestamps():
    base = _engine(data={INST.key: series()}).run()
    shifted = _engine(data={INST.key: series(start=101.0)}).run()
    assert shifted.data_hash != base.data_hash


# --- missing data -----------------------------------------------------------

def test_a_hole_in_the_data_is_reported_and_never_forward_filled():
    hole = set(range(40, 48))
    data = {INST.key: series(n=90, skip=hole)}
    result = _engine(data=data).run()

    assert any("no data between" in w for w in result.warnings)
    stamps = {ts for ts, _ in result.equity_curve}
    for i in hole:
        assert (T0 + timedelta(days=i + 1)) not in stamps, (
            "the engine invented an equity point for a bar it never saw")


def test_the_first_window_after_a_hole_is_skipped_as_discontinuous():
    """That window straddles a gap, so any indicator computed over it is wrong
    in a way no downstream check can detect."""
    class Recorder:
        id = "rec"

        def __init__(self):
            self.seen = []

        def on_bar(self, ctx):
            self.seen.append(ctx.bar.ts_close)
            return []

    hole = set(range(40, 48))
    strategy = Recorder()
    _engine(strategy, data={INST.key: series(n=90, skip=hole)},
            cfg=BacktestConfig(timeframe="1d", warmup_bars=0)).run()
    resume = T0 + timedelta(days=49)                 # bar 48's close
    assert resume not in strategy.seen
    assert (resume + timedelta(days=1)) in strategy.seen


def test_a_weekend_sized_gap_is_not_mistaken_for_missing_data():
    """Daily equity bars skip weekends. Warning on every Monday would make the
    warning list useless, and a useless warning list gets ignored."""
    bars = [c for c in series(n=60)
            if (c.ts_open.weekday() < 5)]
    result = _engine(data={INST.key: bars}).run()
    assert not any("no data between" in w for w in result.warnings)


# --- survivorship -----------------------------------------------------------

def test_a_static_universe_over_a_long_window_is_flagged():
    """A universe picked from instruments that exist *today* carries a bias the
    engine cannot remove, so it says so rather than staying quiet."""
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                         survivorship_warn_days=30)
    result = _engine(cfg=cfg).run()                  # 120 days of data > 30
    assert any("survivorship" in w for w in result.warnings)


def test_a_short_window_is_not_flagged_for_survivorship():
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                         survivorship_warn_days=365)
    result = _engine(cfg=cfg).run()                  # 120 days < 365
    assert not any("survivorship" in w for w in result.warnings)


def test_supplying_listings_replaces_the_survivorship_warning_with_real_dates():
    listings = {INST.key: (T0.date(), (T0 + timedelta(days=200)).date())}
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                         survivorship_warn_days=30)
    result = _engine(cfg=cfg, listings=listings).run()
    assert not any("survivorship" in w for w in result.warnings)


def test_a_delisted_instrument_stops_being_traded():
    listings = {INST.key: (T0.date(), (T0 + timedelta(days=30)).date())}
    result = _engine(listings=listings).run()
    assert result.bars_processed < 120


def test_an_instrument_is_not_traded_before_its_inclusion_date():
    listings = {INST.key: ((T0 + timedelta(days=60)).date(), None)}
    result = _engine(listings=listings).run()
    assert result.bars_processed <= 61
    assert all(ts >= T0 + timedelta(days=59)
               for ts, _ in result.equity_curve[-1:])


def test_intrabar_ambiguity_is_disclosed_not_buried():
    result = _engine().run()
    assert any("tick" in w for w in result.warnings)


# --- the real risk engine, not a stub --------------------------------------

def test_a_backtest_without_a_risk_engine_is_refused():
    with pytest.raises(ConfigurationError):
        BacktestEngine(config=BacktestConfig(), data={INST.key: series()},
                       instruments={INST.key: INST}, strategy=Never(),
                       risk_engine=None)


def test_rejected_intents_are_recorded_not_discarded():
    """An operator must be able to see what the risk engine refused."""
    limits = R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        allowed_modes=frozenset({Mode.BACKTEST}),
        max_order_value=Decimal("1"),                # everything gets refused
        require_stop_loss=True, require_broker_connected=False,
        max_daily_loss=None, max_data_age_s=None)
    result = _engine(limits=limits).run()
    assert result.intents and result.orders == ()
    assert len(result.rejected) == len(result.intents)


def test_a_strategy_that_raises_does_not_truncate_the_run():
    """Aborting would report a partial curve as if it were complete."""
    class Exploding:
        id = "boom"

        def on_bar(self, ctx):
            raise RuntimeError("strategy bug")

    result = _engine(Exploding()).run()
    assert result.bars_processed == 120
    assert result.intents == ()


def test_a_strategy_that_never_trades_produces_a_flat_curve():
    result = _engine(Never()).run()
    assert result.orders == ()
    assert result.report.n_trades == 0
    assert result.report.sharpe is None, (
        "a flat curve has no Sharpe ratio; returning a number would put the "
        "strategy that never traded on the leaderboard")


# --- reporting --------------------------------------------------------------

def test_the_equity_curve_has_exactly_one_point_per_timestamp():
    data = {INST.key: series(n=40), OTHER.key: series(n=40, instrument=OTHER)}
    result = _engine(Alternating(instruments=(INST, OTHER)), data=data,
                     instruments={INST.key: INST, OTHER.key: OTHER}).run()
    stamps = [ts for ts, _ in result.equity_curve]
    assert len(stamps) == len(set(stamps)) == 40
    assert stamps == sorted(stamps)


def test_the_per_instrument_breakdown_sums_back_to_the_whole():
    data = {INST.key: series(n=100), OTHER.key: series(n=100, instrument=OTHER,
                                                       start=50.0)}
    result = _engine(Alternating(instruments=(INST, OTHER)), data=data,
                     instruments={INST.key: INST, OTHER.key: OTHER}).run()
    assert len(result.per_instrument) == 2
    assert sum(r.n_trades for r in result.per_instrument.values()) == \
        result.report.n_trades
    assert sum(r.net.total_pnl for r in result.per_instrument.values()) == \
        pytest.approx(result.report.net.total_pnl)


def test_the_per_regime_breakdown_sums_back_to_the_whole():
    regimes = {T0 + timedelta(days=i): (Regime.RANGING if i < 60
                                        else Regime.TRENDING_UP)
               for i in range(0, 130, 10)}
    result = _engine(regimes=regimes).run()
    assert result.per_regime
    assert sum(r.n_trades for r in result.per_regime.values()) == \
        result.report.n_trades


def test_the_per_period_breakdown_covers_every_bar():
    result = _engine().run()
    assert sum(r.n_bars for r in result.per_period.values()) >= \
        result.report.n_bars
    assert sum(r.n_trades for r in result.per_period.values()) == \
        result.report.n_trades


def test_turnover_measures_notional_not_order_count():
    """Counting orders makes a strategy that trades one share look as busy as
    one that trades the whole book."""
    result = _engine().run()
    assert 0 < result.report.turnover < 100
    assert result.report.turnover != float(len(result.orders))


def test_exposure_is_the_fraction_of_bars_holding_a_position():
    flat = _engine(Never()).run()
    active = _engine().run()
    assert flat.report.exposure == 0.0
    assert 0.0 < active.report.exposure <= 1.0


def test_the_result_serialises_to_json():
    import json
    json.dumps(_engine().run().to_dict())


def test_the_report_carries_the_annualisation_it_used():
    result = _engine().run()
    assert result.report.periods_per_year == pytest.approx(252.0)
    assert result.report.timeframe == "1d"


# --- windowing --------------------------------------------------------------

def test_start_and_end_clip_the_replayed_stream():
    cfg = BacktestConfig(timeframe="1d", warmup_bars=0,
                         start=T0 + timedelta(days=10),
                         end=T0 + timedelta(days=30))
    result = _engine(cfg=cfg).run()
    assert result.equity_curve[0][0] >= T0 + timedelta(days=10)
    assert result.equity_curve[-1][0] <= T0 + timedelta(days=30)


def test_an_end_before_start_is_refused():
    with pytest.raises(ConfigurationError):
        BacktestConfig(start=T0 + timedelta(days=5), end=T0)


def test_a_declared_universe_excludes_everything_else():
    data = {INST.key: series(n=30), OTHER.key: series(n=30, instrument=OTHER)}
    cfg = BacktestConfig(timeframe="1d", warmup_bars=0,
                         instruments=(INST.key,))
    result = _engine(Never(), data=data, cfg=cfg,
                     instruments={INST.key: INST, OTHER.key: OTHER}).run()
    assert result.bars_processed == 30


# --- walk-forward -----------------------------------------------------------

def test_a_window_whose_test_span_overlaps_its_training_span_is_refused():
    """Every metric produced from an overlapping split is in-sample."""
    with pytest.raises(ConfigurationError):
        WalkForwardWindow(train_start=T0, train_end=T0 + timedelta(days=60),
                          test_start=T0 + timedelta(days=30),
                          test_end=T0 + timedelta(days=90))


def test_rolling_windows_never_overlap_train_with_test():
    windows = rolling_windows(T0, T0 + timedelta(days=365),
                              train=timedelta(days=90), test=timedelta(days=30))
    assert windows
    for w in windows:
        assert w.train_end <= w.test_start
    for a, b in zip(windows, windows[1:]):
        assert b.test_start >= a.test_end            # nor with each other


def test_anchored_windows_grow_the_training_span_from_a_fixed_start():
    windows = anchored_windows(T0, T0 + timedelta(days=365),
                               train=timedelta(days=90), test=timedelta(days=30))
    assert all(w.train_start == T0 for w in windows)
    spans = [w.train_end - w.train_start for w in windows]
    assert spans == sorted(spans)
    for w in windows:
        assert w.train_end <= w.test_start


def test_walk_forward_refuses_overlapping_test_windows():
    """Overlapping test spans would count the same bars twice in the combined
    report and inflate it."""
    a = WalkForwardWindow(train_start=T0, train_end=T0 + timedelta(days=30),
                          test_start=T0 + timedelta(days=30),
                          test_end=T0 + timedelta(days=90))
    b = WalkForwardWindow(train_start=T0, train_end=T0 + timedelta(days=40),
                          test_start=T0 + timedelta(days=40),
                          test_end=T0 + timedelta(days=100))
    with pytest.raises(ConfigurationError):
        WalkForward(lambda w: None, [a, b])


def test_walk_forward_evaluates_only_the_test_spans():
    bars = series(n=300)
    windows = rolling_windows(T0, T0 + timedelta(days=300),
                              train=timedelta(days=100),
                              test=timedelta(days=50))

    def factory(window):
        cfg = BacktestConfig(timeframe="1d", warmup_bars=2,
                             start=window.test_start, end=window.test_end)
        return _engine(data={INST.key: bars}, cfg=cfg)

    outcome = WalkForward(factory, windows).run()
    assert outcome.n_windows == len(windows)
    for window, result in zip(windows, outcome.results):
        for ts, _ in result.equity_curve:
            assert window.test_start <= ts <= window.test_end, (
                "a walk-forward segment reported bars from its training span")
    assert not hasattr(outcome, "in_sample")


def test_the_combined_walk_forward_report_chains_returns_not_levels():
    """Each window restarts from the same cash, so concatenating raw equity
    would draw a sawtooth and invent a drawdown at every boundary."""
    bars = series(n=300)
    windows = rolling_windows(T0, T0 + timedelta(days=300),
                              train=timedelta(days=100),
                              test=timedelta(days=50))

    def factory(window):
        cfg = BacktestConfig(timeframe="1d", warmup_bars=2,
                             start=window.test_start, end=window.test_end)
        return _engine(data={INST.key: bars}, cfg=cfg)

    outcome = WalkForward(factory, windows).run()
    per_window = 1.0
    for result in outcome.results:
        per_window *= (1.0 + result.report.total_return_net)
    assert outcome.combined.total_return_net == pytest.approx(per_window - 1.0,
                                                              rel=1e-6)
    assert outcome.combined.max_drawdown <= max(
        r.report.max_drawdown for r in outcome.results) + 1e-9


def test_walk_forward_needs_at_least_one_window():
    with pytest.raises(ConfigurationError):
        WalkForward(lambda w: None, [])


def test_the_bar_indexed_wrapper_returns_only_out_of_sample_segments():
    bars = series(n=200)

    def make(data):
        return _engine(data=data,
                       cfg=BacktestConfig(timeframe="1d", warmup_bars=2))

    results = walk_forward(data={INST.key: bars}, make_engine=make,
                           train_bars=50, test_bars=25)
    # windows start at 0, 25, ... while start + 50 + 25 <= 200
    assert len(results) == 6
    for r in results:
        assert r.bars_processed <= 25


def test_walk_forward_rejects_nonsense_windows():
    with pytest.raises(ConfigurationError):
        walk_forward(data={INST.key: series()}, make_engine=lambda d: None,
                     train_bars=0, test_bars=5)


# --- metrics wiring (the metrics themselves live in test_trading_perf.py) ----

def test_max_drawdown_against_a_hand_computed_curve():
    curve = [(T0, Decimal("100")), (T0 + timedelta(days=1), Decimal("120")),
             (T0 + timedelta(days=2), Decimal("90")),
             (T0 + timedelta(days=3), Decimal("150"))]
    depth, _ = perf.max_drawdown(curve)
    assert depth == pytest.approx(0.25)              # 120 -> 90


def test_a_flat_curve_has_no_sharpe_rather_than_infinite():
    curve = [(T0 + timedelta(days=i), Decimal("100")) for i in range(30)]
    report = perf.compute_report(curve, [], timeframe="1d")
    assert report.sharpe is None
    assert report.volatility == 0.0
    assert report.max_drawdown == 0.0


def test_report_formats_undefined_metrics_as_not_available():
    curve = [(T0, Decimal("100")), (T0 + timedelta(days=1), Decimal("100"))]
    text = perf.compute_report(curve, [], timeframe="1d").format()
    assert "n/a" in text


def test_breakdowns_partition_the_trades():
    """Each breakdown returns a report per bucket, and the trade counts must sum
    back to the whole — a breakdown that loses or double-counts trades would
    quietly misattribute where the P&L came from."""
    class T:
        def __init__(self, key, when):
            self.net_pnl = 1.0
            self.gross_pnl = 1.0
            self.fees = 0.0
            self.instrument_key = key
            self.closed_at = when

    trades = [T("A", T0), T("B", T0), T("A", T0 + timedelta(days=40))]

    per_instrument = perf.by_instrument(trades)
    assert set(per_instrument) == {"A", "B"}
    assert sum(r.n_trades for r in per_instrument.values()) == len(trades)
    assert per_instrument["A"].n_trades == 2

    curve = [(T0 + timedelta(days=i), Decimal("1000") + i) for i in range(60)]
    per_month = perf.by_period(curve, "M", trades=trades)
    assert sum(r.n_trades for r in per_month.values()) == len(trades)
    assert len(perf.by_period(curve, "Y", trades=trades)) == 1
