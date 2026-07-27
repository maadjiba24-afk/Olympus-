"""The backtester and its metrics.

Organised around the ways a backtest lies rather than around its API surface.
Each test names the specific self-deception it closes off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading import perf
from olympus.trading.backtest import (BacktestConfig, BacktestEngine,
                                      walk_forward)
from olympus.trading.contracts import (Candle, Instrument, Mode, OrderType,
                                       Side, TradeIntent)
from olympus.trading.errors import ConfigurationError, DataValidationError
from olympus.trading.killswitch import KillSwitchRegistry
from olympus.trading import risk as R

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
INST = Instrument(symbol="AAPL", exchange="NASDAQ")


def series(n=120, start=100.0, step=0.5):
    return [Candle(instrument_key=INST.key, timeframe="1d",
                   ts_open=T0 + timedelta(days=i),
                   ts_close=T0 + timedelta(days=i + 1),
                   open=start + i * step, high=start + i * step + 1,
                   low=start + i * step - 1, close=start + i * step + 0.5,
                   volume=10_000, amount=1_000_000)
            for i in range(n)]


class Alternating:
    """Buys then sells every 20 bars — enough activity to produce trades."""
    id = "alt"
    version = "1"

    def __init__(self):
        self.n = 0
        self.seen_windows = []

    def on_bar(self, ctx):
        self.n += 1
        self.seen_windows.append((ctx.as_of, tuple(c.ts_close for c in ctx.candles)))
        if self.n % 20:
            return []
        buy = (self.n // 20) % 2 == 1
        px = round(ctx.bar.close, 2)
        return [TradeIntent(
            intent_id=f"i{self.n}", strategy_id="alt", strategy_version="1",
            instrument_key=INST.key, side=Side.BUY if buy else Side.SELL,
            order_type=OrderType.MARKET, quantity=Decimal("10"),
            created_at=ctx.as_of, intended_entry=Decimal(str(px)),
            stop_loss=Decimal(str(round(px * (0.9 if buy else 1.1), 2))),
            confidence=0.7)]


class Never:
    id = "never"
    version = "1"

    def on_bar(self, ctx):
        return []


def _engine(strategy=None, data=None, cfg=None, **kw):
    limits = R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        allowed_modes=frozenset({Mode.BACKTEST}),
        max_order_value=Decimal("50000"), require_stop_loss=True,
        require_broker_connected=False, max_daily_loss=None,
        max_data_age_s=None)
    engine = R.RiskEngine(limits=limits, killswitches=KillSwitchRegistry())
    return BacktestEngine(
        config=cfg or BacktestConfig(timeframe="1d", warmup_bars=5),
        data=data or {INST.key: series()}, instruments={INST.key: INST},
        strategy=strategy or Alternating(), risk_engine=engine, **kw)


# --- the lies it must not tell --------------------------------------------

def test_a_strategy_never_sees_a_bar_that_has_not_closed():
    strategy = Alternating()
    _engine(strategy).run()
    for as_of, closes in strategy.seen_windows:
        assert all(c <= as_of for c in closes), (
            "a strategy was handed a bar closing after its decision time")


def test_zero_latency_is_refused_at_configuration():
    """latency_bars=0 means filling on the bar that produced the signal."""
    with pytest.raises(ConfigurationError):
        BacktestConfig(latency_bars=0)


def test_warmup_bars_produce_no_trades():
    cfg = BacktestConfig(timeframe="1d", warmup_bars=1000)
    result = _engine(cfg=cfg).run()
    assert result.intents == () and result.orders == ()


def test_costs_are_always_modelled_and_reported_both_ways():
    result = _engine().run()
    assert result.report.total_fees > 0
    assert result.report.total_return_gross > result.report.total_return_net, (
        "gross must exceed net whenever costs were charged; a report that can "
        "show only one of them will eventually show only the flattering one")


def test_the_run_is_deterministic():
    """Regression: two identical runs once disagreed because the second reused
    the first's persisted orders and never reached the broker."""
    a, b = _engine().run(), _engine().run()
    assert a.equity_curve[-1][1] == b.equity_curve[-1][1]
    assert a.config_hash == b.config_hash
    assert a.data_hash == b.data_hash
    assert len(a.orders) == len(b.orders)


def test_config_and_data_hashes_change_with_their_inputs():
    base = _engine().run()
    other_cfg = _engine(cfg=BacktestConfig(timeframe="1d", warmup_bars=7)).run()
    other_data = _engine(data={INST.key: series(n=80)}).run()
    assert other_cfg.config_hash != base.config_hash
    assert other_data.data_hash != base.data_hash


def test_a_static_universe_over_a_long_window_is_flagged():
    """A universe picked from instruments that exist *today* carries a bias the
    engine cannot remove, so it says so rather than staying quiet."""
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                         survivorship_warn_days=30)
    result = _engine(cfg=cfg).run()          # 120 days of data > 30
    assert any("survivorship" in w for w in result.warnings)


def test_a_short_window_is_not_flagged_for_survivorship():
    cfg = BacktestConfig(timeframe="1d", warmup_bars=5,
                         survivorship_warn_days=365)
    result = _engine(cfg=cfg).run()          # 120 days < 365
    assert not any("survivorship" in w for w in result.warnings)


def test_intrabar_ambiguity_is_disclosed_not_buried():
    result = _engine().run()
    assert any("tick" in w or "intrabar" in w.lower() for w in result.warnings)


def test_a_delisted_instrument_stops_being_traded():
    listings = {INST.key: (T0.date(), (T0 + timedelta(days=30)).date())}
    result = _engine(listings=listings).run()
    assert result.bars_processed < 120


def test_rejected_intents_are_recorded_not_discarded():
    """An operator must be able to see what the risk engine refused."""
    limits = R.RiskLimits(
        approved_instruments=frozenset({INST.key}),
        allowed_modes=frozenset({Mode.BACKTEST}),
        max_order_value=Decimal("1"),          # everything gets refused
        require_stop_loss=True, require_broker_connected=False,
        max_daily_loss=None, max_data_age_s=None)
    engine = BacktestEngine(
        config=BacktestConfig(timeframe="1d", warmup_bars=5),
        data={INST.key: series()}, instruments={INST.key: INST},
        strategy=Alternating(),
        risk_engine=R.RiskEngine(limits=limits, killswitches=KillSwitchRegistry()))
    result = engine.run()
    assert result.intents and result.orders == ()
    assert len(result.rejected) == len(result.intents)


def test_a_strategy_that_never_trades_produces_a_flat_curve():
    result = _engine(Never()).run()
    assert result.orders == ()
    assert result.report.n_trades == 0
    assert result.report.sharpe is None, (
        "a flat curve has no Sharpe ratio; returning a number would put the "
        "strategy that never traded on the leaderboard")


# --- walk-forward ----------------------------------------------------------

def test_walk_forward_returns_only_out_of_sample_segments():
    bars = series(n=200)

    def make(data):
        return _engine(data=data, cfg=BacktestConfig(timeframe="1d", warmup_bars=2))

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


# --- metrics ---------------------------------------------------------------

def test_max_drawdown_against_a_hand_computed_curve():
    curve = [(T0, Decimal("100")), (T0, Decimal("120")), (T0, Decimal("90")),
             (T0, Decimal("150"))]
    dd, _ = perf.max_drawdown(curve)
    assert dd == pytest.approx(0.25)             # 120 -> 90


def test_a_flat_curve_has_no_sharpe_rather_than_infinite():
    curve = [(T0 + timedelta(days=i), Decimal("100")) for i in range(30)]
    report = perf.compute_report(curve, [], timeframe="1d")
    assert report.sharpe is None
    assert report.volatility == 0.0 or report.volatility is None
    assert report.max_drawdown == 0.0


def test_profit_factor_and_win_rate_against_known_trades():
    class T:
        def __init__(self, pnl):
            self.net_pnl = pnl
            self.gross_pnl = pnl
            self.instrument_key = INST.key
            self.closed_at = T0
    trades = [T(100.0), T(-50.0), T(200.0), T(-25.0)]
    curve = [(T0, Decimal("1000")), (T0 + timedelta(days=1), Decimal("1225"))]
    report = perf.compute_report(curve, trades, timeframe="1d")
    assert report.win_rate == pytest.approx(0.5)
    assert report.profit_factor == pytest.approx(300 / 75)
    assert report.average_win == pytest.approx(150.0)
    assert report.average_loss == pytest.approx(-37.5)
    assert report.expectancy == pytest.approx(56.25)
    assert report.best_trade == 200.0 and report.worst_trade == -50.0


def test_annualisation_differs_by_timeframe():
    daily = perf.periods_per_year("1d")
    hourly = perf.periods_per_year("1h")
    assert hourly > daily
    assert perf.periods_per_year("1d", continuous=True) > daily


def test_report_formats_undefined_metrics_as_not_available():
    curve = [(T0, Decimal("100")), (T0 + timedelta(days=1), Decimal("100"))]
    text = perf.compute_report(curve, [], timeframe="1d").format()
    assert "n/a" in text


def test_breakdowns_partition_the_trades():
    class T:
        def __init__(self, key, when):
            self.net_pnl = 1.0
            self.gross_pnl = 1.0
            self.instrument_key = key
            self.closed_at = when
    trades = [T("A", T0), T("B", T0), T("A", T0 + timedelta(days=40))]
    assert sum(len(v) for v in perf.by_instrument(trades).values()) == 3
    assert set(perf.by_instrument(trades)) == {"A", "B"}
    assert sum(len(v) for v in perf.by_period(trades, "M").values()) == 3
    assert len(perf.by_period(trades, "Y")) == 1
