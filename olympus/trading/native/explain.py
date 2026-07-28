"""Structured explanations: reason codes and evidence, never prose as proof.

> Do not use free-form explanation as proof that a prediction is correct.

Enforced, not requested. Every explanation is a list of `ReasonCode`s from a
closed enumeration, each with the *measurement* that triggered it. There is a
`narrative()` method and it is generated **from** the codes — so the prose
cannot say anything the codes do not, and `Explanation.evidence_only` is what a
machine consumes.

The distinction that makes this worth having
---------------------------------------------
An explanation says **why this forecast came out this way** and **what would
make it wrong**. It does not say the forecast is right, and there is no field
in which it could:

* `reasons` — what drove the number, each with a measured value;
* `uncertainty_sources` — where the width came from, ranked;
* `data_limitations` — what was missing, stale or proxied;
* `risk_factors` — what would hurt if it is wrong;
* `invalidated_by` — the conditions under which it should be discarded.

`invalidated_by` is required and non-empty. An explanation with nothing that
would falsify it is a description of a number, not an account of it.

Reason codes are stable strings
-------------------------------
They travel into audit records and outlive the code that emitted them, so they
are an enumeration rather than formatted text. A code whose meaning changes
needs a new code, not a new message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when a reason code's meaning changes.
EXPLANATION_SCHEMA_VERSION = 1


class ReasonCode(str, Enum):
    """Why a forecast came out the way it did. A closed set, on purpose."""

    # -- what drove the point forecast -----------------------------------
    TIMEFRAME_DOMINANT = "timeframe_dominant"
    TREND_CONTINUATION = "trend_continuation"
    MEAN_REVERSION_PULL = "mean_reversion_pull"
    CROSS_ASSET_LEAD = "cross_asset_lead"
    REGIME_CONDITIONED = "regime_conditioned"
    SPECIALIST_ROUTED = "specialist_routed"
    PORTFOLIO_CONDITIONED = "portfolio_conditioned"
    EVENT_PROXIMITY = "event_proximity"

    # -- where the uncertainty came from ---------------------------------
    UNCERTAINTY_VOLATILITY = "uncertainty_volatility"
    UNCERTAINTY_HORIZON = "uncertainty_horizon"
    UNCERTAINTY_REGIME_AMBIGUITY = "uncertainty_regime_ambiguity"
    UNCERTAINTY_THIN_SUPPORT = "uncertainty_thin_support"
    UNCERTAINTY_CALIBRATION = "uncertainty_calibration"
    UNCERTAINTY_MODEL_DISAGREEMENT = "uncertainty_model_disagreement"

    # -- what the data could not supply ----------------------------------
    DATA_TIMEFRAME_UNUSABLE = "data_timeframe_unusable"
    DATA_CHANNEL_MISSING = "data_channel_missing"
    DATA_STALE = "data_stale"
    DATA_PROXY_USED = "data_proxy_used"
    DATA_REFERENCE_MISSING = "data_reference_missing"
    DATA_UNCALIBRATED_ESTIMATE = "data_uncalibrated_estimate"

    # -- what would hurt --------------------------------------------------
    RISK_LIQUIDITY = "risk_liquidity"
    RISK_EVENT = "risk_event"
    RISK_CONCENTRATION = "risk_concentration"
    RISK_DRAWDOWN = "risk_drawdown"
    RISK_CROWDED_CORRELATION = "risk_crowded_correlation"
    RISK_OUT_OF_DISTRIBUTION = "risk_out_of_distribution"


class ReasonCategory(str, Enum):
    DRIVER = "driver"
    UNCERTAINTY = "uncertainty"
    DATA_LIMITATION = "data_limitation"
    RISK_FACTOR = "risk_factor"


#: Which category each code belongs to, and what it means. A code missing from
#: here cannot be emitted — `Reason` looks it up and raises.
CODE_META: Mapping[ReasonCode, tuple[ReasonCategory, str]] = {
    ReasonCode.TIMEFRAME_DOMINANT:
        (ReasonCategory.DRIVER,
         "one timeframe contributed most of the signal"),
    ReasonCode.TREND_CONTINUATION:
        (ReasonCategory.DRIVER, "recent directional drift was extrapolated"),
    ReasonCode.MEAN_REVERSION_PULL:
        (ReasonCategory.DRIVER, "price was far from a local mean and the "
                                "forecast pulls back toward it"),
    ReasonCode.CROSS_ASSET_LEAD:
        (ReasonCategory.DRIVER, "a related instrument moved first"),
    ReasonCode.REGIME_CONDITIONED:
        (ReasonCategory.DRIVER, "the regime classification shaped the forecast"),
    ReasonCode.SPECIALIST_ROUTED:
        (ReasonCategory.DRIVER, "a regime specialist answered rather than the "
                                "generalist"),
    ReasonCode.PORTFOLIO_CONDITIONED:
        (ReasonCategory.DRIVER, "existing portfolio state was part of the input"),
    ReasonCode.EVENT_PROXIMITY:
        (ReasonCategory.DRIVER, "a scheduled event sits inside or near the "
                                "horizon"),

    ReasonCode.UNCERTAINTY_VOLATILITY:
        (ReasonCategory.UNCERTAINTY, "realised volatility is elevated"),
    ReasonCode.UNCERTAINTY_HORIZON:
        (ReasonCategory.UNCERTAINTY, "the horizon is long relative to the "
                                     "signal's persistence"),
    ReasonCode.UNCERTAINTY_REGIME_AMBIGUITY:
        (ReasonCategory.UNCERTAINTY, "the regime classification is near a "
                                     "boundary"),
    ReasonCode.UNCERTAINTY_THIN_SUPPORT:
        (ReasonCategory.UNCERTAINTY, "few training windows resembled this one"),
    ReasonCode.UNCERTAINTY_CALIBRATION:
        (ReasonCategory.UNCERTAINTY, "the model's recorded coverage error is "
                                     "large, so its interval width is itself "
                                     "uncertain"),
    ReasonCode.UNCERTAINTY_MODEL_DISAGREEMENT:
        (ReasonCategory.UNCERTAINTY, "the direction head and the median "
                                     "disagree about the sign"),

    ReasonCode.DATA_TIMEFRAME_UNUSABLE:
        (ReasonCategory.DATA_LIMITATION, "a timeframe was partial, delayed or "
                                         "absent and was excluded"),
    ReasonCode.DATA_CHANNEL_MISSING:
        (ReasonCategory.DATA_LIMITATION, "a declared channel was not observed"),
    ReasonCode.DATA_STALE:
        (ReasonCategory.DATA_LIMITATION, "an input was older than its "
                                         "staleness tolerance"),
    ReasonCode.DATA_PROXY_USED:
        (ReasonCategory.DATA_LIMITATION, "a proxy stood in for the quantity "
                                         "actually wanted"),
    ReasonCode.DATA_REFERENCE_MISSING:
        (ReasonCategory.DATA_LIMITATION, "a declared cross-asset reference had "
                                         "no usable data"),
    ReasonCode.DATA_UNCALIBRATED_ESTIMATE:
        (ReasonCategory.DATA_LIMITATION, "an estimate rests on a declared "
                                         "default rather than a fit"),

    ReasonCode.RISK_LIQUIDITY:
        (ReasonCategory.RISK_FACTOR, "the book is thin or widening"),
    ReasonCode.RISK_EVENT:
        (ReasonCategory.RISK_FACTOR, "a high-importance event could reprice "
                                     "the instrument inside the horizon"),
    ReasonCode.RISK_CONCENTRATION:
        (ReasonCategory.RISK_FACTOR, "the portfolio is already concentrated"),
    ReasonCode.RISK_DRAWDOWN:
        (ReasonCategory.RISK_FACTOR, "the portfolio is below its peak"),
    ReasonCode.RISK_CROWDED_CORRELATION:
        (ReasonCategory.RISK_FACTOR, "correlated exposure means this position "
                                     "diversifies less than it appears"),
    ReasonCode.RISK_OUT_OF_DISTRIBUTION:
        (ReasonCategory.RISK_FACTOR, "the input is unlike the training set"),
}


@dataclass(frozen=True)
class Reason:
    """One code, with the number that triggered it.

    `measured` is what makes this evidence rather than assertion. A reason
    saying "volatility is elevated" with no value attached is a sentence; one
    saying "volatility ratio 3.2 against a threshold of 2.0" is a claim someone
    can check and disagree with.
    """

    code: ReasonCode
    measured: float | None = None
    threshold: float | None = None
    detail: str = ""
    #: Weight in [0, 1] when the emitter can rank its reasons.
    weight: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "code", ReasonCode(self.code))
        if self.code not in CODE_META:                   # pragma: no cover
            raise ConfigurationError("reason code has no declared meaning",
                                     code=self.code.value)
        if self.weight is not None and not 0.0 <= float(self.weight) <= 1.0:
            raise ConfigurationError("weight must be in [0, 1]",
                                     code=self.code.value, weight=self.weight)

    @property
    def category(self) -> ReasonCategory:
        return CODE_META[self.code][0]

    @property
    def meaning(self) -> str:
        return CODE_META[self.code][1]

    def to_dict(self) -> dict:
        return {"code": self.code.value, "category": self.category.value,
                "meaning": self.meaning, "measured": self.measured,
                "threshold": self.threshold, "detail": self.detail,
                "weight": self.weight}


@dataclass(frozen=True)
class Explanation:
    """Why this forecast came out this way, and what would make it wrong.

    `narrative()` is generated from the codes and is offered for a human
    reading a log. It is never the thing a machine consumes, and it cannot
    contain a claim the codes do not carry — because it is assembled from them.
    """

    as_of: datetime
    instrument_key: str
    reasons: tuple[Reason, ...]
    #: Conditions under which this forecast should be discarded. Required.
    invalidated_by: tuple[str, ...]
    dominant_timeframes: tuple[str, ...] = ()
    cross_asset_relationships: tuple[str, ...] = ()
    regime: str = ""
    specialist: str = ""
    schema_version: int = EXPLANATION_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "as_of",
                           ensure_utc(self.as_of, field_name="as_of"))
        for name in ("reasons", "invalidated_by", "dominant_timeframes",
                     "cross_asset_relationships"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not self.reasons:
            raise ConfigurationError(
                "an explanation with no reasons explains nothing",
                instrument=self.instrument_key)
        if not self.invalidated_by:
            raise ConfigurationError(
                "an explanation must state what would falsify the forecast; "
                "one that cannot be falsified is a description of a number, "
                "not an account of it", instrument=self.instrument_key)

    def by_category(self, category: ReasonCategory) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.category is category)

    @property
    def drivers(self) -> tuple[Reason, ...]:
        return self.by_category(ReasonCategory.DRIVER)

    @property
    def uncertainty_sources(self) -> tuple[Reason, ...]:
        return self.by_category(ReasonCategory.UNCERTAINTY)

    @property
    def data_limitations(self) -> tuple[Reason, ...]:
        return self.by_category(ReasonCategory.DATA_LIMITATION)

    @property
    def risk_factors(self) -> tuple[Reason, ...]:
        return self.by_category(ReasonCategory.RISK_FACTOR)

    @property
    def dominant_uncertainty(self) -> Reason | None:
        """The largest-weighted uncertainty source, or `None` when unranked.

        `None` rather than the first one: "the main source of uncertainty" with
        nothing behind the ranking is exactly the kind of unfounded claim this
        module exists to prevent.
        """
        ranked = [r for r in self.uncertainty_sources if r.weight is not None]
        return max(ranked, key=lambda r: r.weight) if ranked else None

    @property
    def evidence_only(self) -> Mapping[str, Any]:
        """What a machine consumes. Codes and numbers, no prose."""
        return {
            "codes": [r.code.value for r in self.reasons],
            "measurements": {r.code.value: r.measured for r in self.reasons
                             if r.measured is not None},
            "dominant_timeframes": list(self.dominant_timeframes),
            "cross_asset": list(self.cross_asset_relationships),
            "regime": self.regime, "specialist": self.specialist,
            "invalidated_by": list(self.invalidated_by),
        }

    def narrative(self) -> str:
        """Prose assembled from the codes. Never a source of new claims.

        Deliberately dull. A narrative that read well would invite being
        quoted as the reason, and the reason is the code list.
        """
        lines = []
        if self.drivers:
            lines.append("Driven by: " + "; ".join(
                _phrase(r) for r in self.drivers))
        if self.uncertainty_sources:
            lines.append("Uncertainty from: " + "; ".join(
                _phrase(r) for r in self.uncertainty_sources))
        if self.data_limitations:
            lines.append("Data limitations: " + "; ".join(
                _phrase(r) for r in self.data_limitations))
        if self.risk_factors:
            lines.append("Risks: " + "; ".join(
                _phrase(r) for r in self.risk_factors))
        lines.append("This forecast should be discarded if: "
                     + "; ".join(self.invalidated_by))
        lines.append("These are the conditions the forecast rests on. They are "
                     "not evidence that it is correct.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        dominant = self.dominant_uncertainty
        return {"as_of": jsonable(self.as_of),
                "instrument": self.instrument_key,
                "reasons": [r.to_dict() for r in self.reasons],
                "drivers": [r.code.value for r in self.drivers],
                "uncertainty_sources": [r.code.value
                                        for r in self.uncertainty_sources],
                "dominant_uncertainty":
                    dominant.code.value if dominant else None,
                "data_limitations": [r.code.value for r in self.data_limitations],
                "risk_factors": [r.code.value for r in self.risk_factors],
                "dominant_timeframes": list(self.dominant_timeframes),
                "cross_asset_relationships": list(self.cross_asset_relationships),
                "regime": self.regime, "specialist": self.specialist,
                "invalidated_by": list(self.invalidated_by),
                "evidence_only": dict(self.evidence_only),
                "schema_version": self.schema_version}


def _phrase(reason: Reason) -> str:
    text = reason.detail or reason.meaning
    if reason.measured is None:
        return text
    if reason.threshold is None:
        return f"{text} ({reason.measured:.4g})"
    return f"{text} ({reason.measured:.4g} vs {reason.threshold:.4g})"


class ExplanationBuilder:
    """Accumulates reasons, then builds. Refuses to build an empty one."""

    __slots__ = ("_reasons", "_invalidators", "_timeframes", "_cross_asset",
                 "regime", "specialist")

    def __init__(self):
        self._reasons: list[Reason] = []
        self._invalidators: list[str] = []
        self._timeframes: list[str] = []
        self._cross_asset: list[str] = []
        self.regime = ""
        self.specialist = ""

    def add(self, code: ReasonCode, *, measured: float | None = None,
            threshold: float | None = None, detail: str = "",
            weight: float | None = None) -> "ExplanationBuilder":
        self._reasons.append(Reason(code=code, measured=measured,
                                    threshold=threshold, detail=detail,
                                    weight=weight))
        return self

    def invalidated_by(self, *conditions: str) -> "ExplanationBuilder":
        self._invalidators.extend(c for c in conditions if c.strip())
        return self

    def timeframes(self, *names: str) -> "ExplanationBuilder":
        self._timeframes.extend(names)
        return self

    def cross_asset(self, *names: str) -> "ExplanationBuilder":
        self._cross_asset.extend(names)
        return self

    def build(self, *, as_of: datetime, instrument_key: str) -> Explanation:
        return Explanation(
            as_of=as_of, instrument_key=instrument_key,
            reasons=tuple(self._reasons),
            invalidated_by=tuple(dict.fromkeys(self._invalidators)),
            dominant_timeframes=tuple(dict.fromkeys(self._timeframes)),
            cross_asset_relationships=tuple(dict.fromkeys(self._cross_asset)),
            regime=self.regime, specialist=self.specialist)


def explain_forecast(*, as_of: datetime, instrument_key: str,
                     timeframe_view: Any = None,
                     cross_asset: Any = None,
                     event_context: Any = None,
                     portfolio: Any = None,
                     routing: Any = None,
                     liquidity: Any = None,
                     tradability: Any = None,
                     forecast: Any = None) -> Explanation:
    """Assemble an explanation from whatever context was actually available.

    Every argument is optional and every branch is guarded, because the point
    of an explanation is to be produced *especially* when things are missing —
    a forecast made with three of eight timeframes and no order book is exactly
    the one whose limitations a reader needs.
    """
    builder = ExplanationBuilder()

    if timeframe_view is not None:
        usable = list(getattr(timeframe_view, "usable_timeframes", ()))
        unusable = dict(getattr(timeframe_view, "unusable", {}))
        if usable:
            builder.timeframes(*usable)
            builder.add(ReasonCode.TIMEFRAME_DOMINANT,
                        measured=float(len(usable)),
                        detail=f"usable timeframes: {', '.join(usable)}")
        for name, state in sorted(unusable.items()):
            builder.add(ReasonCode.DATA_TIMEFRAME_UNUSABLE,
                        detail=f"{name} was {state}")
        builder.invalidated_by(
            "a currently-unusable timeframe becoming available and "
            "contradicting the usable ones")

    if cross_asset is not None:
        strongest = cross_asset.strongest() if hasattr(cross_asset, "strongest") \
            else None
        if strongest is not None:
            builder.cross_asset(strongest.reference)
            builder.add(ReasonCode.CROSS_ASSET_LEAD,
                        measured=strongest.correlation,
                        detail=f"{strongest.reference} "
                               f"({strongest.kind.value})")
            builder.invalidated_by(
                f"{strongest.reference} decoupling from this instrument")
        for name in getattr(cross_asset, "missing", ()):
            builder.add(ReasonCode.DATA_REFERENCE_MISSING,
                        detail=f"{name} had no usable data")

    if event_context is not None and getattr(event_context, "in_event_window",
                                             False):
        builder.add(ReasonCode.EVENT_PROXIMITY,
                    measured=event_context.bars_until_next,
                    detail=f"{getattr(event_context.next_kind, 'value', 'event')}"
                           f" is near")
        builder.add(ReasonCode.RISK_EVENT,
                    detail="a high-importance event could reprice the "
                           "instrument inside the horizon")
        builder.invalidated_by("the event resolving differently from the "
                               "distribution's centre")

    if portfolio is not None:
        builder.add(ReasonCode.PORTFOLIO_CONDITIONED,
                    measured=getattr(portfolio, "existing_exposure", None),
                    detail=f"existing position is "
                           f"{getattr(portfolio, 'existing_side', 'flat')}")
        if getattr(portfolio, "adding_would_concentrate", False):
            builder.add(ReasonCode.RISK_CONCENTRATION,
                        measured=getattr(portfolio, "concentration", None))
        if (getattr(portfolio, "drawdown", 0.0) or 0.0) > 0.1:
            builder.add(ReasonCode.RISK_DRAWDOWN,
                        measured=portfolio.drawdown, threshold=0.1)
        correlated = getattr(portfolio, "correlated_exposure", None)
        if correlated is not None and abs(correlated) > 0.5:
            builder.add(ReasonCode.RISK_CROWDED_CORRELATION,
                        measured=correlated, threshold=0.5)

    if routing is not None:
        builder.specialist = getattr(routing.chosen, "value", str(routing.chosen))
        builder.add(ReasonCode.SPECIALIST_ROUTED,
                    measured=routing.margin,
                    detail=routing.reason)
        if getattr(routing, "used_fallback", False):
            builder.add(ReasonCode.UNCERTAINTY_THIN_SUPPORT,
                        measured=float(getattr(routing, "support", 0)),
                        detail="the generalist answered: " + routing.reason)
        builder.invalidated_by(
            "the regime changing enough to route to a different specialist")

    if liquidity is not None and getattr(liquidity, "deteriorating", False):
        builder.add(ReasonCode.RISK_LIQUIDITY,
                    measured=getattr(liquidity, "spread_ratio", None),
                    detail="; ".join(getattr(liquidity, "reasons", ())))

    if tradability is not None:
        for name in getattr(tradability, "uncalibrated_inputs", ()):
            builder.add(ReasonCode.DATA_UNCALIBRATED_ESTIMATE, detail=name)

    if forecast is not None:
        regime = getattr(forecast, "most_likely_regime", "")
        if regime:
            builder.regime = regime
            builder.add(ReasonCode.REGIME_CONDITIONED, detail=regime)
        realised = getattr(forecast, "realised_volatility", None)
        if realised is not None:
            builder.add(ReasonCode.UNCERTAINTY_VOLATILITY, measured=realised,
                        weight=0.5)
        ood = getattr(forecast, "ood_score", None)
        if ood is not None and ood > 0.8:
            builder.add(ReasonCode.RISK_OUT_OF_DISTRIBUTION, measured=ood,
                        threshold=0.8)
        calibration = getattr(forecast, "calibration", None)
        error = getattr(calibration, "coverage_error", None)
        if error is not None and abs(error) > 10.0:
            builder.add(ReasonCode.UNCERTAINTY_CALIBRATION, measured=abs(error),
                        threshold=10.0, weight=0.8)
        for warning in getattr(forecast, "warnings", ()):
            if "disagree" in warning:
                builder.add(ReasonCode.UNCERTAINTY_MODEL_DISAGREEMENT,
                            detail=warning, weight=0.7)
        builder.invalidated_by(
            "realised volatility moving outside the range the model was "
            "trained on")

    if not builder._reasons:
        builder.add(ReasonCode.UNCERTAINTY_THIN_SUPPORT,
                    detail="no context was supplied, so nothing about this "
                           "forecast can be explained beyond the model's own "
                           "output")
    builder.invalidated_by("any input channel this forecast used becoming "
                           "unavailable")
    return builder.build(as_of=as_of, instrument_key=instrument_key)


__all__ = ["EXPLANATION_SCHEMA_VERSION", "ReasonCode", "ReasonCategory",
           "CODE_META", "Reason", "Explanation", "ExplanationBuilder",
           "explain_forecast"]
