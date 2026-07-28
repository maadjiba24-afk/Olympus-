"""Order-book and liquidity intelligence, and the gate on tradability.

> A forecast must not automatically become tradable merely because the expected
> return is positive.

That is the load-bearing requirement, and `TradabilityAssessment` is where it
lives. A positive expected return is a *necessary* condition and nothing more:
the move has to clear the spread, the size has to fit in the book, the impact
has to be smaller than the edge, the fill has to be probable, and the book has
to not be deteriorating. `tradable` is a conjunction of all six, and it is
computed — there is no argument that sets it.

Calibration is the honest problem
---------------------------------
Fill probability, slippage and market impact are *models*, and a model with no
data behind it is a guess with error bars drawn on. Every estimator here
therefore carries a `calibrated` flag and a `basis` string saying what it was
fitted on. In this environment nothing is calibrated: no book data is reachable
(blocker B1), so the estimators run on constructed snapshots with declared
default parameters and say so. `capability.py` records
`production_eligible=False` for exactly that reason, and
`TradabilityAssessment.uncalibrated_inputs` lists which numbers a reader should
not trust.

Why square-root impact
----------------------
`impact_bps = eta · spread_bps · sqrt(size / depth)` is the standard functional
form, chosen because it is the one whose *shape* is robust: impact grows
sub-linearly in size, which is the behaviour every empirical study agrees on
even where the coefficient does not. `eta` is a declared parameter, not a fitted
one, and `ImpactModel.calibrated` is `False` until it is fitted against real
fills — which `outcomes.py` records and which is the one calibration this system
could actually do today.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError, DataValidationError

#: Default impact coefficient. Declared, not fitted — see the module docstring.
DEFAULT_IMPACT_ETA = 0.5

#: Default logistic steepness for fill probability, in units of spread.
DEFAULT_FILL_STEEPNESS = 2.0


class BookQuality(str, Enum):
    """How much of the book is actually known."""

    FULL = "full"              # touch and depth
    TOUCH_ONLY = "touch_only"  # bid and ask, no depth
    ABSENT = "absent"          # nothing


@dataclass(frozen=True)
class BookSnapshot:
    """The book at one instant. Every field beyond the touch is optional.

    A snapshot, and stamped as one. Treating it as though it held over a bar
    overstates what was tradable — which is why `schema.py` marks `bid`, `ask`
    and depth as `SNAPSHOT_AT_CLOSE` rather than `INTERVAL_AGGREGATE`.
    """

    instrument_key: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float | None = None
    ask_size: float | None = None
    #: Resting size within a declared band around the mid, per side.
    bid_depth: float | None = None
    ask_depth: float | None = None
    depth_band_bps: float | None = None
    #: Aggressor flow over the preceding interval.
    buy_volume: float | None = None
    sell_volume: float | None = None
    trade_count: int | None = None
    source: str = "unknown"

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        for name in ("bid", "ask"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise DataValidationError(f"{name} must be positive and finite",
                                          instrument_key=self.instrument_key,
                                          **{name: value})
            object.__setattr__(self, name, value)
        if self.ask < self.bid:
            raise DataValidationError(
                "a crossed book is not a wide spread, it is bad data",
                instrument_key=self.instrument_key, bid=self.bid, ask=self.ask)
        for name in ("bid_size", "ask_size", "bid_depth", "ask_depth",
                     "buy_volume", "sell_volume", "depth_band_bps"):
            value = getattr(self, name)
            if value is None:
                continue
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise DataValidationError(f"{name} must be non-negative",
                                          instrument_key=self.instrument_key,
                                          **{name: value})
            object.__setattr__(self, name, value)
        if self.depth_band_bps is None and (self.bid_depth is not None
                                            or self.ask_depth is not None):
            raise ConfigurationError(
                "depth was supplied without the band it was measured over; "
                "'size within 5bp' and 'size within 50bp' are different "
                "quantities and a model cannot tell them apart",
                instrument_key=self.instrument_key)

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        return (self.spread / self.mid) * 10_000.0 if self.mid > 0 else 0.0

    @property
    def quality(self) -> BookQuality:
        if self.bid_depth is not None and self.ask_depth is not None:
            return BookQuality.FULL
        return BookQuality.TOUCH_ONLY

    @property
    def book_imbalance(self) -> float | None:
        """(bid − ask) / (bid + ask) over depth, or size at the touch.

        `None` when neither is known. Zero would be a claim of balance, which
        is a real and different statement from "we cannot see the book".
        """
        for a, b in ((self.bid_depth, self.ask_depth),
                     (self.bid_size, self.ask_size)):
            if a is None or b is None:
                continue
            total = a + b
            if total > 0:
                return (a - b) / total
        return None

    @property
    def trade_imbalance(self) -> float | None:
        if self.buy_volume is None or self.sell_volume is None:
            return None
        total = self.buy_volume + self.sell_volume
        return (self.buy_volume - self.sell_volume) / total if total > 0 else None

    @property
    def total_depth(self) -> float | None:
        if self.bid_depth is None or self.ask_depth is None:
            return None
        return self.bid_depth + self.ask_depth

    def depth_for(self, side: str) -> float | None:
        """Depth on the side a taker would consume. `None` when unknown."""
        if side not in ("buy", "sell"):
            raise ConfigurationError("side must be 'buy' or 'sell'", side=side)
        return self.ask_depth if side == "buy" else self.bid_depth

    def to_dict(self) -> dict:
        return {"instrument": self.instrument_key, "ts": jsonable(self.ts),
                "bid": self.bid, "ask": self.ask, "mid": self.mid,
                "spread": self.spread, "spread_bps": self.spread_bps,
                "bid_depth": self.bid_depth, "ask_depth": self.ask_depth,
                "depth_band_bps": self.depth_band_bps,
                "book_imbalance": self.book_imbalance,
                "trade_imbalance": self.trade_imbalance,
                "quality": self.quality.value, "source": self.source}


@dataclass(frozen=True)
class Estimate:
    """A number, and whether anything was fitted to produce it.

    The pairing is the point. An uncalibrated estimate is still useful — it has
    the right shape and the right monotonicity — and reporting it without
    saying so would let a caller size a position on a coefficient somebody
    guessed.
    """

    value: float
    calibrated: bool
    basis: str
    #: Inputs that were missing and defaulted, if any.
    assumptions: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        if not self.basis.strip():
            raise ConfigurationError(
                "an estimate must say what it rests on; a number with no basis "
                "is a number nobody can check")

    def to_dict(self) -> dict:
        return {"value": self.value, "calibrated": self.calibrated,
                "basis": self.basis, "assumptions": list(self.assumptions)}


@dataclass(frozen=True)
class ImpactModel:
    """Square-root market impact. Parameters declared, never silently fitted.

    `calibrate_from_fills` is the one path that sets `calibrated=True`, and it
    needs real fills — which `outcomes.py` records from the paper broker. That
    makes this the one estimator in this module whose calibration is reachable
    today, and it has not been done.
    """

    eta: float = DEFAULT_IMPACT_ETA
    calibrated: bool = False
    basis: str = "declared default; not fitted to any fill history"
    n_fills: int = 0

    def __post_init__(self):
        if self.eta <= 0:
            raise ConfigurationError("eta must be positive", eta=self.eta)

    def impact_bps(self, *, size: float, snapshot: BookSnapshot,
                   side: str = "buy") -> Estimate:
        """`eta · spread_bps · sqrt(size / depth)`.

        Depth is the *consumable* side. When it is unknown the estimate falls
        back to a spread-only floor and says so in `assumptions`, rather than
        assuming a depth that would make the impact look small.
        """
        if size <= 0:
            raise ConfigurationError("size must be positive", size=size)
        depth = snapshot.depth_for(side)
        spread_bps = snapshot.spread_bps
        if depth is None or depth <= 0:
            return Estimate(
                value=spread_bps * 0.5, calibrated=False,
                basis="half-spread floor; the depth this size would consume is "
                      "unknown, so the true impact is at least this and "
                      "possibly much larger",
                assumptions=("depth unknown", "impact is a lower bound"))
        ratio = size / depth
        return Estimate(
            value=self.eta * spread_bps * math.sqrt(ratio),
            calibrated=self.calibrated,
            basis=self.basis,
            assumptions=() if self.calibrated else ("eta is a declared default",))

    def calibrate_from_fills(self, fills: Sequence[Mapping[str, float]]
                             ) -> "ImpactModel":
        """Fit `eta` to observed slippage. Needs `size`, `depth`,
        `spread_bps` and `realised_bps` per fill.

        Least squares through the origin on `realised / (spread·sqrt(size/depth))`
        — a one-parameter fit, because with the fill counts this system can
        realistically produce, anything richer would fit noise.
        """
        rows = []
        for fill in fills:
            try:
                size = float(fill["size"])
                depth = float(fill["depth"])
                spread = float(fill["spread_bps"])
                realised = float(fill["realised_bps"])
            except (KeyError, TypeError, ValueError):
                continue
            if size <= 0 or depth <= 0 or spread <= 0:
                continue
            predictor = spread * math.sqrt(size / depth)
            if predictor > 0:
                rows.append(realised / predictor)
        if len(rows) < 10:
            raise ConfigurationError(
                "too few usable fills to fit an impact coefficient; an eta "
                "fitted on a handful of fills is a guess with a decimal point",
                usable=len(rows), need=10)
        return ImpactModel(eta=max(1e-6, statistics.median(rows)),
                           calibrated=True,
                           basis=f"median ratio over {len(rows)} recorded fills",
                           n_fills=len(rows))

    def to_dict(self) -> dict:
        return {"eta": self.eta, "calibrated": self.calibrated,
                "basis": self.basis, "n_fills": self.n_fills}


def fill_probability(*, limit_offset_bps: float, snapshot: BookSnapshot,
                     steepness: float = DEFAULT_FILL_STEEPNESS) -> Estimate:
    """Probability a limit order at `limit_offset_bps` from the mid fills.

    A logistic in units of half-spread: an order at the touch is roughly even
    money, one deep inside the spread is near-certain, one far outside is
    near-zero. The *shape* is defensible and the steepness is not fitted, which
    is what `calibrated=False` says.

    Deliberately not a step function at the touch. A book is not static over the
    life of an order, and a model that says "0 or 1" is one that cannot express
    the only interesting case.
    """
    half_spread = max(snapshot.spread_bps / 2.0, 1e-9)
    # Positive offset = more aggressive (further through the mid toward the
    # far touch), so probability rises with it.
    z = (limit_offset_bps + half_spread) / (half_spread * max(steepness, 1e-6))
    probability = 1.0 / (1.0 + math.exp(-z))
    return Estimate(
        value=max(0.0, min(1.0, probability)), calibrated=False,
        basis="logistic in units of half-spread; steepness is a declared "
              "default, not fitted to any fill history",
        assumptions=("steepness is a declared default",
                     "the book is assumed static over the order's life"))


def slippage_bps(*, size: float, snapshot: BookSnapshot, side: str = "buy",
                 impact: ImpactModel | None = None) -> Estimate:
    """Expected execution shortfall against the mid, in basis points.

    Half-spread to cross plus market impact. Both terms, because a model that
    reported only impact would say a one-lot trade is free, and one that
    reported only the spread would say a hundred-lot trade costs the same as a
    one-lot.
    """
    model = impact or ImpactModel()
    impact_estimate = model.impact_bps(size=size, snapshot=snapshot, side=side)
    total = snapshot.spread_bps / 2.0 + impact_estimate.value
    return Estimate(
        value=total, calibrated=impact_estimate.calibrated,
        basis=f"half-spread ({snapshot.spread_bps / 2.0:.2f} bp) + impact "
              f"({impact_estimate.value:.2f} bp)",
        assumptions=impact_estimate.assumptions)


@dataclass(frozen=True)
class LiquidityState:
    """The book's condition now, against its own recent history.

    Against its own history rather than an absolute threshold: a 40bp spread is
    normal for one instrument and a crisis for another, and an absolute limit
    would be wrong for every instrument except the one it was set on.
    """

    spread_bps: float
    median_spread_bps: float | None
    depth: float | None
    median_depth: float | None
    book_imbalance: float | None
    trade_imbalance: float | None
    quality: BookQuality
    observations: int = 0

    @property
    def spread_ratio(self) -> float | None:
        if not self.median_spread_bps:
            return None
        return self.spread_bps / self.median_spread_bps

    @property
    def depth_ratio(self) -> float | None:
        if self.depth is None or not self.median_depth:
            return None
        return self.depth / self.median_depth

    @property
    def deteriorating(self) -> bool:
        """Spread more than doubled, or depth more than halved.

        Either alone is enough. A book can widen without thinning (a nervous
        market) or thin without widening (a departing market maker), and both
        make a forecast less actionable.
        """
        spread = self.spread_ratio
        depth = self.depth_ratio
        return bool((spread is not None and spread > 2.0)
                    or (depth is not None and depth < 0.5))

    @property
    def reasons(self) -> tuple[str, ...]:
        out = []
        spread, depth = self.spread_ratio, self.depth_ratio
        if spread is not None and spread > 2.0:
            out.append(f"spread is {spread:.1f}x its median")
        if depth is not None and depth < 0.5:
            out.append(f"depth is {depth:.0%} of its median")
        if self.quality is not BookQuality.FULL:
            out.append(f"book quality is {self.quality.value}")
        return tuple(out)

    def to_dict(self) -> dict:
        return {"spread_bps": self.spread_bps,
                "median_spread_bps": self.median_spread_bps,
                "spread_ratio": self.spread_ratio, "depth": self.depth,
                "median_depth": self.median_depth, "depth_ratio": self.depth_ratio,
                "book_imbalance": self.book_imbalance,
                "trade_imbalance": self.trade_imbalance,
                "quality": self.quality.value,
                "deteriorating": self.deteriorating,
                "reasons": list(self.reasons),
                "observations": self.observations}


def assess_liquidity(current: BookSnapshot,
                     history: Sequence[BookSnapshot] = ()) -> LiquidityState:
    """Current book against its own trailing median."""
    past = [s for s in history if s.ts < current.ts]
    spreads = [s.spread_bps for s in past]
    depths = [s.total_depth for s in past if s.total_depth is not None]
    return LiquidityState(
        spread_bps=current.spread_bps,
        median_spread_bps=statistics.median(spreads) if spreads else None,
        depth=current.total_depth,
        median_depth=statistics.median(depths) if depths else None,
        book_imbalance=current.book_imbalance,
        trade_imbalance=current.trade_imbalance,
        quality=current.quality, observations=len(past))


@dataclass(frozen=True)
class TradabilityAssessment:
    """Whether a forecast is actionable. `tradable` is computed, never set.

    Six conditions, all of which must hold. A positive expected return is the
    first and the weakest — it is what makes the question worth asking, not
    what answers it.
    """

    expected_return_bps: float
    cost_bps: float
    slippage: Estimate
    fill: Estimate
    liquidity: LiquidityState
    size: float
    #: Minimum probability of filling before a forecast counts as actionable.
    min_fill_probability: float = 0.5
    #: How many times the round-trip cost the edge must be. 1.0 means "break
    #: even", which is not a reason to trade.
    min_edge_multiple: float = 1.5

    def __post_init__(self):
        if self.min_edge_multiple < 1.0:
            raise ConfigurationError(
                "requiring less than break-even is not a threshold",
                min_edge_multiple=self.min_edge_multiple)
        if not 0.0 <= self.min_fill_probability <= 1.0:
            raise ConfigurationError("min_fill_probability must be in [0, 1]")

    @property
    def total_cost_bps(self) -> float:
        return self.cost_bps + self.slippage.value

    @property
    def net_edge_bps(self) -> float:
        return abs(self.expected_return_bps) - self.total_cost_bps

    @property
    def conditions(self) -> Mapping[str, bool]:
        """The six, individually. What the explainer reports."""
        return {
            "expected_return_nonzero": self.expected_return_bps != 0.0,
            "edge_clears_costs":
                abs(self.expected_return_bps)
                >= self.total_cost_bps * self.min_edge_multiple,
            "fill_probable": self.fill.value >= self.min_fill_probability,
            "size_fits_book": self._size_fits(),
            "impact_smaller_than_edge":
                self.slippage.value < abs(self.expected_return_bps),
            "book_not_deteriorating": not self.liquidity.deteriorating,
        }

    def _size_fits(self) -> bool:
        """True when the order is a modest fraction of visible depth.

        `None` depth is treated as **not fitting**. An unknown book is not an
        infinite one, and defaulting the other way is how a size that could
        never have filled gets marked tradable.
        """
        depth = self.liquidity.depth
        if depth is None or depth <= 0:
            return False
        return self.size <= 0.25 * depth

    @property
    def tradable(self) -> bool:
        return all(self.conditions.values())

    @property
    def blocking(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.conditions.items() if not ok)

    @property
    def uncalibrated_inputs(self) -> tuple[str, ...]:
        """Which numbers here rest on a declared default rather than a fit."""
        out = []
        if not self.slippage.calibrated:
            out.append("slippage/impact")
        if not self.fill.calibrated:
            out.append("fill probability")
        return tuple(out)

    def to_dict(self) -> dict:
        return {"expected_return_bps": self.expected_return_bps,
                "cost_bps": self.cost_bps,
                "slippage": self.slippage.to_dict(),
                "fill": self.fill.to_dict(),
                "total_cost_bps": self.total_cost_bps,
                "net_edge_bps": self.net_edge_bps,
                "size": self.size,
                "conditions": dict(self.conditions),
                "tradable": self.tradable, "blocking": list(self.blocking),
                "uncalibrated_inputs": list(self.uncalibrated_inputs),
                "liquidity": self.liquidity.to_dict()}


def assess_tradability(*, expected_return_bps: float, size: float,
                       snapshot: BookSnapshot,
                       history: Sequence[BookSnapshot] = (),
                       fee_bps: float = 1.0,
                       side: str = "buy",
                       impact: ImpactModel | None = None,
                       limit_offset_bps: float = 0.0,
                       min_fill_probability: float = 0.5,
                       min_edge_multiple: float = 1.5
                       ) -> TradabilityAssessment:
    """The gate. A positive expected return is where this starts, not ends."""
    liquidity = assess_liquidity(snapshot, history)
    return TradabilityAssessment(
        expected_return_bps=float(expected_return_bps),
        cost_bps=2.0 * float(fee_bps),
        slippage=slippage_bps(size=size, snapshot=snapshot, side=side,
                              impact=impact),
        fill=fill_probability(limit_offset_bps=limit_offset_bps,
                              snapshot=snapshot),
        liquidity=liquidity, size=float(size),
        min_fill_probability=min_fill_probability,
        min_edge_multiple=min_edge_multiple)


__all__ = ["DEFAULT_IMPACT_ETA", "DEFAULT_FILL_STEEPNESS", "BookQuality",
           "BookSnapshot", "Estimate", "ImpactModel", "fill_probability",
           "slippage_bps", "LiquidityState", "assess_liquidity",
           "TradabilityAssessment", "assess_tradability"]
