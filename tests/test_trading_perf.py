"""Performance metrics.

Organised around the ways a performance report flatters rather than around its
API surface: costs quietly omitted, an undefined ratio reported as a number, an
annualisation factor borrowed from the wrong timeframe, a breakdown whose parts
do not add up to the whole.

Every metric here is checked against a fixture computed by hand in the
docstring, because a metrics module that is only tested against itself will
happily agree with its own arithmetic errors forever.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from olympus.trading import perf
from olympus.trading.contracts import Regime

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


class Trade:
    """The duck type `portfolio.TradeRecord` satisfies."""

    def __init__(self, gross, fees=0.0, *, key="NASDAQ:AAPL", closed=None,
                 opened=None):
        self.gross_pnl = gross
        self.fees = fees
        self.instrument_key = key
        self.closed_at = closed or T0
        self.opened_at = opened

    @property
    def net_pnl(self):
        return self.gross_pnl - self.fees


#: The hand-computed three-trade series every trade statistic is checked against.
#:
#:   gross P&L   +100    -40    +60     fees  10   5   5
#:   net   P&L    +90    -45    +55
#:
#: net : wins [90, 55] losses [-45] -> win rate 2/3, profit factor 145/45,
#:       average win 72.5, average loss -45, expectancy 100/3, best 90, worst -45
#: gross: wins [100, 60] losses [-40] -> win rate 2/3, profit factor 4.0,
#:       average win 80, average loss -40, expectancy 40, best 100, worst -40
THREE_TRADES = [
    Trade(100.0, 10.0, closed=T0 + timedelta(days=10),
          opened=T0 + timedelta(days=8)),
    Trade(-40.0, 5.0, closed=T0 + timedelta(days=20),
          opened=T0 + timedelta(days=16)),
    Trade(60.0, 5.0, closed=T0 + timedelta(days=30),
          opened=T0 + timedelta(days=27)),
]


def curve(values, *, step=timedelta(days=1), start=T0):
    return [(start + step * i, Decimal(str(v))) for i, v in enumerate(values)]


# --- undefined is None, never a number -------------------------------------

def test_a_flat_curve_has_no_sharpe_rather_than_infinity_or_nan():
    """Zero volatility makes the quotient undefined. A `nan` here would sort as
    neither greater nor less than any threshold, so it would silently pass every
    `sharpe > 1` filter downstream."""
    report = perf.compute_report(curve([100] * 30), [], timeframe="1d")
    assert report.sharpe is None
    assert report.sortino is None
    assert report.calmar is None
    assert report.volatility == 0.0
    assert report.max_drawdown == 0.0
    assert report.net.total_return == 0.0


def test_no_metric_is_ever_nan_or_inf():
    """The whole-report guarantee, not just Sharpe's."""
    for values in ([100] * 5, [100, 0.01, 100], [100, 200], [1, 1, 1, 1]):
        report = perf.compute_report(curve(values), THREE_TRADES, timeframe="1d")
        for basis in (report.gross, report.net):
            for name in ("annualised_return", "volatility", "sharpe", "sortino",
                         "calmar", "profit_factor", "expectancy"):
                value = getattr(basis, name)
                assert value is None or math.isfinite(value), (name, values)


def test_a_single_point_curve_produces_no_statistics_and_says_so():
    report = perf.compute_report(curve([100]), [], timeframe="1d")
    assert report.sharpe is None and report.volatility is None
    assert any("too short" in w for w in report.warnings)


def test_no_trades_leaves_trade_statistics_undefined_not_zero():
    """A win rate of 0.0 and a win rate of 'never traded' are different facts."""
    report = perf.compute_report(curve([100, 101]), [], timeframe="1d")
    assert report.win_rate is None
    assert report.profit_factor is None
    assert report.expectancy is None
    assert report.n_trades == 0


def test_a_curve_that_only_wins_has_no_profit_factor():
    trades = [Trade(10.0), Trade(20.0)]
    report = perf.compute_report(curve([100, 130]), trades, timeframe="1d")
    assert report.profit_factor is None, (
        "dividing by zero losses gives infinity, which ranks a two-trade "
        "sample above every real strategy")
    assert report.average_loss is None


# --- gross and net, structurally -------------------------------------------

def test_a_report_cannot_be_built_with_only_one_cost_basis():
    """The structural half of the requirement: `gross` and `net` have no
    defaults, so there is no way to construct a half-report."""
    with pytest.raises(TypeError):
        perf.PerformanceReport()                     # type: ignore[call-arg]
    with pytest.raises(TypeError):
        perf.PerformanceReport(gross=perf.ReturnStats())  # type: ignore[call-arg]


def test_gross_and_net_differ_by_exactly_the_costs():
    """1000 -> 1090 net after 30 of fees means the price move alone made 120."""
    trades = [Trade(50.0, 10.0, closed=T0 + timedelta(days=1)),
              Trade(70.0, 20.0, closed=T0 + timedelta(days=2))]
    report = perf.compute_report(curve([1000, 1050, 1090]), trades,
                                 timeframe="1d")
    assert report.net.total_return == pytest.approx(0.09)
    assert report.gross.total_return == pytest.approx(0.12)
    assert report.total_fees == Decimal("30")


def test_costs_are_added_back_chronologically_not_at_the_end():
    """A fee paid at the trough deepened that trough.

    Adding the total back at the final point would shift the whole curve
    uniformly and report an identical gross and net drawdown, which is wrong.
    """
    trades = [Trade(0.0, 50.0, closed=T0 + timedelta(days=1))]
    report = perf.compute_report(curve([1000, 900, 1000]), trades,
                                 timeframe="1d")
    assert report.gross.max_drawdown < report.net.max_drawdown


def test_both_bases_appear_in_the_formatted_table():
    text = perf.compute_report(curve([100, 110]), THREE_TRADES,
                               timeframe="1d").format()
    assert "gross" in text and "net" in text
    assert "n/a" in text                             # undefined printed honestly


def test_the_flat_accessors_report_net_not_gross():
    """Net is what the account experiences, so the unqualified name means net."""
    report = perf.compute_report(curve([1000, 1090]), THREE_TRADES,
                                 timeframe="1d")
    assert report.total_return_net == report.net.total_return
    assert report.sharpe == report.net.sharpe
    assert report.expectancy == report.net.expectancy


# --- trade statistics, against the hand-computed fixture -------------------

def test_net_trade_statistics_match_the_hand_computed_series():
    report = perf.compute_report(curve([1000, 1100]), THREE_TRADES,
                                 timeframe="1d")
    net = report.net
    assert net.win_rate == pytest.approx(2 / 3)
    assert net.profit_factor == pytest.approx(145 / 45)
    assert net.average_win == pytest.approx(72.5)
    assert net.average_loss == pytest.approx(-45.0)
    assert net.expectancy == pytest.approx(100 / 3)
    assert net.best_trade == pytest.approx(90.0)
    assert net.worst_trade == pytest.approx(-45.0)
    assert net.total_pnl == pytest.approx(100.0)


def test_gross_trade_statistics_match_the_hand_computed_series():
    report = perf.compute_report(curve([1000, 1100]), THREE_TRADES,
                                 timeframe="1d")
    gross = report.gross
    assert gross.win_rate == pytest.approx(2 / 3)
    assert gross.profit_factor == pytest.approx(4.0)
    assert gross.average_win == pytest.approx(80.0)
    assert gross.average_loss == pytest.approx(-40.0)
    assert gross.expectancy == pytest.approx(40.0)
    assert gross.best_trade == pytest.approx(100.0)
    assert gross.worst_trade == pytest.approx(-40.0)


def test_average_holding_period_is_the_mean_of_the_round_trips():
    """2, 4 and 3 days held -> 3 days on average."""
    report = perf.compute_report(curve([1000, 1100]), THREE_TRADES,
                                 timeframe="1d")
    assert report.avg_holding_period_s == pytest.approx(3 * 86400)


def test_holding_period_is_undefined_when_no_trade_recorded_an_open():
    assert perf.average_holding_period([Trade(1.0), Trade(2.0)]) is None


def test_a_trade_exposing_only_gross_reports_the_same_on_both_bases():
    """Honest rather than flattering: unknown costs are not assumed to be zero
    on the net side only."""
    class GrossOnly:
        gross_pnl = 25.0
        instrument_key = "X"
        closed_at = T0

    report = perf.compute_report(curve([100, 125]), [GrossOnly()],
                                 timeframe="1d")
    assert report.net.expectancy == report.gross.expectancy == 25.0


# --- drawdown ---------------------------------------------------------------

def test_max_drawdown_against_a_hand_computed_curve():
    """100 -> 120 -> 90 -> 150: the deepest fall is 120 -> 90, i.e. 25%."""
    depth, bars = perf.max_drawdown(curve([100, 120, 90, 150]))
    assert depth == pytest.approx(0.25)
    assert bars == 1                                 # peak at index 1, trough 2


def test_drawdown_reports_peak_trough_and_recovery():
    """100 -> 80 -> 90 -> 60 -> 100: worst is 100 -> 60 (40%), three bars to the
    trough and four to full recovery."""
    stats = perf.drawdown_stats(curve([100, 80, 90, 60, 100]))
    assert stats.depth == pytest.approx(0.40)
    assert stats.peak_index == 0 and stats.trough_index == 3
    assert stats.bars_to_trough == 3
    assert stats.recovery_index == 4
    assert stats.bars_to_recovery == 4
    assert stats.seconds_to_trough == pytest.approx(3 * 86400)


def test_an_unrecovered_drawdown_says_so_rather_than_reporting_a_recovery():
    """The single most important thing a drawdown statistic can say."""
    stats = perf.drawdown_stats(curve([100, 120, 60, 70, 80]))
    assert stats.recovery_index is None
    assert stats.bars_to_recovery is None
    report = perf.compute_report(curve([100, 120, 60, 70, 80]), [],
                                 timeframe="1d")
    assert report.net.max_drawdown_recovered is False


def test_the_longest_underwater_stretch_is_not_the_deepest_one():
    """A shallow drawdown that never ends is the one that gets a strategy
    switched off, so it is reported separately from the deepest."""
    values = [100, 60, 100, 99, 99, 99, 99, 99, 99]
    stats = perf.drawdown_stats(curve(values))
    assert stats.depth == pytest.approx(0.40)        # deepest: 100 -> 60
    assert stats.bars_to_trough == 1
    assert stats.longest_underwater_bars == 6        # index 2 -> 8


def test_a_monotonically_rising_curve_has_no_drawdown():
    stats = perf.drawdown_stats(curve([100, 101, 102, 103]))
    assert stats.depth == 0.0 and stats.bars_to_trough == 0
    assert stats.longest_underwater_bars == 0


def test_drawdown_of_an_empty_or_single_point_curve_is_zero_not_an_error():
    assert perf.drawdown_stats([]).depth == 0.0
    assert perf.drawdown_stats(curve([100])).depth == 0.0


# --- annualisation ----------------------------------------------------------

def test_a_daily_bar_annualises_at_the_number_of_trading_days():
    """The regression: a session-seconds year divided by 86400 gives 68.25
    bars/year, understating a daily strategy's volatility by ~1.9x and
    inflating its Sharpe by the same factor."""
    assert perf.periods_per_year("1d") == pytest.approx(252.0)


def test_intraday_bars_count_only_session_time():
    assert perf.periods_per_year("1h") == pytest.approx(252 * 6.5)
    assert perf.periods_per_year("5m") == pytest.approx(252 * 78)
    assert perf.periods_per_year("1m") == pytest.approx(252 * 390)


def test_weekly_and_monthly_bars_convert_to_trading_days():
    assert perf.periods_per_year("1w") == pytest.approx(252 / 5)
    assert perf.periods_per_year("30d") == pytest.approx(12.0)


def test_a_continuous_venue_uses_calendar_time():
    """Annualising a crypto day on a 6.5-hour session would overstate
    volatility by roughly 1.9x."""
    assert perf.periods_per_year("1d", continuous=True) == pytest.approx(365.25)
    assert perf.periods_per_year("1h", continuous=True) == pytest.approx(8766.0)


def test_shorter_timeframes_always_have_more_bars_per_year():
    order = ["1m", "5m", "1h", "1d", "1w"]
    counts = [perf.periods_per_year(t) for t in order]
    assert counts == sorted(counts, reverse=True)


def test_the_annualisation_factor_is_recorded_in_the_report():
    """So a reader can check the assumption instead of trusting it."""
    report = perf.compute_report(curve([100, 110]), [], timeframe="1h")
    assert report.periods_per_year == pytest.approx(252 * 6.5)
    assert report.timeframe == "1h"


def test_annualising_from_a_handful_of_bars_is_flagged():
    report = perf.compute_report(curve([100, 101, 102]), [], timeframe="1d")
    assert any("annualised from" in w for w in report.warnings)


def test_annualised_return_compounds_the_total_over_the_year_fraction():
    """252 daily bars of +0.1% each: (1.001 ** 252) - 1 ~= 28.6%, and one year
    of data means the annualised figure equals the total."""
    values = [1000.0]
    for _ in range(252):
        values.append(values[-1] * 1.001)
    report = perf.compute_report(curve(values), [], timeframe="1d")
    assert report.net.total_return == pytest.approx(1.001 ** 252 - 1, rel=1e-6)
    assert report.net.annualised_return == pytest.approx(
        report.net.total_return, rel=1e-6)


# --- ratios -----------------------------------------------------------------

def test_sharpe_matches_the_textbook_formula():
    values = [100.0, 102.0, 101.0, 104.0, 103.0, 106.0]
    report = perf.compute_report(curve(values), [], timeframe="1d")
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    expected = mean / math.sqrt(var) * math.sqrt(252.0)
    assert report.sharpe == pytest.approx(expected)


def test_the_risk_free_rate_is_de_annualised_before_it_is_subtracted():
    """Deducting a 4% annual rate from every single bar would turn every
    strategy into a catastrophe."""
    values = [100.0, 102.0, 101.0, 104.0, 103.0, 106.0]
    free = perf.compute_report(curve(values), [], timeframe="1d")
    charged = perf.compute_report(curve(values), [], timeframe="1d",
                                  risk_free_rate=0.04)
    assert charged.sharpe < free.sharpe
    assert charged.sharpe > free.sharpe - 1.0


def test_sortino_ignores_upside_volatility():
    """A curve that only ever rises has no downside deviation, so Sortino is
    undefined rather than infinite — and it is never below Sharpe when the
    losses are the smaller half of the dispersion."""
    rising = perf.compute_report(curve([100, 101, 103, 104, 108]), [],
                                 timeframe="1d")
    assert rising.sortino is None
    mixed = perf.compute_report(curve([100, 104, 101, 106, 103, 110]), [],
                                timeframe="1d")
    assert mixed.sortino is not None and mixed.sharpe is not None
    assert mixed.sortino > mixed.sharpe


def test_calmar_is_annualised_return_over_max_drawdown():
    values = [100, 120, 90, 150, 140]
    report = perf.compute_report(curve(values), [], timeframe="1d")
    assert report.calmar == pytest.approx(
        report.net.annualised_return / report.net.max_drawdown)


def test_volatility_is_annualised_from_the_period_deviation():
    values = [100.0, 102.0, 101.0, 104.0, 103.0]
    report = perf.compute_report(curve(values), [], timeframe="1d")
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    assert report.volatility == pytest.approx(sd * math.sqrt(252.0))


def test_a_wiped_out_account_does_not_manufacture_a_return():
    """Dividing by zero equity would invent a percentage from nothing."""
    report = perf.compute_report(curve([100, 0, 0, 50]), [], timeframe="1d")
    assert report.net.total_return == pytest.approx(-0.5)
    assert report.sharpe is None or math.isfinite(report.sharpe)


# --- exposure, turnover, costs ---------------------------------------------

def test_exposure_and_turnover_are_carried_verbatim():
    report = perf.compute_report(curve([100, 110]), [], timeframe="1d",
                                 exposure=0.42, turnover=3.5,
                                 fees=Decimal("12"), slippage=Decimal("8"))
    assert report.exposure == 0.42
    assert report.turnover == 3.5
    assert report.total_fees == Decimal("12")
    assert report.total_slippage == Decimal("8")
    assert report.total_costs == Decimal("20")


# --- breakdowns -------------------------------------------------------------

def _mixed_trades():
    return [
        Trade(100.0, 10.0, key="A", closed=T0 + timedelta(days=5)),
        Trade(-40.0, 5.0, key="B", closed=T0 + timedelta(days=40)),
        Trade(60.0, 5.0, key="A", closed=T0 + timedelta(days=70)),
        Trade(-10.0, 2.0, key="B", closed=T0 + timedelta(days=100)),
    ]


def test_by_instrument_returns_a_report_per_instrument():
    reports = perf.by_instrument(_mixed_trades(), starting_equity=1000)
    assert set(reports) == {"A", "B"}
    assert all(isinstance(r, perf.PerformanceReport) for r in reports.values())
    assert reports["A"].net.win_rate == 1.0
    assert reports["B"].net.win_rate == 0.0


def test_the_instrument_breakdown_sums_back_to_the_whole():
    trades = _mixed_trades()
    whole = perf.compute_report(curve([1000, 1108]), trades, timeframe="1d")
    parts = perf.by_instrument(trades, starting_equity=1000)
    assert sum(r.n_trades for r in parts.values()) == whole.n_trades
    total = sum(r.net.total_pnl for r in parts.values())
    assert total == pytest.approx(whole.net.total_pnl)
    gross_total = sum(r.gross.total_pnl for r in parts.values())
    assert gross_total == pytest.approx(whole.gross.total_pnl)


def test_the_regime_breakdown_sums_back_to_the_whole():
    trades = _mixed_trades()
    regimes = {T0: Regime.RANGING,
               T0 + timedelta(days=50): Regime.TRENDING_UP}
    parts = perf.by_regime(trades, regimes, starting_equity=1000)
    assert set(parts) == {"ranging", "trending_up"}
    assert sum(r.n_trades for r in parts.values()) == len(trades)
    assert sum(r.net.total_pnl for r in parts.values()) == pytest.approx(
        sum(t.net_pnl for t in trades))


def test_regimes_are_looked_up_as_of_not_by_exact_timestamp():
    """A regime series is sampled per bar; a trade closes on a fill timestamp.
    Requiring an exact key match would label almost everything 'unknown' — a
    breakdown that looks populated and is entirely wrong."""
    trades = [Trade(10.0, closed=T0 + timedelta(days=3, hours=7))]
    regimes = {T0: Regime.CRISIS, T0 + timedelta(days=3): Regime.TRENDING_UP}
    assert set(perf.by_regime(trades, regimes)) == {"trending_up"}


def test_trades_before_any_known_regime_are_labelled_unknown_not_dropped():
    trades = [Trade(10.0, closed=T0 - timedelta(days=1))]
    parts = perf.by_regime(trades, {T0: Regime.RANGING})
    assert set(parts) == {"unknown"}
    assert parts["unknown"].n_trades == 1


def test_a_regime_callable_is_accepted_as_well_as_a_mapping():
    trades = _mixed_trades()
    parts = perf.by_regime(trades, lambda t: Regime.CRISIS)
    assert set(parts) == {"crisis"}


def test_by_period_partitions_the_equity_curve_by_calendar_bucket():
    values = [1000 + i for i in range(120)]
    equity = curve(values)
    months = perf.by_period(equity, "M")
    quarters = perf.by_period(equity, "Q")
    years = perf.by_period(equity, "Y")
    assert set(months) == {"2026-01", "2026-02", "2026-03", "2026-04", "2026-05"}
    assert set(quarters) == {"2026-Q1", "2026-Q2"}
    assert set(years) == {"2026"}
    assert sum(r.n_bars for r in years.values()) == len(equity)


def test_period_buckets_compound_back_to_the_whole_period_return():
    """The boundary return belongs to the month that earned it, so the buckets
    must multiply out to the total — otherwise every month silently drops its
    first bar's return."""
    values = [1000.0]
    for i in range(90):
        values.append(values[-1] * (1.002 if i % 3 else 0.999))
    equity = curve(values)
    whole = perf.compute_report(equity, [], timeframe="1d")
    product = 1.0
    for report in perf.by_period(equity, "M").values():
        product *= (1.0 + report.net.total_return)
    assert product - 1.0 == pytest.approx(whole.net.total_return, rel=1e-9)


def test_period_keys_are_sortable_strings():
    assert perf.period_key(T0, "M") == "2026-01"
    assert perf.period_key(T0, "Q") == "2026-Q1"
    assert perf.period_key(T0, "Y") == "2026"
    assert perf.period_key(datetime(2026, 11, 2, tzinfo=timezone.utc), "Q") == "2026-Q4"


def test_an_unknown_granularity_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        perf.period_key(T0, "W")


def test_breakdown_reports_disclose_that_they_use_trade_pnl_not_marks():
    """A slice has no marked-to-market curve of its own; saying so is the
    difference between a caveat and a misrepresentation."""
    reports = perf.by_instrument(_mixed_trades(), starting_equity=1000)
    assert all(any("closed-trade" in w for w in r.warnings)
               for r in reports.values())


def test_a_slice_with_no_closed_trades_still_produces_a_report():
    """So callers never have to special-case 'traded but never closed'."""
    class Open:
        gross_pnl = 0.0
        net_pnl = 0.0
        fees = 0.0
        instrument_key = "Z"
        closed_at = None

    reports = perf.by_instrument([Open()])
    assert set(reports) == {"Z"}
    assert reports["Z"].n_trades == 1
    assert reports["Z"].sharpe is None


# --- serialisation ----------------------------------------------------------

def test_the_report_serialises_to_json_safe_values():
    import json
    report = perf.compute_report(curve([1000, 1100]), THREE_TRADES,
                                 timeframe="1d", fees=Decimal("20"),
                                 slippage=Decimal("5"))
    data = report.to_dict()
    json.dumps(data)                                 # must not raise
    assert data["total_fees"] == "20"                # Decimal as string
    assert set(data["gross"]) == set(data["net"])
    assert data["net"]["total_return"] == pytest.approx(0.10)


def test_an_empty_report_still_carries_both_bases():
    report = perf.PerformanceReport.empty(label="nothing")
    assert isinstance(report.gross, perf.ReturnStats)
    assert isinstance(report.net, perf.ReturnStats)
    assert "gross" in report.format() and "net" in report.format()


def test_drawdown_stats_serialise():
    import json
    json.dumps(perf.drawdown_stats(curve([100, 80, 100])).to_dict())
