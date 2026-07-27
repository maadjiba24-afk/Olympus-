"""Performance metrics.

Pure stdlib, and deliberately unforgiving about three things.

**Gross and net are reported together, always.** `PerformanceReport` takes
`gross` and `net` as its first two *required* fields — there is no way to build
one that carries only a single cost basis, and `format()` prints them as two
columns side by side. This is structural rather than advisory because the most
common way a strategy report misleads is by quoting returns before costs. A
strategy that makes 8% gross and −2% net is a losing strategy, and any API that
lets you print only the first number will eventually be used to do exactly that.

**Undefined is `None`, never a number.** A flat equity curve has no Sharpe
ratio — zero volatility makes the quotient undefined, not infinite. Returning
`inf`, `nan`, or `0.0` there produces a leaderboard where the strategy that
never traded looks competitive. Every ratio here returns `None` when its
denominator vanishes, and the formatter prints "n/a".

**Annualisation is derived from the timeframe**, not hard-coded to 252, so a
5-minute strategy is not silently annualised as if it traded daily. The factor
is `periods_per_year(timeframe)` and it is written into the report's label path
rather than left implicit.

Numeric domains follow `contracts`: money is `Decimal` (starting/ending equity,
fees, slippage), ratios and returns are `float`. The conversion happens once,
at the boundary of each computation, because a Sharpe ratio computed in Decimal
is false precision and a fee total computed in float is a reconciliation bug.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .contracts import jsonable

#: Trading periods per year, by convention. Approximate by nature — the point
#: is proportionality between timeframes, and being explicit about the
#: assumption beats an unexplained 252 buried in a division.
TRADING_DAYS_PER_YEAR = 252
SESSION_SECONDS = 6.5 * 3600                  # a US cash-equity session
_CALENDAR_SECONDS_PER_YEAR = 365.25 * 24 * 3600
_DAY_SECONDS = 24 * 3600

#: Below this many years of data, annualising a return extrapolates a handful of
#: bars across a whole year. The number is still computed (refusing would be
#: worse — callers would reimplement it), but the report says so.
_ANNUALISATION_WARN_YEARS = 1.0 / 12.0

_ZERO = Decimal("0")


def periods_per_year(timeframe: str, *, continuous: bool = False) -> float:
    """Bars per year for `timeframe`.

    Three regimes, because one division does not cover them:

    * **24/7 venues** (`continuous=True`) — calendar time throughout. A daily
      crypto bar occurs 365.25 times a year.
    * **Intraday bars on a session venue** — bars occur only while the session
      is open, so the count is `sessions_per_year × bars_per_session`. An hourly
      bar occurs 252 × 6.5 = 1638 times a year, not 8766.
    * **Daily and longer bars on a session venue** — the bar spans calendar
      time but only exists on trading days, so the span is converted to trading
      days first. A daily bar occurs 252 times a year; a weekly bar covers ~5
      trading days and so occurs ~50 times.

    The middle and last cases must not share a formula. Dividing a
    session-seconds year by a daily bar's 86400 seconds gives 68.25 bars/year —
    which understates a daily strategy's annualised volatility by a factor of
    1.9 and inflates its Sharpe by the same. Getting this wrong is not a
    rounding error, it is the difference between a Sharpe of 1.0 and 1.9 on
    identical returns.
    """
    from .instruments import timeframe_seconds
    seconds = timeframe_seconds(timeframe)
    if seconds <= 0:
        raise ValueError("timeframe must be positive")
    if continuous:
        return _CALENDAR_SECONDS_PER_YEAR / seconds
    if seconds < _DAY_SECONDS:
        return TRADING_DAYS_PER_YEAR * (SESSION_SECONDS / seconds)
    calendar_days = seconds / _DAY_SECONDS
    trading_days = max(1, round(calendar_days * TRADING_DAYS_PER_YEAR / 365.25))
    return TRADING_DAYS_PER_YEAR / trading_days


# ---------------------------------------------------------------------------
# small numeric helpers — each one exists to make a divide-by-zero impossible
# ---------------------------------------------------------------------------

def _f(value: Any) -> float:
    """Float from Decimal/int/float/str without going through binary noise."""
    return float(value)


def _finite(value: float | None) -> float | None:
    """`None` for anything that is not a real number.

    Every ratio in this module passes through here. `inf` and `nan` are the two
    values that survive a division nobody guarded and then poison every
    downstream comparison silently — a `nan` Sharpe sorts as neither greater
    nor less than anything, so it never fails a `> threshold` check.
    """
    if value is None:
        return None
    return value if math.isfinite(value) else None


def _returns(curve: Sequence[tuple[Any, Any]]) -> list[float]:
    """Simple period returns from an equity curve.

    Skips steps whose *starting* equity is non-positive: a wiped-out account has
    no meaningful percentage return, and dividing by it would manufacture one.
    """
    out: list[float] = []
    for i in range(1, len(curve)):
        prev = _f(curve[i - 1][1])
        cur = _f(curve[i][1])
        if prev <= 0:
            continue
        out.append(cur / prev - 1.0)
    return out


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: Sequence[float]) -> float | None:
    """Sample standard deviation, or `None` when fewer than two observations.

    One observation has no dispersion; reporting 0.0 there would then divide
    into a Sharpe of infinity.
    """
    if len(xs) < 2:
        return None
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def _downside_deviation(xs: Sequence[float], target: float) -> float | None:
    """Textbook downside deviation: RMS of the shortfalls below `target`.

    The denominator is the *full* sample, not just the losing periods. Using
    only the losers (a common shortcut) computes the dispersion of losses rather
    than the risk of loss, and makes a strategy that loses rarely-but-hugely
    look safer than one that loses often-and-mildly.
    """
    if len(xs) < 2:
        return None
    shortfalls = [min(x - target, 0.0) ** 2 for x in xs]
    return math.sqrt(sum(shortfalls) / len(xs))


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DrawdownStats:
    """The full shape of the worst decline, not just its depth.

    Depth alone is not a risk measure. A 20% fall recovered in three bars and a
    20% fall still unrecovered after two years are different businesses, so the
    time axis is reported next to the depth rather than discarded.
    """
    depth: float = 0.0                      # fraction in [0, 1]
    peak_index: int = 0
    trough_index: int = 0
    #: Index at which the prior peak was regained. `None` == never recovered,
    #: which is the single most important thing a drawdown stat can say.
    recovery_index: int | None = None
    bars_to_trough: int = 0
    bars_to_recovery: int | None = None
    seconds_to_trough: float = 0.0
    seconds_to_recovery: float | None = None
    #: Longest stretch spent below a prior peak, anywhere in the curve. Often a
    #: shallower drawdown than the deepest one, and often the one that would
    #: have got the strategy switched off.
    longest_underwater_bars: int = 0

    def to_dict(self) -> dict:
        return {f.name: jsonable(getattr(self, f.name)) for f in fields(self)}


def drawdown_stats(curve: Sequence[tuple[Any, Any]]) -> DrawdownStats:
    """Deepest peak-to-trough decline and the time it took, both ways."""
    if len(curve) < 2:
        return DrawdownStats()

    peak = _f(curve[0][1])
    peak_i = 0
    worst = 0.0
    worst_peak_i = 0
    worst_trough_i = 0
    longest_underwater = 0
    underwater_from: int | None = None

    for i in range(len(curve)):
        value = _f(curve[i][1])
        if value >= peak:
            peak = value
            peak_i = i
            if underwater_from is not None:
                longest_underwater = max(longest_underwater, i - underwater_from)
                underwater_from = None
            continue
        if underwater_from is None:
            underwater_from = peak_i
        if peak > 0:
            drop = (peak - value) / peak
            if drop > worst:
                worst = drop
                worst_peak_i = peak_i
                worst_trough_i = i
    if underwater_from is not None:
        longest_underwater = max(longest_underwater, len(curve) - 1 - underwater_from)

    recovery_i: int | None = None
    if worst > 0:
        peak_value = _f(curve[worst_peak_i][1])
        for j in range(worst_trough_i + 1, len(curve)):
            if _f(curve[j][1]) >= peak_value:
                recovery_i = j
                break

    def _elapsed(a: int, b: int) -> float:
        ta, tb = curve[a][0], curve[b][0]
        if isinstance(ta, datetime) and isinstance(tb, datetime):
            return (tb - ta).total_seconds()
        return 0.0

    return DrawdownStats(
        depth=worst, peak_index=worst_peak_i, trough_index=worst_trough_i,
        recovery_index=recovery_i,
        bars_to_trough=worst_trough_i - worst_peak_i,
        bars_to_recovery=(recovery_i - worst_peak_i) if recovery_i is not None else None,
        seconds_to_trough=_elapsed(worst_peak_i, worst_trough_i),
        seconds_to_recovery=(_elapsed(worst_peak_i, recovery_i)
                             if recovery_i is not None else None),
        longest_underwater_bars=longest_underwater)


def max_drawdown(curve: Sequence[tuple[Any, Any]]) -> tuple[float, int]:
    """`(depth_fraction, bars_from_peak_to_trough)` — the short form."""
    stats = drawdown_stats(curve)
    return stats.depth, stats.bars_to_trough


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReturnStats:
    """Every metric whose value changes depending on whether costs are deducted.

    Two of these hang off one `PerformanceReport` — gross and net — so the pair
    is always carried together and always computed the same way.
    """
    total_return: float = 0.0
    annualised_return: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float = 0.0
    max_drawdown_bars: int = 0
    max_drawdown_seconds: float = 0.0
    max_drawdown_recovered: bool = True
    calmar: float | None = None

    # trade-level statistics (P&L per closed round trip)
    win_rate: float | None = None
    profit_factor: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    expectancy: float | None = None
    best_trade: float | None = None
    worst_trade: float | None = None
    total_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {f.name: jsonable(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True)
class PerformanceReport:
    """Every required metric, gross and net.

    `gross` and `net` have no defaults on purpose: you cannot construct a report
    that quotes one without the other. `None` inside them means *undefined*, not
    zero — see the module docstring.
    """
    gross: ReturnStats
    net: ReturnStats

    n_bars: int = 0
    n_trades: int = 0
    start: datetime | None = None
    end: datetime | None = None
    starting_equity: Decimal = _ZERO
    ending_equity: Decimal = _ZERO

    #: Traded notional divided by average equity over the window. Unitless; 2.0
    #: means the book was turned over twice.
    turnover: float = 0.0
    #: Fraction of bars with a non-flat position. A strategy with a great Sharpe
    #: and 2% exposure has mostly been holding cash.
    exposure: float = 0.0
    total_fees: Decimal = _ZERO
    #: Modelled execution shortfall: |fill price − reference price| × quantity.
    total_slippage: Decimal = _ZERO
    #: Mean seconds a round trip was held. `None` when no trade recorded both
    #: an open and a close time.
    avg_holding_period_s: float | None = None

    timeframe: str = ""
    periods_per_year: float = 0.0
    risk_free_rate: float = 0.0
    label: str = ""
    warnings: tuple[str, ...] = ()

    # -- backwards-compatible flat accessors ------------------------------
    # Net is the default view because net is what the account experiences. The
    # gross number always exists beside it and is never the unqualified answer.

    @property
    def total_return_net(self) -> float:
        return self.net.total_return

    @property
    def total_return_gross(self) -> float:
        return self.gross.total_return

    @property
    def annualised_return(self) -> float | None:
        return self.net.annualised_return

    @property
    def volatility(self) -> float | None:
        return self.net.volatility

    @property
    def sharpe(self) -> float | None:
        return self.net.sharpe

    @property
    def sortino(self) -> float | None:
        return self.net.sortino

    @property
    def max_drawdown(self) -> float:
        return self.net.max_drawdown

    @property
    def max_drawdown_bars(self) -> int:
        return self.net.max_drawdown_bars

    @property
    def calmar(self) -> float | None:
        return self.net.calmar

    @property
    def win_rate(self) -> float | None:
        return self.net.win_rate

    @property
    def profit_factor(self) -> float | None:
        return self.net.profit_factor

    @property
    def average_win(self) -> float | None:
        return self.net.average_win

    @property
    def average_loss(self) -> float | None:
        return self.net.average_loss

    @property
    def expectancy(self) -> float | None:
        return self.net.expectancy

    @property
    def best_trade(self) -> float | None:
        return self.net.best_trade

    @property
    def worst_trade(self) -> float | None:
        return self.net.worst_trade

    @property
    def total_costs(self) -> Decimal:
        return self.total_fees + self.total_slippage

    @classmethod
    def empty(cls, *, label: str = "", timeframe: str = "",
              warnings: Sequence[str] = ()) -> "PerformanceReport":
        """A report over no data. Still carries both cost bases."""
        return cls(gross=ReturnStats(), net=ReturnStats(), label=label,
                   timeframe=timeframe, warnings=tuple(warnings))

    def to_dict(self) -> dict:
        out: dict = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = (value.to_dict() if isinstance(value, ReturnStats)
                           else jsonable(value))
        return out

    def format(self) -> str:
        """A two-column text table: gross beside net, never one alone."""
        def num(value, pct=False):
            if value is None:
                return "n/a"
            if isinstance(value, Decimal):
                return f"{value:,.2f}"
            return f"{value * 100:,.2f}%" if pct else f"{value:,.3f}"

        paired = [
            ("Total return", "total_return", True),
            ("Annualised return", "annualised_return", True),
            ("Volatility (ann.)", "volatility", True),
            ("Sharpe", "sharpe", False),
            ("Sortino", "sortino", False),
            ("Max drawdown", "max_drawdown", True),
            ("Calmar", "calmar", False),
            ("Profit factor", "profit_factor", False),
            ("Win rate", "win_rate", True),
            ("Average win", "average_win", False),
            ("Average loss", "average_loss", False),
            ("Expectancy", "expectancy", False),
            ("Best trade", "best_trade", False),
            ("Worst trade", "worst_trade", False),
        ]
        single = [
            ("Bars", str(self.n_bars)),
            ("Trades", str(self.n_trades)),
            ("Max DD bars (net)", str(self.net.max_drawdown_bars)),
            ("Avg holding (s)", num(self.avg_holding_period_s)),
            ("Turnover", num(self.turnover)),
            ("Exposure", num(self.exposure, pct=True)),
            ("Fees", num(self.total_fees)),
            ("Slippage", num(self.total_slippage)),
            ("Starting equity", num(self.starting_equity)),
            ("Ending equity", num(self.ending_equity)),
        ]

        names = [n for n, _, _ in paired] + [n for n, _ in single]
        width = max(len(n) for n in names)
        lines = [self.label or "performance",
                 f"{'':<{width}}  {'gross':>12}  {'net':>12}",
                 "-" * (width + 28)]
        for name, attr, pct in paired:
            g = num(getattr(self.gross, attr), pct=pct)
            n = num(getattr(self.net, attr), pct=pct)
            lines.append(f"{name:<{width}}  {g:>12}  {n:>12}")
        lines.append("-" * (width + 28))
        for name, value in single:
            lines.append(f"{name:<{width}}  {value:>12}")
        for warning in self.warnings:
            lines.append(f"! {warning}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# trade extraction — duck-typed so `portfolio.TradeRecord` drops straight in
# ---------------------------------------------------------------------------

def _trade_pnl(trade: Any, *, net: bool) -> float:
    """P&L of one closed round trip on the requested cost basis.

    Falls back gracefully: an object exposing only `gross_pnl` reports the same
    number on both bases, which is honest (its costs are unknown) rather than
    silently flattering.
    """
    if net:
        value = getattr(trade, "net_pnl", None)
        if value is None:
            value = getattr(trade, "gross_pnl", 0)
    else:
        value = getattr(trade, "gross_pnl", None)
        if value is None:
            value = getattr(trade, "net_pnl", 0)
    return _f(value)


def _trade_stats(trades: Sequence[Any], *, net: bool) -> dict:
    pnls = [_trade_pnl(t, net=net) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "profit_factor": _finite(gross_win / gross_loss) if gross_loss > 0 else None,
        "average_win": _mean(wins) if wins else None,
        "average_loss": _mean(losses) if losses else None,
        "expectancy": _mean(pnls) if pnls else None,
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "total_pnl": sum(pnls),
    }


def average_holding_period(trades: Sequence[Any]) -> float | None:
    """Mean seconds held, over the trades that recorded an open time."""
    spans = []
    for t in trades:
        opened = getattr(t, "opened_at", None)
        closed = getattr(t, "closed_at", None)
        if isinstance(opened, datetime) and isinstance(closed, datetime):
            spans.append((closed - opened).total_seconds())
    return _mean(spans) if spans else None


# ---------------------------------------------------------------------------
# gross reconstruction
# ---------------------------------------------------------------------------

def _cost_events(trades: Sequence[Any]) -> list[tuple[datetime, float]]:
    """`(when, cost)` pairs from trades that recorded their fees."""
    events = []
    for t in trades:
        when = getattr(t, "closed_at", None)
        fee = getattr(t, "fees", None)
        if isinstance(when, datetime) and fee is not None:
            events.append((when, _f(fee)))
    events.sort(key=lambda e: e[0])
    return events


def _gross_curve(curve: Sequence[tuple[Any, Any]],
                 cost_curve: Sequence[tuple[Any, Any]] | None,
                 cost_events: Sequence[tuple[datetime, float]],
                 residual: float) -> list[tuple[Any, float]]:
    """The equity curve costs would never have touched.

    Gross equity at time *t* is net equity plus every cost charged up to *t* —
    not net equity plus the total charged over the whole run. The distinction
    matters for drawdown: adding all costs back at the end would shift the curve
    uniformly and leave the drawdown unchanged, which is wrong, because a fee
    paid at the trough deepened that trough.

    `cost_curve` (cumulative costs, from the backtester) wins when supplied;
    otherwise cumulative fees are rebuilt from the trades. `residual` is any
    cost the timeline could not place — charged at the end, which is the
    conservative direction for gross.
    """
    if not curve:
        return []
    if cost_curve:
        stamps = [row[0] for row in cost_curve]
        cumulative = [_f(row[1]) for row in cost_curve]
    else:
        stamps = [ts for ts, _ in cost_events]
        cumulative = []
        running = 0.0
        for _, cost in cost_events:
            running += cost
            cumulative.append(running)
    placed = cumulative[-1] if cumulative else 0.0
    tail = max(0.0, residual - placed)

    out: list[tuple[Any, float]] = []
    for ts, value in curve:
        added = 0.0
        if cumulative and isinstance(ts, datetime) and stamps:
            index = bisect.bisect_right(stamps, ts) - 1
            if index >= 0:
                added = cumulative[index]
        out.append((ts, _f(value) + added))
    if tail and out:
        ts, value = out[-1]
        out[-1] = (ts, value + tail)
    return out


# ---------------------------------------------------------------------------
# the computation
# ---------------------------------------------------------------------------

def _curve_stats(curve: Sequence[tuple[Any, Any]], trades: Sequence[Any], *,
                 net: bool, ppy: float, risk_free_rate: float,
                 warnings: list[str]) -> ReturnStats:
    rets = _returns(curve)
    start_eq = _f(curve[0][1]) if curve else 0.0
    end_eq = _f(curve[-1][1]) if curve else 0.0

    total = (end_eq / start_eq - 1.0) if start_eq > 0 else 0.0

    sd = _stdev(rets)
    vol = _finite(sd * math.sqrt(ppy)) if sd is not None else None
    mu = _mean(rets)
    #: The risk-free rate arrives annualised; de-annualise it to per-bar before
    #: subtracting, or a 4% rate would be deducted from every single bar.
    rf_per_bar = risk_free_rate / ppy if ppy > 0 else 0.0

    annualised = None
    if rets and start_eq > 0 and (1.0 + total) > 0 and ppy > 0:
        years = len(rets) / ppy
        if years > 0:
            try:
                annualised = _finite((1.0 + total) ** (1.0 / years) - 1.0)
            except (OverflowError, ValueError):
                annualised = None
            if net and years < _ANNUALISATION_WARN_YEARS:
                message = (f"annualised from {years:.3f} years of data; "
                           "extrapolating this far is not a forecast")
                if message not in warnings:
                    warnings.append(message)

    sharpe = None
    if sd is not None and sd > 0:
        sharpe = _finite((mu - rf_per_bar) / sd * math.sqrt(ppy))

    dsd = _downside_deviation(rets, rf_per_bar)
    sortino = None
    if dsd is not None and dsd > 0:
        sortino = _finite((mu - rf_per_bar) / dsd * math.sqrt(ppy))

    dd = drawdown_stats(curve)
    calmar = None
    if annualised is not None and dd.depth > 0:
        calmar = _finite(annualised / dd.depth)

    stats = _trade_stats(trades, net=net)
    return ReturnStats(
        total_return=total, annualised_return=annualised, volatility=vol,
        sharpe=sharpe, sortino=sortino, max_drawdown=dd.depth,
        max_drawdown_bars=dd.bars_to_trough,
        max_drawdown_seconds=dd.seconds_to_trough,
        max_drawdown_recovered=(dd.depth == 0 or dd.recovery_index is not None),
        calmar=calmar, **stats)


def compute_report(equity_curve: Sequence[tuple[Any, Any]],
                   trades: Sequence[Any] = (), *,
                   timeframe: str = "1d",
                   risk_free_rate: float = 0.0,
                   continuous: bool = False,
                   cost_curve: Sequence[tuple[Any, Any]] | None = None,
                   fees: Decimal | float = 0,
                   slippage: Decimal | float = 0,
                   exposure: float = 0.0,
                   turnover: float = 0.0,
                   label: str = "",
                   warnings: Sequence[str] = ()) -> PerformanceReport:
    """Build a full gross-and-net report from an equity curve and its trades.

    `equity_curve` is `[(timestamp, equity)]`, one point per bar, *net* of every
    cost already charged. `trades` are duck-typed: anything exposing
    `gross_pnl` / `net_pnl` / `fees` / `opened_at` / `closed_at` works, so
    `portfolio.TradeRecord` drops straight in.

    `cost_curve` is `[(timestamp, cumulative_cost)]`. Supplying it is what lets
    the gross curve be reconstructed *chronologically* rather than by adding the
    total back at the end; the backtester always supplies it.

    `risk_free_rate` is annualised and de-annualised internally.
    """
    notes = list(warnings)
    if len(equity_curve) < 2:
        notes.append("equity curve too short for return statistics")

    ppy = periods_per_year(timeframe, continuous=continuous)

    start_eq = Decimal(str(equity_curve[0][1])) if equity_curve else _ZERO
    end_eq = Decimal(str(equity_curve[-1][1])) if equity_curve else _ZERO
    fees_d = Decimal(str(fees))
    slip_d = Decimal(str(slippage))

    net_curve = [(ts, _f(v)) for ts, v in equity_curve]
    gross = _gross_curve(net_curve, cost_curve, _cost_events(trades),
                         residual=_f(fees_d + slip_d))

    net_stats = _curve_stats(net_curve, trades, net=True, ppy=ppy,
                             risk_free_rate=risk_free_rate, warnings=notes)
    gross_stats = _curve_stats(gross, trades, net=False, ppy=ppy,
                               risk_free_rate=risk_free_rate, warnings=notes)

    return PerformanceReport(
        gross=gross_stats, net=net_stats,
        n_bars=len(equity_curve), n_trades=len(trades),
        start=equity_curve[0][0] if equity_curve else None,
        end=equity_curve[-1][0] if equity_curve else None,
        starting_equity=start_eq, ending_equity=end_eq,
        turnover=turnover, exposure=exposure,
        total_fees=fees_d, total_slippage=slip_d,
        avg_holding_period_s=average_holding_period(trades),
        timeframe=timeframe, periods_per_year=ppy,
        risk_free_rate=risk_free_rate,
        label=label, warnings=tuple(notes))


# ---------------------------------------------------------------------------
# breakdowns
# ---------------------------------------------------------------------------

def trade_equity_curve(trades: Sequence[Any], *,
                       starting_equity: Decimal | float = 0,
                       net: bool = True) -> list[tuple[datetime, float]]:
    """A curve built from closed-trade P&L, for slices that have no marks.

    A per-instrument or per-regime slice has no mark-to-market equity of its
    own — the portfolio is marked as a whole. The honest substitute is the
    cumulative realised P&L of that slice's trades, and it is a *different*
    thing: it steps only at trade closes, so its drawdown understates the
    intraday excursion the position actually went through. That limitation is
    stated in each breakdown report's warnings rather than left for the reader
    to infer.
    """
    equity = _f(starting_equity)
    ordered = sorted(
        (t for t in trades if isinstance(getattr(t, "closed_at", None), datetime)),
        key=lambda t: t.closed_at)
    if not ordered:
        return []
    curve = [(ordered[0].closed_at - timedelta(microseconds=1), equity)]
    for trade in ordered:
        equity += _trade_pnl(trade, net=net)
        curve.append((trade.closed_at, equity))
    return curve


_BREAKDOWN_CAVEAT = ("slice metrics are computed from closed-trade P&L, not "
                     "from a marked-to-market curve; drawdown here understates "
                     "the excursion the open position actually took")


def _slice_report(trades: Sequence[Any], *, label: str, timeframe: str,
                  starting_equity: Decimal | float,
                  risk_free_rate: float, continuous: bool) -> PerformanceReport:
    curve = trade_equity_curve(trades, starting_equity=starting_equity, net=True)
    fees = sum((Decimal(str(getattr(t, "fees", 0))) for t in trades), _ZERO)
    if len(curve) < 2:
        # No closed trades: still emit a report so the key exists and the caller
        # does not have to special-case "traded but never closed".
        return PerformanceReport(
            gross=ReturnStats(**_trade_stats(trades, net=False)),
            net=ReturnStats(**_trade_stats(trades, net=True)),
            n_trades=len(trades), label=label, timeframe=timeframe,
            total_fees=fees,
            avg_holding_period_s=average_holding_period(trades),
            warnings=(_BREAKDOWN_CAVEAT,))
    return compute_report(curve, trades, timeframe=timeframe,
                          risk_free_rate=risk_free_rate, continuous=continuous,
                          fees=fees, label=label,
                          warnings=(_BREAKDOWN_CAVEAT,))


def group_by_instrument(trades: Sequence[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for t in trades:
        out.setdefault(getattr(t, "instrument_key", "?"), []).append(t)
    return out


def by_instrument(trades: Sequence[Any], *, timeframe: str = "1d",
                  starting_equity: Decimal | float = 0,
                  risk_free_rate: float = 0.0,
                  continuous: bool = False) -> dict[str, PerformanceReport]:
    """One report per instrument. Trade counts sum back to the whole."""
    return {key: _slice_report(group, label=key, timeframe=timeframe,
                               starting_equity=starting_equity,
                               risk_free_rate=risk_free_rate,
                               continuous=continuous)
            for key, group in group_by_instrument(trades).items()}


def _regime_label(value: Any) -> str:
    return str(getattr(value, "value", None) or value or "unknown")


def group_by_regime(trades: Sequence[Any],
                    regimes: Mapping[Any, Any] | Callable[[Any], Any] | None
                    ) -> dict[str, list[Any]]:
    """Group closed trades by the regime in force when they closed.

    `regimes` may be a callable, or a mapping from timestamp to regime. A
    mapping is looked up **as-of**, not exactly: a regime series is sampled per
    bar and a trade closes on a fill timestamp, so requiring an exact key match
    would silently label almost everything "unknown" — a breakdown that looks
    populated and is entirely wrong.
    """
    out: dict[str, list[Any]] = {}
    stamps: list[Any] = []
    values: list[Any] = []
    if isinstance(regimes, Mapping):
        pairs = sorted((k for k in regimes if isinstance(k, datetime)))
        stamps = list(pairs)
        values = [regimes[k] for k in pairs]

    for t in trades:
        closed = getattr(t, "closed_at", None)
        found: Any = None
        if callable(regimes):
            found = regimes(t)
        elif isinstance(regimes, Mapping):
            if closed in regimes:
                found = regimes[closed]
            elif stamps and isinstance(closed, datetime):
                index = bisect.bisect_right(stamps, closed) - 1
                if index >= 0:
                    found = values[index]
        out.setdefault(_regime_label(found), []).append(t)
    return out


def by_regime(trades: Sequence[Any],
              regimes: Mapping[Any, Any] | Callable[[Any], Any] | None = None,
              *, timeframe: str = "1d", starting_equity: Decimal | float = 0,
              risk_free_rate: float = 0.0,
              continuous: bool = False) -> dict[str, PerformanceReport]:
    """One report per market regime. Trade counts sum back to the whole."""
    return {label: _slice_report(group, label=label, timeframe=timeframe,
                                 starting_equity=starting_equity,
                                 risk_free_rate=risk_free_rate,
                                 continuous=continuous)
            for label, group in group_by_regime(trades, regimes).items()}


def period_key(ts: datetime, granularity: str = "M") -> str:
    """Calendar bucket label: month ("M"), quarter ("Q"), or year ("Y")."""
    upper = (granularity or "M").upper()
    if upper == "Y":
        return f"{ts.year}"
    if upper == "Q":
        return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"
    if upper == "M":
        return f"{ts.year}-{ts.month:02d}"
    raise ValueError(f"granularity must be one of M, Q, Y (got {granularity!r})")


def group_by_period(trades: Sequence[Any],
                    granularity: str = "M") -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for t in trades:
        ts = getattr(t, "closed_at", None)
        if not isinstance(ts, datetime):
            continue
        out.setdefault(period_key(ts, granularity), []).append(t)
    return out


def by_period(equity_curve: Sequence[tuple[Any, Any]], granularity: str = "M",
              *, trades: Sequence[Any] = (), timeframe: str = "1d",
              risk_free_rate: float = 0.0, continuous: bool = False,
              cost_curve: Sequence[tuple[Any, Any]] | None = None
              ) -> dict[str, PerformanceReport]:
    """One report per calendar month / quarter / year of the equity curve.

    Each bucket's sub-curve is prefixed with the *previous* bucket's closing
    point, so the return that spans the boundary belongs to the bucket that
    earned it. Without that prefix the first bar of every month would silently
    contribute no return at all, and the buckets would not compound back to the
    whole-period figure.
    """
    if not equity_curve:
        return {}
    buckets: dict[str, list[int]] = {}
    for i, (ts, _) in enumerate(equity_curve):
        if not isinstance(ts, datetime):
            continue
        buckets.setdefault(period_key(ts, granularity), []).append(i)

    trade_groups = group_by_period(trades, granularity)
    costs = list(cost_curve or ())

    out: dict[str, PerformanceReport] = {}
    for label, indices in buckets.items():
        first = indices[0]
        lo = first - 1 if first > 0 else first
        window = list(equity_curve[lo: indices[-1] + 1])
        sub_costs = None
        if costs:
            sub_costs = costs[lo: indices[-1] + 1]
        group = trade_groups.get(label, [])
        fees = sum((Decimal(str(getattr(t, "fees", 0))) for t in group), _ZERO)
        out[label] = compute_report(
            window, group, timeframe=timeframe, risk_free_rate=risk_free_rate,
            continuous=continuous, cost_curve=sub_costs, fees=fees,
            label=label)
    return out


__all__ = [
    "DrawdownStats", "PerformanceReport", "ReturnStats",
    "average_holding_period", "by_instrument", "by_period", "by_regime",
    "compute_report", "drawdown_stats", "group_by_instrument",
    "group_by_period", "group_by_regime", "max_drawdown", "period_key",
    "periods_per_year", "trade_equity_curve",
]
