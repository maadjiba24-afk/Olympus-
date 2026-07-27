"""Performance metrics.

Pure stdlib, and deliberately unforgiving about two things.

**Gross and net are reported together, always.** `PerformanceReport` carries
both, because the single most common way a strategy report misleads is by
quoting returns before costs. A strategy that makes 8% gross and −2% net is a
losing strategy, and a report that lets you print only the first number will
eventually be used to do exactly that.

**Undefined is `None`, never a number.** A flat equity curve has no Sharpe
ratio — zero volatility makes the quotient undefined, not infinite. Returning
`inf`, `nan`, or `0.0` there produces a leaderboard where the strategy that
never traded looks competitive. Every ratio here returns `None` when its
denominator vanishes, and the formatter prints "n/a".

Annualisation is derived from the timeframe rather than hard-coded to 252, so a
5-minute strategy is not silently annualised as if it traded daily.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .contracts import jsonable

#: Trading periods per year by timeframe unit. Approximate by nature — the
#: point is proportionality, and being explicit beats an unexplained 252.
_SECONDS_PER_YEAR = 252 * 6.5 * 3600          # 252 trading days, 6.5h sessions
_CALENDAR_SECONDS_PER_YEAR = 365.25 * 24 * 3600


def periods_per_year(timeframe: str, *, continuous: bool = False) -> float:
    """Bars per year for `timeframe`.

    `continuous=True` for 24/7 markets (crypto), where a day really is 24 hours
    and annualising on a 6.5-hour session would overstate volatility by ~1.9x.
    """
    from .instruments import timeframe_seconds
    seconds = timeframe_seconds(timeframe)
    if seconds <= 0:
        raise ValueError("timeframe must be positive")
    total = _CALENDAR_SECONDS_PER_YEAR if continuous else _SECONDS_PER_YEAR
    return total / seconds


def _returns(curve: Sequence[tuple[Any, Any]]) -> list[float]:
    """Simple period returns from an equity curve. Skips non-positive equity —
    a wiped-out account has no meaningful percentage return."""
    out = []
    for i in range(1, len(curve)):
        prev = float(curve[i - 1][1])
        cur = float(curve[i][1])
        if prev <= 0:
            continue
        out.append(cur / prev - 1.0)
    return out


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: Sequence[float]) -> float | None:
    """Sample standard deviation, or None when undefined."""
    if len(xs) < 2:
        return None
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def max_drawdown(curve: Sequence[tuple[Any, Any]]) -> tuple[float, int]:
    """Deepest peak-to-trough fall as a fraction, and its duration in bars."""
    if not curve:
        return 0.0, 0
    peak = float(curve[0][1])
    worst = 0.0
    peak_i = 0
    longest = 0
    for i, (_, value) in enumerate(curve):
        value = float(value)
        if value > peak:
            peak = value
            peak_i = i
        elif peak > 0:
            drop = (peak - value) / peak
            if drop > worst:
                worst = drop
            longest = max(longest, i - peak_i)
    return worst, longest


@dataclass(frozen=True)
class PerformanceReport:
    """Every required metric, gross and net.

    `None` means *undefined*, not zero — see the module docstring.
    """
    n_bars: int = 0
    n_trades: int = 0
    start: datetime | None = None
    end: datetime | None = None
    starting_equity: Decimal = Decimal("0")
    ending_equity: Decimal = Decimal("0")

    total_return_gross: float = 0.0
    total_return_net: float = 0.0
    annualised_return: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float = 0.0
    max_drawdown_bars: int = 0
    calmar: float | None = None

    win_rate: float | None = None
    profit_factor: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    expectancy: float | None = None
    best_trade: float | None = None
    worst_trade: float | None = None

    turnover: float = 0.0
    exposure: float = 0.0
    total_fees: Decimal = Decimal("0")
    total_slippage: Decimal = Decimal("0")

    label: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {f.name: jsonable(getattr(self, f.name)) for f in fields(self)}

    def format(self) -> str:
        def fmt(value, pct=False):
            if value is None:
                return "n/a"
            if isinstance(value, Decimal):
                return f"{value:,.2f}"
            return f"{value * 100:,.2f}%" if pct else f"{value:,.3f}"

        rows = [
            ("Total return (gross)", fmt(self.total_return_gross, pct=True)),
            ("Total return (net)", fmt(self.total_return_net, pct=True)),
            ("Annualised return", fmt(self.annualised_return, pct=True)),
            ("Volatility (ann.)", fmt(self.volatility, pct=True)),
            ("Sharpe", fmt(self.sharpe)),
            ("Sortino", fmt(self.sortino)),
            ("Max drawdown", fmt(self.max_drawdown, pct=True)),
            ("Calmar", fmt(self.calmar)),
            ("Profit factor", fmt(self.profit_factor)),
            ("Win rate", fmt(self.win_rate, pct=True)),
            ("Average win", fmt(self.average_win)),
            ("Average loss", fmt(self.average_loss)),
            ("Expectancy", fmt(self.expectancy)),
            ("Trades", str(self.n_trades)),
            ("Turnover", fmt(self.turnover)),
            ("Exposure", fmt(self.exposure, pct=True)),
            ("Fees", fmt(self.total_fees)),
            ("Slippage", fmt(self.total_slippage)),
        ]
        width = max(len(name) for name, _ in rows)
        head = f"{self.label or 'performance'}\n" + "-" * (width + 16) + "\n"
        return head + "\n".join(f"{n:<{width}}  {v:>12}" for n, v in rows)


def compute_report(equity_curve: Sequence[tuple[Any, Any]],
                   trades: Sequence[Any] = (), *, timeframe: str = "1d",
                   risk_free_rate: float = 0.0, continuous: bool = False,
                   fees: Decimal | float = 0, slippage: Decimal | float = 0,
                   exposure: float = 0.0, turnover: float = 0.0,
                   label: str = "") -> PerformanceReport:
    """Build a full report from an equity curve and its trades.

    `trades` are duck-typed: anything exposing `gross_pnl`/`net_pnl` works, so
    `portfolio.TradeRecord` drops straight in.
    """
    warnings: list[str] = []
    if len(equity_curve) < 2:
        warnings.append("equity curve too short for return statistics")

    rets = _returns(equity_curve)
    ppy = periods_per_year(timeframe, continuous=continuous)

    start_eq = Decimal(str(equity_curve[0][1])) if equity_curve else Decimal("0")
    end_eq = Decimal(str(equity_curve[-1][1])) if equity_curve else Decimal("0")

    fees_d = Decimal(str(fees))
    slip_d = Decimal(str(slippage))

    total_net = float(end_eq / start_eq - 1) if start_eq > 0 else 0.0
    # Gross adds the costs back: what the price movement alone would have given.
    gross_end = end_eq + fees_d + slip_d
    total_gross = float(gross_end / start_eq - 1) if start_eq > 0 else 0.0

    sd = _stdev(rets)
    vol = sd * math.sqrt(ppy) if sd is not None else None
    mu = _mean(rets)

    ann = None
    if rets and start_eq > 0 and (1 + total_net) > 0:
        years = len(rets) / ppy
        if years > 0:
            ann = (1 + total_net) ** (1 / years) - 1

    sharpe = None
    if sd is not None and sd > 0:
        sharpe = (mu - risk_free_rate / ppy) / sd * math.sqrt(ppy)

    downside = [r for r in rets if r < 0]
    dsd = _stdev(downside)
    sortino = None
    if dsd is not None and dsd > 0:
        sortino = (mu - risk_free_rate / ppy) / dsd * math.sqrt(ppy)

    mdd, mdd_bars = max_drawdown(equity_curve)
    calmar = (ann / mdd) if (ann is not None and mdd > 0) else None

    pnls = []
    for t in trades:
        value = getattr(t, "net_pnl", None)
        if value is None:
            value = getattr(t, "gross_pnl", 0)
        pnls.append(float(value))
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return PerformanceReport(
        n_bars=len(equity_curve), n_trades=len(pnls),
        start=equity_curve[0][0] if equity_curve else None,
        end=equity_curve[-1][0] if equity_curve else None,
        starting_equity=start_eq, ending_equity=end_eq,
        total_return_gross=total_gross, total_return_net=total_net,
        annualised_return=ann, volatility=vol, sharpe=sharpe, sortino=sortino,
        max_drawdown=mdd, max_drawdown_bars=mdd_bars, calmar=calmar,
        win_rate=(len(wins) / len(pnls)) if pnls else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        average_win=_mean(wins) if wins else None,
        average_loss=_mean(losses) if losses else None,
        expectancy=_mean(pnls) if pnls else None,
        best_trade=max(pnls) if pnls else None,
        worst_trade=min(pnls) if pnls else None,
        turnover=turnover, exposure=exposure,
        total_fees=fees_d, total_slippage=slip_d,
        label=label, warnings=tuple(warnings))


def by_instrument(trades: Sequence[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for t in trades:
        out.setdefault(getattr(t, "instrument_key", "?"), []).append(t)
    return out


def by_regime(trades: Sequence[Any],
              regimes: Mapping[Any, Any]) -> dict[str, list[Any]]:
    """Group closed trades by the market regime in force when they closed."""
    out: dict[str, list[Any]] = {}
    for t in trades:
        closed = getattr(t, "closed_at", None)
        regime = regimes.get(closed)
        label = getattr(regime, "value", None) or str(regime or "unknown")
        out.setdefault(label, []).append(t)
    return out


def by_period(trades: Sequence[Any], granularity: str = "M") -> dict[str, list[Any]]:
    """Group by calendar month ("M"), quarter ("Q"), or year ("Y")."""
    out: dict[str, list[Any]] = {}
    for t in trades:
        ts = getattr(t, "closed_at", None)
        if ts is None:
            continue
        if granularity == "Y":
            key = f"{ts.year}"
        elif granularity == "Q":
            key = f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"
        else:
            key = f"{ts.year}-{ts.month:02d}"
        out.setdefault(key, []).append(t)
    return out


__all__ = ["PerformanceReport", "compute_report", "max_drawdown",
           "periods_per_year", "by_instrument", "by_regime", "by_period"]
