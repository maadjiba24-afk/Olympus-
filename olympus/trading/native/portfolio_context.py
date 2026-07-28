"""Portfolio-aware forecasting — read-only, and structurally so.

> The forecasting model must not directly modify the portfolio or place orders.

That is enforced three ways, because one would be a convention:

1. **This module takes a snapshot, not a manager.** `PortfolioView` is built
   from plain values. There is no code path from here to
   `PortfolioManager.apply_fill`, because there is no reference to a manager to
   call it on.
2. **Every type here is frozen.** A context object cannot be mutated into a
   position change even by a caller who wanted to.
3. **`kernel.audit_evolution_modules` covers this module**, and the forbidden
   call names include `submit_order` and `place_order`.

What a forecast may condition on
--------------------------------
Existing positions, concentration, correlation, gross and net exposure,
marginal risk, drawdown contribution, hedge relationships and liquidity
requirements. All of it is *evidence*: it changes what the model predicts and
how confident it is, and it never becomes a size. Sizing stays in `risk.py`,
which is in the safety kernel and is not reachable from here.

The one genuinely internal dataset
----------------------------------
This is the only capability in Phase 3 whose inputs Olympus actually owns.
`portfolio.py` holds real positions from the paper broker, with real fills and
real fees. It is not market data and it is not synthetic — which makes this the
capability with the strongest data position and, correspondingly, the one whose
limitations are about modelling rather than about access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when a field's meaning changes.
PORTFOLIO_CONTEXT_VERSION = 1


@dataclass(frozen=True)
class PositionView:
    """One position, as a forecaster may see it. Plain floats, frozen.

    Deliberately not `contracts.Position`: that carries `Decimal` quantities
    and is the object the portfolio manager mutates. Copying the numbers out
    into a frozen float view is what makes "the model cannot change the
    portfolio" structural rather than aspirational.
    """

    instrument_key: str
    #: Signed. Negative is short.
    quantity: float
    average_price: float
    mark_price: float
    #: Signed notional at the mark.
    notional: float = 0.0
    unrealised_pnl: float = 0.0
    opened_at: datetime | None = None

    def __post_init__(self):
        if not str(self.instrument_key).strip():
            raise ConfigurationError("a position must name its instrument")
        for name in ("quantity", "average_price", "mark_price", "notional",
                     "unrealised_pnl"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ConfigurationError(f"{name} must be finite",
                                         instrument_key=self.instrument_key)
            object.__setattr__(self, name, value)
        # Derive both, and derive them the same way. `notional` being computed
        # while `unrealised_pnl` was not is exactly the kind of asymmetry that
        # silently zeroes a real number for a caller who supplied one and not
        # the other. Deriving a zero is harmless: when mark == average the
        # formula gives zero anyway.
        if self.quantity != 0.0:
            if self.notional == 0.0:
                object.__setattr__(self, "notional",
                                   self.quantity * self.mark_price)
            if self.unrealised_pnl == 0.0:
                object.__setattr__(
                    self, "unrealised_pnl",
                    self.quantity * (self.mark_price - self.average_price))
        if self.opened_at is not None:
            object.__setattr__(self, "opened_at",
                               ensure_utc(self.opened_at, field_name="opened_at"))

    @property
    def side(self) -> str:
        if self.quantity > 0:
            return "long"
        return "short" if self.quantity < 0 else "flat"

    def to_dict(self) -> dict:
        return {"instrument": self.instrument_key, "quantity": self.quantity,
                "average_price": self.average_price,
                "mark_price": self.mark_price, "notional": self.notional,
                "unrealised_pnl": self.unrealised_pnl, "side": self.side}

    @classmethod
    def from_position(cls, position: Any, *, mark_price: float) -> "PositionView":
        """Copy a `contracts.Position` into a frozen float view."""
        quantity = float(position.quantity)
        average = float(position.average_price)
        return cls(instrument_key=position.instrument_key, quantity=quantity,
                   average_price=average, mark_price=float(mark_price),
                   notional=quantity * float(mark_price),
                   unrealised_pnl=quantity * (float(mark_price) - average),
                   opened_at=getattr(position, "opened_at", None))


@dataclass(frozen=True)
class PortfolioView:
    """The portfolio as evidence. Frozen, and built from values not managers."""

    as_of: datetime
    equity: float
    cash: float
    positions: tuple[PositionView, ...] = ()
    peak_equity: float = 0.0
    #: Trailing correlation between instruments, if it has been estimated.
    #: Supplied rather than computed here: correlation needs a return history,
    #: and a portfolio view is a snapshot.
    correlations: Mapping[tuple[str, str], float] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "correlations",
                           {tuple(sorted(k)): float(v)
                            for k, v in dict(self.correlations).items()})
        for name in ("equity", "cash", "peak_equity"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ConfigurationError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if self.equity <= 0:
            raise ConfigurationError(
                "equity must be positive; every exposure figure here is a "
                "fraction of it and dividing by zero would produce an "
                "exposure of infinity that reads as a number",
                equity=self.equity)
        if self.peak_equity == 0.0:
            object.__setattr__(self, "peak_equity", self.equity)
        keys = [p.instrument_key for p in self.positions]
        if len(set(keys)) != len(keys):
            raise ConfigurationError("an instrument appears twice",
                                     instruments=sorted(keys))

    # -- exposure ----------------------------------------------------------

    @property
    def gross_exposure(self) -> float:
        """Sum of absolute notionals, as a fraction of equity."""
        return sum(abs(p.notional) for p in self.positions) / self.equity

    @property
    def net_exposure(self) -> float:
        """Signed sum, as a fraction of equity. Long-short nets out here and
        does not net out of `gross_exposure`, which is the point of having
        both."""
        return sum(p.notional for p in self.positions) / self.equity

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:                        # pragma: no cover
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def concentration(self) -> float:
        """Herfindahl index over absolute notionals, in [0, 1].

        1.0 is everything in one instrument; 1/n is evenly spread. Chosen over
        "largest position as a fraction" because that number is identical for a
        portfolio of two positions and one of twenty with the same largest.
        """
        notionals = [abs(p.notional) for p in self.positions]
        total = sum(notionals)
        if total <= 0:
            return 0.0
        return sum((n / total) ** 2 for n in notionals)

    @property
    def largest_position(self) -> float:
        notionals = [abs(p.notional) for p in self.positions]
        return max(notionals) / self.equity if notionals else 0.0

    def position(self, instrument_key: str) -> PositionView | None:
        return next((p for p in self.positions
                     if p.instrument_key == instrument_key), None)

    def correlation(self, a: str, b: str) -> float | None:
        """`None` when it was never estimated. Never zero as a default —
        an unestimated correlation is not an estimate of independence."""
        if a == b:
            return 1.0
        return self.correlations.get(tuple(sorted((a, b))))

    def to_dict(self) -> dict:
        return {"as_of": jsonable(self.as_of), "equity": self.equity,
                "cash": self.cash, "peak_equity": self.peak_equity,
                "positions": [p.to_dict() for p in self.positions],
                "gross_exposure": self.gross_exposure,
                "net_exposure": self.net_exposure,
                "drawdown": self.drawdown,
                "concentration": self.concentration,
                "largest_position": self.largest_position,
                "n_correlations": len(self.correlations),
                "schema_version": PORTFOLIO_CONTEXT_VERSION}

    @classmethod
    def from_manager(cls, manager: Any, *, marks: Mapping[str, float],
                     as_of: datetime,
                     correlations: Mapping[tuple[str, str], float] | None = None
                     ) -> "PortfolioView":
        """Copy a `PortfolioManager`'s state out into a frozen view.

        A copy, deliberately. The manager is not retained, so nothing
        downstream can reach `apply_fill` through this object — which is what
        makes the read-only claim structural rather than a matter of
        discipline.
        """
        positions = tuple(
            PositionView.from_position(position, mark_price=marks[key])
            for key, position in sorted(manager.open_positions().items())
            if key in marks)
        equity = float(manager.equity(as_of))
        return cls(as_of=as_of, equity=equity, cash=float(manager.cash),
                   positions=positions,
                   peak_equity=float(getattr(manager, "peak_equity", equity)
                                     or equity),
                   correlations=dict(correlations or {}))


@dataclass(frozen=True)
class PortfolioSignal:
    """What the portfolio says about *this* instrument, as evidence.

    Every field is a number a forecaster may condition on. None of them is a
    size, and there is no method here that produces one.
    """

    instrument_key: str
    existing_side: str
    existing_exposure: float
    gross_exposure: float
    net_exposure: float
    concentration: float
    drawdown: float
    #: Change in gross exposure a unit position would cause. The cheap version
    #: of marginal risk, and honest about being cheap.
    marginal_gross: float
    #: Correlation-weighted exposure to instruments related to this one.
    correlated_exposure: float | None
    #: Positions whose correlation with this one is strongly negative.
    hedges: tuple[str, ...] = ()
    #: This position's share of portfolio drawdown, when it is in one.
    drawdown_contribution: float | None = None
    #: Fraction of equity that would have to be liquidated to exit.
    liquidity_requirement: float = 0.0
    #: Reasons a forecast on this instrument should be treated cautiously.
    cautions: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "hedges", tuple(self.hedges))
        object.__setattr__(self, "cautions", tuple(self.cautions))

    @property
    def adding_would_concentrate(self) -> bool:
        return self.concentration > 0.5 or self.largest_share > 0.35

    @property
    def largest_share(self) -> float:
        return abs(self.existing_exposure)

    def features(self) -> Mapping[str, float]:
        """The numeric surface a model consumes."""
        return {
            "portfolio_existing_exposure": self.existing_exposure,
            "portfolio_gross_exposure": self.gross_exposure,
            "portfolio_net_exposure": self.net_exposure,
            "portfolio_concentration": self.concentration,
            "portfolio_drawdown": self.drawdown,
            "portfolio_marginal_gross": self.marginal_gross,
            "portfolio_correlated_exposure":
                self.correlated_exposure if self.correlated_exposure is not None
                else 0.0,
            "portfolio_has_hedge": 1.0 if self.hedges else 0.0,
        }

    def to_dict(self) -> dict:
        return {"instrument": self.instrument_key,
                "existing_side": self.existing_side,
                "existing_exposure": self.existing_exposure,
                "gross_exposure": self.gross_exposure,
                "net_exposure": self.net_exposure,
                "concentration": self.concentration,
                "drawdown": self.drawdown,
                "marginal_gross": self.marginal_gross,
                "correlated_exposure": self.correlated_exposure,
                "hedges": list(self.hedges),
                "drawdown_contribution": self.drawdown_contribution,
                "liquidity_requirement": self.liquidity_requirement,
                "cautions": list(self.cautions),
                "adding_would_concentrate": self.adding_would_concentrate,
                "features": dict(self.features())}


def portfolio_signal(view: PortfolioView, instrument_key: str, *,
                     hedge_threshold: float = -0.4,
                     concentration_limit: float = 0.5) -> PortfolioSignal:
    """What the portfolio says about this instrument. Evidence, never a size."""
    position = view.position(instrument_key)
    existing = (position.notional / view.equity) if position else 0.0

    correlated: float | None = None
    hedges: list[str] = []
    weighted = 0.0
    seen = 0
    for other in view.positions:
        if other.instrument_key == instrument_key:
            continue
        rho = view.correlation(instrument_key, other.instrument_key)
        if rho is None:
            continue
        seen += 1
        weighted += rho * (other.notional / view.equity)
        if rho <= hedge_threshold:
            hedges.append(other.instrument_key)
    if seen:
        correlated = weighted

    contribution = None
    if view.drawdown > 0 and position is not None:
        losses = sum(min(0.0, p.unrealised_pnl) for p in view.positions)
        if losses < 0:
            contribution = min(0.0, position.unrealised_pnl) / losses

    cautions: list[str] = []
    if view.concentration > concentration_limit:
        cautions.append(
            f"portfolio concentration is {view.concentration:.2f}; adding to "
            f"this instrument increases single-name risk")
    if abs(existing) > 0.25:
        cautions.append(
            f"this instrument is already {abs(existing):.0%} of equity")
    if correlated is not None and abs(correlated) > 0.5:
        cautions.append(
            f"correlated exposure is {correlated:+.2f} of equity; a position "
            f"here is less diversifying than it appears")
    if view.drawdown > 0.1:
        cautions.append(f"portfolio is {view.drawdown:.0%} below its peak")
    if position is not None and not hedges and seen == 0:
        cautions.append(
            "no correlation estimates were supplied, so the diversification "
            "effect of this position is unknown rather than zero")

    return PortfolioSignal(
        instrument_key=instrument_key,
        existing_side=position.side if position else "flat",
        existing_exposure=existing,
        gross_exposure=view.gross_exposure,
        net_exposure=view.net_exposure,
        concentration=view.concentration,
        drawdown=view.drawdown,
        marginal_gross=1.0 if existing == 0 else (1.0 if existing > 0 else -1.0),
        correlated_exposure=correlated,
        hedges=tuple(sorted(hedges)),
        drawdown_contribution=contribution,
        liquidity_requirement=abs(existing),
        cautions=tuple(cautions))


#: Every method name a forecasting module may not call. Declared here, which
#: is why this file has to be exempt from its own check.
MUTATING_METHODS: tuple[str, ...] = (
    "apply_fill", "apply_fills", "submit_order", "place_order",
    "mark_all(", "record_equity_point", "save_limits")

#: Modules permitted to name a mutating method, and why. Same
#: enumerated-with-a-reason pattern as `events.BOUNDARY_EXEMPT` and
#: `originality.MARKER_EXEMPT`, and `read_only_exemption_is_needed` fails if an
#: entry outlives its reason.
READ_ONLY_EXEMPT: Mapping[str, str] = {
    "portfolio_context.py": "declares MUTATING_METHODS; must contain every "
                            "name in it",
}


def assert_read_only(module_source: str, *, name: str = "<module>") -> None:
    """No portfolio-aware code calls a mutating portfolio or order method.

    The names are the ones `kernel.FORBIDDEN_KERNEL_CALLS` already protects,
    plus the portfolio manager's own mutators. Checked on source rather than by
    trying it, because the failure this prevents is one that must never happen
    even once.
    """
    if name in READ_ONLY_EXEMPT:
        return
    lowered = module_source.lower()
    offenders = sorted(f for f in MUTATING_METHODS if f.lower() in lowered)
    if offenders:
        raise ConfigurationError(
            "portfolio-aware forecasting code calls a mutating method",
            module=name, offenders=offenders)


def read_only_exemption_is_needed() -> tuple[str, ...]:
    """Exemptions that have outlived their reason. Empty is the good state."""
    from pathlib import Path
    stale = []
    for name, reason in READ_ONLY_EXEMPT.items():
        path = Path(__file__).resolve().parent / name
        if not path.exists():
            stale.append(f"{name}: exempted and does not exist")
            continue
        text = path.read_text(encoding="utf-8").lower()
        if not any(method.lower() in text for method in MUTATING_METHODS):
            stale.append(f"{name}: exempted and names no mutating method "
                         f"({reason})")
    return tuple(stale)


__all__ = ["PORTFOLIO_CONTEXT_VERSION", "MUTATING_METHODS", "READ_ONLY_EXEMPT",
           "PositionView", "PortfolioView", "PortfolioSignal",
           "portfolio_signal", "assert_read_only",
           "read_only_exemption_is_needed"]
