"""What actually happened, compared with what Olympus expected.

Profit and loss is the least informative thing a trading system records. A
profitable trade taken for a wrong reason and a losing trade taken for a right
one look identical in the P&L column, and a system that learns from P&L alone
learns superstition. This module keeps the *whole* decision — market state,
features, forecast paths, the strategy's expectation, the risk decision, the
model and strategy versions — and scores it against the outcome along thirteen
separate axes, so the question it answers is "which part of the reasoning was
wrong" rather than "did we make money".

The structure is two records and one evaluation:

* `DecisionRecord` is written *before* the outcome is known. It is the complete
  input state, frozen at decision time. Writing it later, or amending it after
  the fact, would make every score that follows unfalsifiable — so `resolve()`
  refuses to modify one.
* `Outcome` is written when the horizon elapses or the position closes.
* `OutcomeEvaluation` is derived, never stored as input: recomputing it from
  the pair must always give the same answer.

The axis worth singling out is `abstention_would_have_been_better`. A system
that only scores the trades it took cannot discover that it should have taken
fewer, and over-trading is a far more common failure than mis-forecasting.
Every resolved decision is scored against the counterfactual of doing nothing.

Nothing here can act. This module reads outcomes and writes findings; it holds
no reference to execution, credentials, limits, or mode control, and
`kernel.audit_evolution_modules()` fails if it ever acquires one.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Regime, Side, ensure_utc, jsonable
from .errors import ConfigurationError, KnowledgeError
from .storekeys import KeyedStore

_NS = "trading.outcomes"

#: Below this many resolved decisions, a weakness "finding" is noise. Chosen to
#: match `evaluate.MIN_PAIRED_OBSERVATIONS` so the two modules cannot disagree
#: about when a sample is big enough to mean anything.
MIN_SAMPLE_FOR_WEAKNESS = 30


class ExitReason(str, Enum):
    TARGET = "target"
    STOP = "stop"
    TIME = "time"
    SIGNAL_REVERSAL = "signal_reversal"
    RISK_REDUCTION = "risk_reduction"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"
    END_OF_TEST = "end_of_test"
    UNKNOWN = "unknown"


class Grade(str, Enum):
    """A coarse verdict per axis. Deliberately not a number.

    Numbers invite averaging, and averaging "stop placement" with "forecast
    calibration" produces a meaningless score that looks authoritative. Coarse
    grades force whoever reads a report to look at the axes.
    """
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR = "poor"
    #: The honest answer when the input needed to judge is absent. Never
    #: silently GOOD — an unmeasured axis must not read as a passing one.
    UNKNOWN = "unknown"


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class DecisionRecord:
    """Everything known at decision time. Written once, never amended."""

    decision_id: str
    ts: datetime
    instrument_key: str
    timeframe: str
    #: The bar/quote state the decision saw. Kept as a mapping rather than a
    #: `Candle` so a decision made on an aggregate or a synthetic bar is
    #: representable without lying about its type.
    market_state: Mapping[str, Any] = field(default_factory=dict)
    #: Identifies the exact data the decision saw — a `CandleStore` content
    #: hash or a dataset version. Without it, "reproduce this decision" has no
    #: answer.
    data_version: str = ""
    features: Mapping[str, Any] = field(default_factory=dict)
    #: The forecast ensemble, path by path. Stored whole: the teardown found
    #: upstream Kronos averaging paths away inside inference (§12.7), and the
    #: dispersion across paths is most of the information.
    forecast_paths: tuple[tuple[float, ...], ...] = ()
    forecast_horizon: int = 0
    forecast_confidence: float | None = None
    forecast_uncertainty: float | None = None
    abstained: bool = False
    model_outputs: Mapping[str, Any] = field(default_factory=dict)
    #: What the strategy decided and why.
    strategy_id: str = ""
    strategy_version: str = ""
    side: Side | None = None
    intended_quantity: str = "0"
    entry_reference_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    expected_return: float | None = None
    #: The risk engine's verdict, as recorded — not re-derived.
    risk_verdict: str = ""
    risk_reasons: tuple[str, ...] = ()
    authorised_quantity: str = "0"
    regime: Regime = Regime.UNKNOWN
    model_versions: Mapping[str, str] = field(default_factory=dict)
    mode: str = "paper"
    correlation_id: str = ""
    tags: tuple[str, ...] = ()

    def __post_init__(self):
        if not str(self.decision_id).strip():
            raise ConfigurationError("decision_id must be non-empty")
        if not str(self.instrument_key).strip():
            raise ConfigurationError("a decision must name its instrument",
                                     decision_id=self.decision_id)
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "regime", Regime(self.regime))
        if self.side is not None:
            object.__setattr__(self, "side", Side(self.side))
        for name in ("market_state", "features", "model_outputs", "model_versions"):
            object.__setattr__(self, name, dict(getattr(self, name)))
        object.__setattr__(self, "forecast_paths",
                           tuple(tuple(float(v) for v in path)
                                 for path in self.forecast_paths))
        object.__setattr__(self, "risk_reasons", tuple(self.risk_reasons))
        object.__setattr__(self, "tags", tuple(self.tags))
        if self.abstained and self.side is not None:
            raise ConfigurationError(
                "an abstained decision cannot also carry a side; abstention is "
                "the absence of a directional call, not a flat one",
                decision_id=self.decision_id)

    @property
    def traded(self) -> bool:
        """True when this decision put risk on."""
        try:
            return float(self.authorised_quantity) > 0
        except (TypeError, ValueError):
            return False

    @property
    def forecast_mean(self) -> tuple[float, ...]:
        """Mean across paths, per step. A view, never a replacement."""
        if not self.forecast_paths:
            return ()
        steps = min(len(p) for p in self.forecast_paths)
        return tuple(statistics.fmean(p[i] for p in self.forecast_paths)
                     for i in range(steps))

    def forecast_quantile(self, q: float) -> tuple[float, ...]:
        """Per-step quantile across the ensemble."""
        if not 0.0 <= q <= 1.0:
            raise ConfigurationError("quantile must be in [0, 1]", q=q)
        if not self.forecast_paths:
            return ()
        steps = min(len(p) for p in self.forecast_paths)
        out = []
        for i in range(steps):
            column = sorted(p[i] for p in self.forecast_paths)
            idx = min(len(column) - 1, max(0, int(round(q * (len(column) - 1)))))
            out.append(column[idx])
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id, "ts": jsonable(self.ts),
            "instrument_key": self.instrument_key, "timeframe": self.timeframe,
            "market_state": jsonable(dict(self.market_state)),
            "data_version": self.data_version,
            "features": jsonable(dict(self.features)),
            "forecast_paths": [list(p) for p in self.forecast_paths],
            "forecast_horizon": self.forecast_horizon,
            "forecast_confidence": self.forecast_confidence,
            "forecast_uncertainty": self.forecast_uncertainty,
            "abstained": self.abstained,
            "model_outputs": jsonable(dict(self.model_outputs)),
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "side": self.side.value if self.side else None,
            "intended_quantity": str(self.intended_quantity),
            "entry_reference_price": self.entry_reference_price,
            "stop_price": self.stop_price, "target_price": self.target_price,
            "expected_return": self.expected_return,
            "risk_verdict": self.risk_verdict,
            "risk_reasons": list(self.risk_reasons),
            "authorised_quantity": str(self.authorised_quantity),
            "regime": self.regime.value,
            "model_versions": dict(self.model_versions), "mode": self.mode,
            "correlation_id": self.correlation_id, "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionRecord":
        return cls(
            decision_id=str(data.get("decision_id", "")),
            ts=datetime.fromisoformat(data["ts"]),
            instrument_key=str(data.get("instrument_key", "")),
            timeframe=str(data.get("timeframe", "")),
            market_state=data.get("market_state") or {},
            data_version=str(data.get("data_version", "")),
            features=data.get("features") or {},
            forecast_paths=tuple(tuple(p) for p in data.get("forecast_paths") or ()),
            forecast_horizon=int(data.get("forecast_horizon", 0)),
            forecast_confidence=_f(data.get("forecast_confidence")),
            forecast_uncertainty=_f(data.get("forecast_uncertainty")),
            abstained=bool(data.get("abstained", False)),
            model_outputs=data.get("model_outputs") or {},
            strategy_id=str(data.get("strategy_id", "")),
            strategy_version=str(data.get("strategy_version", "")),
            side=Side(data["side"]) if data.get("side") else None,
            intended_quantity=str(data.get("intended_quantity", "0")),
            entry_reference_price=_f(data.get("entry_reference_price")),
            stop_price=_f(data.get("stop_price")),
            target_price=_f(data.get("target_price")),
            expected_return=_f(data.get("expected_return")),
            risk_verdict=str(data.get("risk_verdict", "")),
            risk_reasons=tuple(data.get("risk_reasons") or ()),
            authorised_quantity=str(data.get("authorised_quantity", "0")),
            regime=Regime(data.get("regime", Regime.UNKNOWN.value)),
            model_versions=data.get("model_versions") or {},
            mode=str(data.get("mode", "paper")),
            correlation_id=str(data.get("correlation_id", "")),
            tags=tuple(data.get("tags") or ()))


@dataclass(frozen=True)
class Outcome:
    """What the market and the venue actually did."""

    decision_id: str
    resolved_at: datetime
    #: The realised price path over the forecast horizon, aligned step-for-step
    #: with `DecisionRecord.forecast_paths`.
    realised_path: tuple[float, ...] = ()
    entry_price: float | None = None
    exit_price: float | None = None
    exit_reason: ExitReason = ExitReason.UNKNOWN
    filled_quantity: str = "0"
    gross_pnl: float | None = None
    fees: float = 0.0
    slippage: float = 0.0
    #: Maximum favourable / adverse excursion while the position was open, in
    #: price terms. These are what separate "the thesis was wrong" from "the
    #: thesis was right and the stop was too tight".
    mfe: float | None = None
    mae: float | None = None
    holding_period_s: float | None = None
    #: What the same capital would have earned doing the obvious simple thing.
    baseline_pnl: float | None = None
    regime_at_exit: Regime = Regime.UNKNOWN
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "resolved_at",
                           ensure_utc(self.resolved_at, field_name="resolved_at"))
        object.__setattr__(self, "exit_reason", ExitReason(self.exit_reason))
        object.__setattr__(self, "regime_at_exit", Regime(self.regime_at_exit))
        object.__setattr__(self, "realised_path",
                           tuple(float(v) for v in self.realised_path))
        for name in ("fees", "slippage"):
            value = _f(getattr(self, name))
            if value is None or value < 0:
                raise ConfigurationError(f"{name} must be a non-negative number",
                                         decision_id=self.decision_id,
                                         got=getattr(self, name))
            object.__setattr__(self, name, value)
        if self.mfe is not None and _f(self.mfe) is not None and float(self.mfe) < 0:
            raise ConfigurationError(
                "maximum favourable excursion cannot be negative; if the trade "
                "never went in favour, it is zero",
                decision_id=self.decision_id, mfe=self.mfe)
        if self.mae is not None and _f(self.mae) is not None and float(self.mae) < 0:
            raise ConfigurationError(
                "maximum adverse excursion is recorded as a positive magnitude",
                decision_id=self.decision_id, mae=self.mae)

    @property
    def net_pnl(self) -> float | None:
        """After costs. The only P&L number worth acting on."""
        gross = _f(self.gross_pnl)
        if gross is None:
            return None
        return gross - self.fees - self.slippage

    @property
    def total_costs(self) -> float:
        return self.fees + self.slippage

    def to_dict(self) -> dict:
        return {"decision_id": self.decision_id,
                "resolved_at": jsonable(self.resolved_at),
                "realised_path": list(self.realised_path),
                "entry_price": self.entry_price, "exit_price": self.exit_price,
                "exit_reason": self.exit_reason.value,
                "filled_quantity": str(self.filled_quantity),
                "gross_pnl": self.gross_pnl, "net_pnl": self.net_pnl,
                "fees": self.fees, "slippage": self.slippage,
                "mfe": self.mfe, "mae": self.mae,
                "holding_period_s": self.holding_period_s,
                "baseline_pnl": self.baseline_pnl,
                "regime_at_exit": self.regime_at_exit.value, "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Outcome":
        return cls(
            decision_id=str(data.get("decision_id", "")),
            resolved_at=datetime.fromisoformat(data["resolved_at"]),
            realised_path=tuple(data.get("realised_path") or ()),
            entry_price=_f(data.get("entry_price")),
            exit_price=_f(data.get("exit_price")),
            exit_reason=ExitReason(data.get("exit_reason", ExitReason.UNKNOWN.value)),
            filled_quantity=str(data.get("filled_quantity", "0")),
            gross_pnl=_f(data.get("gross_pnl")),
            fees=float(data.get("fees", 0.0)),
            slippage=float(data.get("slippage", 0.0)),
            mfe=_f(data.get("mfe")), mae=_f(data.get("mae")),
            holding_period_s=_f(data.get("holding_period_s")),
            baseline_pnl=_f(data.get("baseline_pnl")),
            regime_at_exit=Regime(data.get("regime_at_exit", Regime.UNKNOWN.value)),
            notes=str(data.get("notes", "")))


@dataclass(frozen=True)
class OutcomeEvaluation:
    """Thirteen axes. Derived, reproducible, never stored as an input."""

    decision_id: str
    evaluated_at: datetime
    directional_correct: bool | None = None
    forecast_error: float | None = None          # MAE over the horizon
    forecast_calibration: Grade = Grade.UNKNOWN
    confidence_calibration: Grade = Grade.UNKNOWN
    uncertainty_accuracy: Grade = Grade.UNKNOWN
    entry_quality: Grade = Grade.UNKNOWN
    exit_quality: Grade = Grade.UNKNOWN
    sizing_quality: Grade = Grade.UNKNOWN
    stop_quality: Grade = Grade.UNKNOWN
    execution_quality: Grade = Grade.UNKNOWN
    net_performance: float | None = None
    versus_baseline: float | None = None
    abstention_would_have_been_better: bool | None = None
    #: Free-text observations, each naming the axis it came from. This is what
    #: the hypothesis engine reads.
    observations: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "evaluated_at",
                           ensure_utc(self.evaluated_at, field_name="evaluated_at"))
        object.__setattr__(self, "observations", tuple(self.observations))

    @property
    def graded_axes(self) -> dict[str, Grade]:
        return {name: getattr(self, name) for name in
                ("forecast_calibration", "confidence_calibration",
                 "uncertainty_accuracy", "entry_quality", "exit_quality",
                 "sizing_quality", "stop_quality", "execution_quality")}

    @property
    def poor_axes(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, g in self.graded_axes.items() if g is Grade.POOR))

    @property
    def unknown_axes(self) -> tuple[str, ...]:
        """Axes that could not be judged. Reported, not hidden.

        A report showing six good axes out of six, where two were unmeasurable,
        is a misleading report.
        """
        return tuple(sorted(n for n, g in self.graded_axes.items() if g is Grade.UNKNOWN))

    def to_dict(self) -> dict:
        out = {"decision_id": self.decision_id,
               "evaluated_at": jsonable(self.evaluated_at),
               "directional_correct": self.directional_correct,
               "forecast_error": self.forecast_error,
               "net_performance": self.net_performance,
               "versus_baseline": self.versus_baseline,
               "abstention_would_have_been_better":
                   self.abstention_would_have_been_better,
               "observations": list(self.observations),
               "poor_axes": list(self.poor_axes),
               "unknown_axes": list(self.unknown_axes)}
        out.update({name: grade.value for name, grade in self.graded_axes.items()})
        return out


def _grade_from(value: float | None, *, good: float, acceptable: float,
                lower_is_better: bool = True) -> Grade:
    if value is None:
        return Grade.UNKNOWN
    if lower_is_better:
        if value <= good:
            return Grade.GOOD
        return Grade.ACCEPTABLE if value <= acceptable else Grade.POOR
    if value >= good:
        return Grade.GOOD
    return Grade.ACCEPTABLE if value >= acceptable else Grade.POOR


def evaluate(decision: DecisionRecord, outcome: Outcome, *,
             clock: Clock | None = None) -> OutcomeEvaluation:
    """Score one decision against its outcome. Pure and deterministic.

    Every axis returns `UNKNOWN` rather than a default when the input needed to
    judge it is missing. That is the difference between a report that says "we
    do not know whether the stop was well placed" and one that quietly says it
    was fine.
    """
    if decision.decision_id != outcome.decision_id:
        raise ConfigurationError("decision and outcome do not refer to the same "
                                 "decision", decision=decision.decision_id,
                                 outcome=outcome.decision_id)
    now = (clock or default_clock()).now()
    observations: list[str] = []

    # -- forecast axes -----------------------------------------------------
    forecast = decision.forecast_mean
    realised = outcome.realised_path
    steps = min(len(forecast), len(realised))
    forecast_error = None
    directional = None
    if steps:
        forecast_error = statistics.fmean(
            abs(forecast[i] - realised[i]) for i in range(steps))
        anchor = decision.entry_reference_price
        if anchor is None and decision.market_state.get("close") is not None:
            anchor = _f(decision.market_state.get("close"))
        if anchor is not None:
            predicted_move = forecast[steps - 1] - anchor
            realised_move = realised[steps - 1] - anchor
            if predicted_move != 0.0 and realised_move != 0.0:
                directional = (predicted_move > 0) == (realised_move > 0)
                if directional is False:
                    observations.append(
                        f"forecast direction wrong in {decision.regime.value} "
                        f"regime on {decision.instrument_key}")

    # Calibration: forecast error relative to the spread of the ensemble. A
    # well calibrated ensemble's error lands inside its own dispersion; error
    # far outside it means the model is confidently wrong, which is worse than
    # being wrong.
    forecast_calibration = Grade.UNKNOWN
    uncertainty_accuracy = Grade.UNKNOWN
    if steps and len(decision.forecast_paths) > 1 and forecast_error is not None:
        lo = decision.forecast_quantile(0.1)
        hi = decision.forecast_quantile(0.9)
        band = statistics.fmean(
            abs(hi[i] - lo[i]) for i in range(min(len(lo), len(hi), steps)))
        if band > 0:
            ratio = forecast_error / band
            forecast_calibration = _grade_from(ratio, good=0.5, acceptable=1.0)
            inside = sum(1 for i in range(steps) if lo[i] <= realised[i] <= hi[i])
            covered = inside / steps
            # An 80% band should contain about 80% of outcomes. Both directions
            # are faults: too narrow understates risk, too wide is useless.
            uncertainty_accuracy = _grade_from(
                abs(covered - 0.8), good=0.1, acceptable=0.25)
            if covered < 0.5:
                observations.append(
                    "forecast uncertainty band too narrow: realised path left "
                    f"the 80% band {(1 - covered):.0%} of the time")

    confidence_calibration = Grade.UNKNOWN
    if directional is not None and decision.forecast_confidence is not None:
        confidence = float(decision.forecast_confidence)
        # High confidence and wrong, or low confidence and right, are both
        # miscalibration; the first is dangerous, the second wasteful.
        gap = abs((1.0 if directional else 0.0) - confidence)
        confidence_calibration = _grade_from(gap, good=0.25, acceptable=0.5)
        if directional is False and confidence >= 0.75:
            observations.append(
                f"high-confidence ({confidence:.2f}) forecast was directionally "
                f"wrong on {decision.instrument_key}")

    # -- trade axes --------------------------------------------------------
    net = outcome.net_pnl
    entry_quality = exit_quality = sizing_quality = stop_quality = Grade.UNKNOWN
    execution_quality = Grade.UNKNOWN
    abstain_better = None

    if decision.traded:
        mfe, mae = outcome.mfe, outcome.mae
        if mfe is not None and mae is not None and (mfe + mae) > 0:
            # Entry: how much of the adverse excursion came before the trade
            # went in favour at all. A good entry does not spend long underwater.
            entry_quality = _grade_from(mae / (mfe + mae), good=0.3, acceptable=0.6)
            if mfe > 0 and outcome.exit_price is not None and outcome.entry_price is not None:
                captured = abs(outcome.exit_price - outcome.entry_price) / mfe
                exit_quality = _grade_from(captured, good=0.6, acceptable=0.3,
                                           lower_is_better=False)
                if captured < 0.3 and (net or 0) > 0:
                    observations.append(
                        "exit captured less than a third of the favourable "
                        f"excursion on {decision.instrument_key}")

        if decision.stop_price is not None and outcome.entry_price is not None:
            stop_distance = abs(outcome.entry_price - decision.stop_price)
            if stop_distance > 0 and mae is not None:
                headroom = mae / stop_distance
                if outcome.exit_reason is ExitReason.STOP and mfe and mfe > stop_distance:
                    # Stopped out of a trade that had already moved further in
                    # favour than the stop was wide: the stop was too tight for
                    # this instrument's noise, not the thesis wrong.
                    stop_quality = Grade.POOR
                    observations.append(
                        f"stop on {decision.instrument_key} was tighter than the "
                        "trade's own favourable excursion before it was hit")
                else:
                    stop_quality = _grade_from(headroom, good=0.7, acceptable=0.95)
        elif decision.stop_price is None:
            observations.append("no stop recorded for a traded decision")

        expected = decision.expected_return
        if expected is not None and net is not None and outcome.entry_price:
            realised_return = net / abs(float(outcome.entry_price) *
                                        float(outcome.filled_quantity or 1) or 1)
            sizing_quality = _grade_from(
                abs(realised_return - expected),
                good=abs(expected) * 0.5 if expected else 0.01,
                acceptable=abs(expected) * 2.0 if expected else 0.05)

        if outcome.gross_pnl is not None and outcome.total_costs >= 0:
            gross = abs(outcome.gross_pnl)
            if gross > 0:
                cost_share = outcome.total_costs / gross
                execution_quality = _grade_from(cost_share, good=0.1, acceptable=0.3)
                if cost_share > 0.3:
                    observations.append(
                        f"costs consumed {cost_share:.0%} of gross P&L on "
                        f"{decision.instrument_key}")
            elif outcome.total_costs > 0:
                execution_quality = Grade.POOR
                observations.append("paid costs on a trade with no gross P&L")

        if net is not None:
            abstain_better = net < 0
            if abstain_better:
                observations.append(
                    f"abstaining would have been better: net {net:+.4f} after "
                    f"{outcome.total_costs:.4f} of costs")
    elif decision.abstained:
        # Scoring abstentions matters as much as scoring trades: a system that
        # never checks its abstentions cannot learn that it is too cautious.
        if outcome.gross_pnl is not None:
            abstain_better = outcome.gross_pnl <= 0
            if not abstain_better:
                observations.append(
                    f"abstained on {decision.instrument_key} but the move was "
                    f"favourable ({outcome.gross_pnl:+.4f} forgone)")

    versus_baseline = None
    if net is not None and outcome.baseline_pnl is not None:
        versus_baseline = net - float(outcome.baseline_pnl)
        if versus_baseline < 0:
            observations.append(
                f"underperformed the baseline by {abs(versus_baseline):.4f}")

    return OutcomeEvaluation(
        decision_id=decision.decision_id, evaluated_at=now,
        directional_correct=directional, forecast_error=forecast_error,
        forecast_calibration=forecast_calibration,
        confidence_calibration=confidence_calibration,
        uncertainty_accuracy=uncertainty_accuracy, entry_quality=entry_quality,
        exit_quality=exit_quality, sizing_quality=sizing_quality,
        stop_quality=stop_quality, execution_quality=execution_quality,
        net_performance=net, versus_baseline=versus_baseline,
        abstention_would_have_been_better=abstain_better,
        observations=tuple(observations))


# ---------------------------------------------------------------------------
# systematic weakness detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeaknessFinding:
    """A recurring error, not a bad trade.

    `sample` is mandatory and reported everywhere this is rendered: a finding
    over eight decisions is a story, not a weakness, and the number is the only
    thing that lets a reader tell the difference.
    """

    axis: str
    scope: str               # "instrument:BINANCE:BTCUSDT", "regime:ranging", …
    detail: str
    sample: int
    rate: float
    severity: str = "medium"

    def to_dict(self) -> dict:
        return {"axis": self.axis, "scope": self.scope, "detail": self.detail,
                "sample": self.sample, "rate": self.rate,
                "severity": self.severity}

    def __str__(self) -> str:                            # pragma: no cover
        return f"[{self.severity}] {self.scope} {self.axis}: {self.detail} (n={self.sample})"


def _scopes(decision: DecisionRecord) -> list[str]:
    return [f"instrument:{decision.instrument_key}",
            f"regime:{decision.regime.value}",
            f"timeframe:{decision.timeframe}",
            f"strategy:{decision.strategy_id or 'unknown'}"]


def detect_weaknesses(pairs: Sequence[tuple[DecisionRecord, OutcomeEvaluation]],
                      *, min_sample: int = MIN_SAMPLE_FOR_WEAKNESS,
                      poor_rate_threshold: float = 0.4,
                      wrong_rate_threshold: float = 0.55
                      ) -> list[WeaknessFinding]:
    """Group evaluations and report axes that fail systematically.

    Thresholds are conservative on purpose. Directional accuracy below 45% is
    reported not because it is bad but because it is *informative* — a forecast
    reliably worse than a coin flip is a forecast with the sign backwards, which
    is a fixable defect rather than a reason to stop.
    """
    buckets: dict[str, list[tuple[DecisionRecord, OutcomeEvaluation]]] = {}
    for decision, evaluation in pairs:
        for scope in _scopes(decision):
            buckets.setdefault(scope, []).append((decision, evaluation))

    findings: list[WeaknessFinding] = []
    for scope, items in sorted(buckets.items()):
        if len(items) < min_sample:
            continue

        directional = [e.directional_correct for _, e in items
                       if e.directional_correct is not None]
        if len(directional) >= min_sample:
            wrong = sum(1 for d in directional if not d) / len(directional)
            if wrong >= wrong_rate_threshold:
                findings.append(WeaknessFinding(
                    axis="directional_accuracy", scope=scope,
                    detail=f"directionally wrong {wrong:.0%} of the time — "
                           "worse than chance, which usually means a sign or "
                           "alignment defect rather than a weak model",
                    sample=len(directional), rate=wrong, severity="high"))

        for axis in ("forecast_calibration", "confidence_calibration",
                     "uncertainty_accuracy", "entry_quality", "exit_quality",
                     "sizing_quality", "stop_quality", "execution_quality"):
            graded = [getattr(e, axis) for _, e in items
                      if getattr(e, axis) is not Grade.UNKNOWN]
            if len(graded) < min_sample:
                continue
            poor = sum(1 for g in graded if g is Grade.POOR) / len(graded)
            if poor >= poor_rate_threshold:
                findings.append(WeaknessFinding(
                    axis=axis, scope=scope,
                    detail=f"graded POOR on {poor:.0%} of {len(graded)} judged "
                           "decisions",
                    sample=len(graded), rate=poor,
                    severity="high" if poor >= 0.6 else "medium"))

        abstain = [e.abstention_would_have_been_better for _, e in items
                   if e.abstention_would_have_been_better is not None]
        if len(abstain) >= min_sample:
            rate = sum(1 for a in abstain if a) / len(abstain)
            if rate >= 0.5:
                findings.append(WeaknessFinding(
                    axis="over_trading", scope=scope,
                    detail=f"doing nothing would have beaten the decision "
                           f"{rate:.0%} of the time",
                    sample=len(abstain), rate=rate,
                    severity="high" if rate >= 0.65 else "medium"))

        beaten = [e.versus_baseline for _, e in items if e.versus_baseline is not None]
        if len(beaten) >= min_sample:
            losing = sum(1 for b in beaten if b < 0) / len(beaten)
            if losing >= 0.5:
                findings.append(WeaknessFinding(
                    axis="versus_baseline", scope=scope,
                    detail=f"underperformed the baseline on {losing:.0%} of "
                           "decisions",
                    sample=len(beaten), rate=losing, severity="high"))

    return findings


# ---------------------------------------------------------------------------
# the journal
# ---------------------------------------------------------------------------


class OutcomeJournal:
    """Durable record of decisions, outcomes and their evaluations.

    Persistent because the learning loop's whole value is cross-restart: a
    weakness visible over three months of decisions is invisible in one session.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _put(self, kind: str, decision_id: str, payload: Mapping[str, Any]) -> None:
        self._store().put((kind, decision_id), jsonable(dict(payload)))

    def _get(self, kind: str, decision_id: str) -> dict | None:
        return self._store().get((kind, decision_id))

    # -- writing -----------------------------------------------------------

    def open(self, decision: DecisionRecord) -> DecisionRecord:
        """Record a decision before its outcome is known.

        Refuses to overwrite. A decision record amended after the outcome is
        known cannot falsify anything, and the entire module rests on it being
        falsifiable.
        """
        if self._get("decision", decision.decision_id) is not None:
            raise KnowledgeError(
                "this decision is already recorded; a decision record is frozen "
                "at decision time and amending it would make every later score "
                "unfalsifiable", decision_id=decision.decision_id)
        self._put("decision", decision.decision_id, decision.to_dict())
        return decision

    def resolve(self, outcome: Outcome) -> OutcomeEvaluation:
        """Attach an outcome and evaluate. Idempotent per decision."""
        raw = self._get("decision", outcome.decision_id)
        if raw is None:
            raise KnowledgeError(
                "no decision recorded for this outcome; an outcome without the "
                "state that produced it teaches nothing",
                decision_id=outcome.decision_id)
        if self._get("outcome", outcome.decision_id) is not None:
            raise KnowledgeError("this decision is already resolved",
                                 decision_id=outcome.decision_id)
        decision = DecisionRecord.from_dict(raw)
        evaluation = evaluate(decision, outcome, clock=self.clock)
        self._put("outcome", outcome.decision_id, outcome.to_dict())
        self._put("evaluation", outcome.decision_id, evaluation.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.OUTCOME_EVALUATED,
                                  evaluation.to_dict(), actor="outcomes",
                                  subject=outcome.decision_id,
                                  correlation_id=decision.correlation_id)
            except Exception:                            # noqa: BLE001
                pass
        return evaluation

    # -- reading -----------------------------------------------------------

    def decision(self, decision_id: str) -> DecisionRecord | None:
        raw = self._get("decision", decision_id)
        return DecisionRecord.from_dict(raw) if raw else None

    def outcome(self, decision_id: str) -> Outcome | None:
        raw = self._get("outcome", decision_id)
        return Outcome.from_dict(raw) if raw else None

    def decision_ids(self) -> list[str]:
        return sorted(parts[1] for parts in self._store().keys(("decision",)))

    def pending(self) -> list[str]:
        """Decisions whose outcome has not arrived. The learning backlog."""
        resolved = {parts[1] for parts in self._store().keys(("outcome",))}
        return [d for d in self.decision_ids() if d not in resolved]

    def resolved(self, *, instrument: str | None = None,
                 strategy_id: str | None = None,
                 regime: Regime | str | None = None
                 ) -> list[tuple[DecisionRecord, OutcomeEvaluation]]:
        """Every scored decision, filtered. Evaluations are recomputed, not read.

        Recomputing is the point: if `evaluate()` changes, historical decisions
        are rescored by the new logic rather than frozen under the old one, and
        a stored score can never drift from the code that claims to produce it.
        """
        out = []
        for decision_id in self.decision_ids():
            decision = self.decision(decision_id)
            outcome = self.outcome(decision_id)
            if decision is None or outcome is None:
                continue
            if instrument is not None and decision.instrument_key != instrument:
                continue
            if strategy_id is not None and decision.strategy_id != strategy_id:
                continue
            if regime is not None and decision.regime is not Regime(regime):
                continue
            out.append((decision, evaluate(decision, outcome, clock=self.clock)))
        return out

    def weaknesses(self, **kwargs) -> list[WeaknessFinding]:
        return detect_weaknesses(self.resolved(), **kwargs)

    def summary(self) -> dict:
        pairs = self.resolved()
        if not pairs:
            return {"resolved": 0, "pending": len(self.pending())}
        directional = [e.directional_correct for _, e in pairs
                       if e.directional_correct is not None]
        nets = [e.net_performance for _, e in pairs if e.net_performance is not None]
        abstain = [e.abstention_would_have_been_better for _, e in pairs
                   if e.abstention_would_have_been_better is not None]
        return {
            "resolved": len(pairs), "pending": len(self.pending()),
            "directional_accuracy": (sum(1 for d in directional if d) / len(directional)
                                     if directional else None),
            "mean_net": statistics.fmean(nets) if nets else None,
            "abstention_better_rate": (sum(1 for a in abstain if a) / len(abstain)
                                       if abstain else None),
            "weaknesses": len(self.weaknesses()),
        }

    def __repr__(self) -> str:                          # pragma: no cover
        return f"OutcomeJournal(namespace={self.namespace!r})"


__all__ = ["ExitReason", "Grade", "DecisionRecord", "Outcome",
           "OutcomeEvaluation", "evaluate", "WeaknessFinding",
           "detect_weaknesses", "OutcomeJournal", "MIN_SAMPLE_FOR_WEAKNESS"]
