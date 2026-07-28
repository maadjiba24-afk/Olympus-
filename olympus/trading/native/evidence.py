"""The learning loop: what a matured native forecast leaves behind.

> For every mature forecast, record: input dataset version, feature schema,
> model version, prediction, uncertainty, abstention decision, actual outcome,
> forecast error, calibration error, regime, instrument, timeframe, strategy
> usage, transaction costs, trade outcome where applicable.

This module is the evidence tier of Phase 4. `outcomes.py` already scores
*trade decisions* on thirteen axes; this scores *forecasts*, which is a
different object with different failure modes — a forecast can be excellent and
never traded, or traded and lose money for reasons the forecast got right.

Four decisions shape everything here.

**An abstention is evidence, not an absence of it.**
A declined forecast produces a full record: the reasons, the realised move that
followed, and whether answering would have helped. Without that, only
*insufficient* abstention is measurable — a model that declined every window
would have an unblemished error record and would look like the best model in
the system. `WeaknessKind.UNNECESSARY_ABSTENTION` exists because of this, and
it cannot be computed from answered rows alone.

**Error is `None` when nothing was predicted, never zero.**
The same rule `NativeForecast.value()` follows. A zero error on a declined
forecast is a claim of perfect accuracy, and averaging it in is how declining
becomes a strategy for winning benchmarks.

**Maturity is a fact about the clock, not a flag.**
`PendingForecast.mature()` refuses to run before `matures_at`, and there is no
argument that overrides it. A "mature" flag somebody could set is a flag that
will eventually be set early, and an early maturation reads the outcome of a
horizon that has not finished.

**A single forecast has no calibration error.**
Coverage is a property of a *set*: one observation is either inside its
interval or outside it, and 0% or 100% is not a calibration measurement. So the
record carries `interval_covered` (the atom) and `calibration_error()` is
computed over a collection with its own sample size attached. This is a
deliberate narrowing of the field list above, and it is narrower because the
per-record version would be a number with no meaning that people would
average.

Ten weaknesses
--------------
`detect_weaknesses` looks for the ten conditions Phase 4 names. Every finding
carries the measurement that produced it, the threshold it crossed, and its
sample size — and a finding below `MIN_SAMPLE` is marked **provisional** rather
than suppressed, because "we saw this three times" is worth recording and is
not worth acting on.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..contracts import DataQuality, Regime, ensure_utc, jsonable
from ..errors import ConfigurationError
from ..instruments import parse_timeframe

#: Bumped when a field's meaning changes, so an old record stays readable.
EVIDENCE_SCHEMA_VERSION = 1

#: Below this, a weakness is reported as provisional. Thirty is the same
#: threshold `outcomes.MIN_SAMPLE_FOR_WEAKNESS` and `champion.MIN_OBSERVATIONS`
#: use; keeping it identical means a finding here and a contest there are
#: judged on the same evidentiary bar.
MIN_SAMPLE = 30


class TradeOutcome(str, Enum):
    """What happened to the trade, when there was one."""

    NOT_TRADED = "not_traded"
    WIN = "win"
    LOSS = "loss"
    SCRATCH = "scratch"
    #: The strategy wanted the trade and the order never filled. Distinct from
    #: NOT_TRADED: the forecast *was* acted on and the execution failed, which
    #: is an execution-model finding rather than a forecasting one.
    UNFILLED = "unfilled"


class WeaknessKind(str, Enum):
    """The ten conditions Phase 4 requires this loop to identify."""

    MODEL_DETERIORATION = "model_deterioration"
    REGIME_WEAKNESS = "regime_weakness"
    POOR_CALIBRATION = "poor_calibration"
    EXCESS_CONFIDENCE = "excess_confidence"
    UNNECESSARY_ABSTENTION = "unnecessary_abstention"
    INSUFFICIENT_ABSTENTION = "insufficient_abstention"
    INSTRUMENT_FAILURE = "instrument_failure"
    TIMEFRAME_FAILURE = "timeframe_failure"
    DATA_QUALITY_FAILURE = "data_quality_failure"
    EXECUTION_MODEL_ERROR = "execution_model_error"


#: What each weakness means, and what a reader should do about it. Data rather
#: than prose so a proposal generator can quote the reason it was created for.
WEAKNESS_NOTES: Mapping[WeaknessKind, str] = {
    WeaknessKind.MODEL_DETERIORATION:
        "error in the recent window is materially worse than in the earlier "
        "one for the same model version; the world moved and the weights did "
        "not",
    WeaknessKind.REGIME_WEAKNESS:
        "the model is materially worse in one regime than across the whole "
        "set; an average that hides this is the average that gets traded",
    WeaknessKind.POOR_CALIBRATION:
        "realised coverage departs from nominal by more than the tolerance; "
        "the stated uncertainty does not mean what it says",
    WeaknessKind.EXCESS_CONFIDENCE:
        "narrow intervals are missing more often than wide ones — the model "
        "is most wrong exactly where it claims to be most certain",
    WeaknessKind.UNNECESSARY_ABSTENTION:
        "declined windows whose realised move was ordinary; the model refused "
        "to answer questions it could have answered",
    WeaknessKind.INSUFFICIENT_ABSTENTION:
        "answered windows with large errors that carried no abstention "
        "reason; the policy did not fire where it should have",
    WeaknessKind.INSTRUMENT_FAILURE:
        "one instrument is materially worse than the rest",
    WeaknessKind.TIMEFRAME_FAILURE:
        "one timeframe is materially worse than the rest",
    WeaknessKind.DATA_QUALITY_FAILURE:
        "forecasts made on degraded inputs are materially worse, and the "
        "degradation was visible at prediction time",
    WeaknessKind.EXECUTION_MODEL_ERROR:
        "realised transaction costs exceed the modelled ones, or trades the "
        "strategy wanted did not fill; the forecast may be right and the "
        "execution model wrong",
}


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PendingForecast:
    """A forecast that has been made and whose horizon has not elapsed.

    Separate from `ForecastEvidence` on purpose. A single type carrying an
    optional outcome would let a caller read `realised_return` before there was
    one, and `None` would be indistinguishable from "the market did not move".
    """

    forecast_id: str
    instrument_key: str
    timeframe: str
    created_at: datetime
    #: Close of the last bar the forecast consumed. The forecast is *about* the
    #: interval starting here.
    input_end: datetime
    horizon: int

    # -- provenance: every one required ------------------------------------
    dataset_version: str
    feature_schema_version: str
    model_version: str
    checkpoint_hash: str

    # -- what was predicted. Empty when the model declined. ----------------
    predicted_return: float | None = None
    quantiles: Mapping[str, float] = field(default_factory=dict)
    #: Nominal coverage of the interval `quantiles` describes, e.g. 0.8 for a
    #: p10–p90 band. `None` when no interval was produced.
    nominal_coverage: float | None = None
    uncertainty: float | None = None
    dispersion: float | None = None

    # -- the abstention decision, recorded either way ----------------------
    abstained: bool = False
    abstention_reasons: tuple[str, ...] = ()

    # -- conditions at prediction time -------------------------------------
    regime: str = Regime.UNKNOWN.value
    data_quality: DataQuality = DataQuality.OK
    anchor_close: float | None = None

    def __post_init__(self):
        for name in ("forecast_id", "instrument_key", "timeframe",
                     "dataset_version", "feature_schema_version",
                     "model_version", "checkpoint_hash"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(
                    f"{name} is required; a forecast whose provenance is "
                    f"unrecorded cannot be learned from",
                    forecast_id=self.forecast_id)
        for name in ("created_at", "input_end"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if int(self.horizon) < 1:
            raise ConfigurationError("horizon must be >= 1",
                                     forecast_id=self.forecast_id)
        object.__setattr__(self, "horizon", int(self.horizon))
        object.__setattr__(self, "quantiles", dict(self.quantiles))
        object.__setattr__(self, "abstention_reasons",
                           tuple(self.abstention_reasons))
        object.__setattr__(self, "data_quality", DataQuality(self.data_quality))
        if self.abstained and self.predicted_return is not None:
            raise ConfigurationError(
                "an abstaining forecast carries no prediction; recording one "
                "would let a declined window be scored as an answer",
                forecast_id=self.forecast_id)
        if self.abstained and not self.abstention_reasons:
            raise ConfigurationError(
                "an abstention must say why; a refusal nobody can attribute "
                "cannot be judged unnecessary or insufficient",
                forecast_id=self.forecast_id)
        if self.nominal_coverage is not None and not (
                0.0 < float(self.nominal_coverage) < 1.0):
            raise ConfigurationError("nominal_coverage must be in (0, 1)",
                                     forecast_id=self.forecast_id)

    @property
    def bar_seconds(self) -> float:
        return parse_timeframe(self.timeframe).total_seconds()

    @property
    def matures_at(self) -> datetime:
        """When the horizon this forecast describes has finished."""
        return self.input_end + timedelta(seconds=self.bar_seconds * self.horizon)

    def is_mature(self, now: datetime) -> bool:
        return ensure_utc(now, field_name="now") >= self.matures_at

    @property
    def interval(self) -> tuple[float, float] | None:
        """The widest quantile band, as `(low, high)`. `None` without one."""
        if len(self.quantiles) < 2:
            return None
        values = sorted(float(v) for v in self.quantiles.values())
        return values[0], values[-1]

    def mature(self, *, realised_return: float, now: datetime,
               realised_path: Sequence[float] = (),
               strategy_id: str = "", strategy_used: bool = False,
               modelled_cost_bps: float | None = None,
               realised_cost_bps: float | None = None,
               trade_outcome: TradeOutcome = TradeOutcome.NOT_TRADED,
               trade_return: float | None = None) -> "ForecastEvidence":
        """Close the record. Refuses before the horizon has elapsed.

        There is no `force`. An early maturation reads an outcome from a
        horizon that has not finished, and a keyword that permitted it would
        eventually be passed by a backfill script at three in the morning.
        """
        moment = ensure_utc(now, field_name="now")
        if not self.is_mature(moment):
            raise ConfigurationError(
                "this forecast's horizon has not elapsed; maturing it now "
                "would score an outcome that has not happened",
                forecast_id=self.forecast_id,
                matures_at=self.matures_at.isoformat(),
                now=moment.isoformat())
        if not math.isfinite(float(realised_return)):
            raise ConfigurationError("realised_return must be finite",
                                     forecast_id=self.forecast_id)
        if strategy_used and not str(strategy_id).strip():
            raise ConfigurationError(
                "a forecast recorded as used by a strategy must name it",
                forecast_id=self.forecast_id)
        if trade_outcome is not TradeOutcome.NOT_TRADED and not strategy_used:
            raise ConfigurationError(
                "a trade outcome without strategy usage is a trade nobody "
                "asked for", forecast_id=self.forecast_id,
                trade_outcome=TradeOutcome(trade_outcome).value)
        return ForecastEvidence(
            forecast=self, matured_at=moment,
            realised_return=float(realised_return),
            realised_path=tuple(float(p) for p in realised_path),
            strategy_id=strategy_id, strategy_used=bool(strategy_used),
            modelled_cost_bps=modelled_cost_bps,
            realised_cost_bps=realised_cost_bps,
            trade_outcome=TradeOutcome(trade_outcome),
            trade_return=trade_return)

    def to_dict(self) -> dict:
        return {"forecast_id": self.forecast_id,
                "instrument": self.instrument_key,
                "timeframe": self.timeframe,
                "created_at": jsonable(self.created_at),
                "input_end": jsonable(self.input_end),
                "matures_at": jsonable(self.matures_at),
                "horizon": self.horizon,
                "dataset_version": self.dataset_version,
                "feature_schema_version": self.feature_schema_version,
                "model_version": self.model_version,
                "checkpoint_hash": self.checkpoint_hash,
                "predicted_return": self.predicted_return,
                "quantiles": dict(self.quantiles),
                "nominal_coverage": self.nominal_coverage,
                "uncertainty": self.uncertainty,
                "dispersion": self.dispersion,
                "abstained": self.abstained,
                "abstention_reasons": list(self.abstention_reasons),
                "regime": self.regime,
                "data_quality": self.data_quality.value,
                "anchor_close": self.anchor_close}

    @classmethod
    def from_forecast(cls, forecast: Any, *, forecast_id: str,
                      regime: str = "", nominal_coverage: float | None = None
                      ) -> "PendingForecast":
        """Build from a `result.NativeForecast`.

        Reads the contract rather than the model, so anything implementing the
        22-field forecast contract can feed this loop — which is the point of
        having a contract.
        """
        quantiles = {label: float(path[-1])
                     for label, path in (forecast.quantiles or {}).items()
                     if path}
        expected = forecast.expected_returns
        predicted = float(expected[-1]) if expected else None
        declined = bool(forecast.abstention.abstained)
        timeframe = forecast.timeframes[0] if forecast.timeframes else ""
        regime_label = regime
        if not regime_label and forecast.regime_probabilities:
            regime_label = max(forecast.regime_probabilities.items(),
                               key=lambda kv: kv[1])[0]
        coverage = nominal_coverage
        if coverage is None and len(quantiles) >= 2:
            coverage = _implied_coverage(quantiles)
        return cls(
            forecast_id=forecast_id,
            instrument_key=forecast.instrument_key,
            timeframe=timeframe,
            created_at=forecast.created_at,
            input_end=forecast.input_end,
            horizon=forecast.horizon,
            dataset_version=forecast.dataset_version,
            feature_schema_version=forecast.feature_schema_version,
            model_version=forecast.model_version,
            checkpoint_hash=forecast.checkpoint_hash,
            predicted_return=None if declined else predicted,
            quantiles={} if declined else quantiles,
            nominal_coverage=None if declined else coverage,
            uncertainty=None if declined else forecast.uncertainty,
            dispersion=None if declined else forecast.dispersion,
            abstained=declined,
            abstention_reasons=tuple(forecast.abstention.reasons),
            regime=regime_label or Regime.UNKNOWN.value,
            data_quality=forecast.data_quality,
            anchor_close=forecast.anchor_close)


def _implied_coverage(quantiles: Mapping[str, float]) -> float | None:
    """Nominal coverage of the widest band, from the quantile labels."""
    from ..evaluate import quantile_level
    levels = [quantile_level(label) for label in quantiles]
    known = sorted(level for level in levels if level is not None)
    if len(known) < 2:
        return None
    return known[-1] - known[0]


@dataclass(frozen=True)
class ForecastEvidence:
    """One matured forecast. Every field Phase 4 names, and nothing inferred.

    The derived quantities — error, coverage, whether the abstention helped —
    are **properties**, not stored fields. A stored `absolute_error` is a field
    that can disagree with the prediction and the outcome it sits beside, and
    the disagreement is invisible.
    """

    forecast: PendingForecast
    matured_at: datetime
    #: Cumulative log return over the horizon, in the same units as the
    #: prediction.
    realised_return: float
    realised_path: tuple[float, ...] = ()
    strategy_id: str = ""
    strategy_used: bool = False
    modelled_cost_bps: float | None = None
    realised_cost_bps: float | None = None
    trade_outcome: TradeOutcome = TradeOutcome.NOT_TRADED
    trade_return: float | None = None
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "matured_at",
                           ensure_utc(self.matured_at, field_name="matured_at"))
        object.__setattr__(self, "realised_path", tuple(self.realised_path))
        object.__setattr__(self, "trade_outcome",
                           TradeOutcome(self.trade_outcome))

    # -- identity ----------------------------------------------------------

    @property
    def forecast_id(self) -> str:
        return self.forecast.forecast_id

    @property
    def instrument_key(self) -> str:
        return self.forecast.instrument_key

    @property
    def timeframe(self) -> str:
        return self.forecast.timeframe

    @property
    def regime(self) -> str:
        return self.forecast.regime

    @property
    def model_version(self) -> str:
        return self.forecast.model_version

    @property
    def abstained(self) -> bool:
        return self.forecast.abstained

    @property
    def answered(self) -> bool:
        return not self.forecast.abstained

    # -- error -------------------------------------------------------------

    @property
    def signed_error(self) -> float | None:
        """Prediction minus outcome. `None` when nothing was predicted."""
        if self.forecast.predicted_return is None:
            return None
        return self.forecast.predicted_return - self.realised_return

    @property
    def absolute_error(self) -> float | None:
        error = self.signed_error
        return abs(error) if error is not None else None

    @property
    def directionally_correct(self) -> bool | None:
        """`None` when declined, and `None` when the outcome was exactly flat —
        a zero move has no direction to get right."""
        predicted = self.forecast.predicted_return
        if predicted is None or self.realised_return == 0.0 or predicted == 0.0:
            return None
        return (predicted > 0) == (self.realised_return > 0)

    # -- calibration -------------------------------------------------------

    @property
    def interval_covered(self) -> bool | None:
        """Did the realised return fall inside the predicted band?

        `None` when there was no band. This is the *atom* of calibration; the
        error is `calibration_error` over a set, because one observation
        covering or not covering is 100% or 0% and neither is a measurement.
        """
        interval = self.forecast.interval
        if interval is None:
            return None
        low, high = interval
        return low <= self.realised_return <= high

    @property
    def interval_width(self) -> float | None:
        interval = self.forecast.interval
        return (interval[1] - interval[0]) if interval else None

    # -- was declining the right call? -------------------------------------

    def abstention_avoided_error(self, *, typical_move: float) -> bool | None:
        """Did declining avoid a large move? `None` when it did not decline.

        The counterfactual is deliberately weak, and stating that is part of
        the answer: we do not know what this model would have predicted for a
        window it declined. What we do know is what the market did, and a
        decline before an ordinary move is a decline that cost information for
        nothing.
        """
        if not self.abstained or typical_move <= 0:
            return None
        return abs(self.realised_return) > typical_move

    # -- execution ---------------------------------------------------------

    @property
    def cost_overrun_bps(self) -> float | None:
        """Realised minus modelled cost. `None` when either is unrecorded —
        never zero, because "we did not measure it" and "it matched" are the
        two answers this distinction exists to keep apart."""
        if self.realised_cost_bps is None or self.modelled_cost_bps is None:
            return None
        return self.realised_cost_bps - self.modelled_cost_bps

    @property
    def unmeasured_fields(self) -> tuple[str, ...]:
        """What this record could not supply. Reported, never defaulted."""
        missing = []
        if self.forecast.predicted_return is None and not self.abstained:
            missing.append("prediction: answered with no point forecast")
        if self.forecast.interval is None and not self.abstained:
            missing.append("interval: no quantiles, so coverage is unknowable")
        if self.modelled_cost_bps is None:
            missing.append("modelled_cost_bps")
        if self.realised_cost_bps is None:
            missing.append("realised_cost_bps")
        if not self.strategy_used:
            missing.append("strategy usage: this forecast was never acted on")
        return tuple(missing)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "forecast": self.forecast.to_dict(),
                "matured_at": jsonable(self.matured_at),
                "realised_return": self.realised_return,
                "realised_path": list(self.realised_path),
                "signed_error": self.signed_error,
                "absolute_error": self.absolute_error,
                "directionally_correct": self.directionally_correct,
                "interval_covered": self.interval_covered,
                "interval_width": self.interval_width,
                "strategy_id": self.strategy_id,
                "strategy_used": self.strategy_used,
                "modelled_cost_bps": self.modelled_cost_bps,
                "realised_cost_bps": self.realised_cost_bps,
                "cost_overrun_bps": self.cost_overrun_bps,
                "trade_outcome": self.trade_outcome.value,
                "trade_return": self.trade_return,
                "unmeasured": list(self.unmeasured_fields)}


# ---------------------------------------------------------------------------
# aggregates
# ---------------------------------------------------------------------------

def calibration_error(records: Sequence[ForecastEvidence]) -> float | None:
    """Realised minus nominal coverage, in coverage points. `None` unmeasurable.

    Positive means the intervals are too wide. Computed over the records that
    actually carried an interval, and `None` — never zero — when none did.
    """
    scored = [(r.interval_covered, r.forecast.nominal_coverage)
              for r in records
              if r.interval_covered is not None
              and r.forecast.nominal_coverage is not None]
    if not scored:
        return None
    realised = sum(1 for covered, _ in scored if covered) / len(scored)
    nominal = statistics.fmean(nominal for _, nominal in scored)
    return (realised - nominal) * 100.0


def mean_absolute_error(records: Sequence[ForecastEvidence]) -> float | None:
    """Over answered records only. `None` when nothing was answered.

    Scoped to answered rows for the reason Phase 2's evaluation was: including
    declined rows as zero credits the model with perfect accuracy on every
    window it refused.
    """
    errors = [r.absolute_error for r in records if r.absolute_error is not None]
    return statistics.fmean(errors) if errors else None


def abstention_rate(records: Sequence[ForecastEvidence]) -> float | None:
    if not records:
        return None
    return sum(1 for r in records if r.abstained) / len(records)


def typical_move(records: Sequence[ForecastEvidence]) -> float | None:
    """Median absolute realised return. The reference an abstention is judged
    against, computed from the outcomes rather than declared."""
    moves = [abs(r.realised_return) for r in records]
    return statistics.median(moves) if moves else None


@dataclass(frozen=True)
class EvidenceSummary:
    """One scope's numbers, with the sample size that produced them."""

    scope: str
    n: int
    n_answered: int
    mae: float | None
    calibration_error: float | None
    abstention_rate: float | None
    directional_accuracy: float | None
    mean_cost_overrun_bps: float | None

    @property
    def usable(self) -> bool:
        return self.n >= MIN_SAMPLE

    def to_dict(self) -> dict:
        return {"scope": self.scope, "n": self.n, "n_answered": self.n_answered,
                "mae": self.mae, "calibration_error": self.calibration_error,
                "abstention_rate": self.abstention_rate,
                "directional_accuracy": self.directional_accuracy,
                "mean_cost_overrun_bps": self.mean_cost_overrun_bps,
                "usable": self.usable}


def summarise(records: Sequence[ForecastEvidence], *,
              scope: str = "all") -> EvidenceSummary:
    answered = [r for r in records if r.answered]
    directions = [r.directionally_correct for r in records
                  if r.directionally_correct is not None]
    overruns = [r.cost_overrun_bps for r in records
                if r.cost_overrun_bps is not None]
    return EvidenceSummary(
        scope=scope, n=len(records), n_answered=len(answered),
        mae=mean_absolute_error(records),
        calibration_error=calibration_error(records),
        abstention_rate=abstention_rate(records),
        directional_accuracy=(sum(1 for d in directions if d) / len(directions)
                              if directions else None),
        mean_cost_overrun_bps=(statistics.fmean(overruns) if overruns else None))


# ---------------------------------------------------------------------------
# weakness detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeaknessFinding:
    """One measured weakness, with the number that produced it."""

    kind: WeaknessKind
    scope: str
    detail: str
    measured: float
    threshold: float
    n: int
    #: Records the finding is drawn from, so a proposal can cite them.
    forecast_ids: tuple[str, ...] = ()
    model_version: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", WeaknessKind(self.kind))
        object.__setattr__(self, "forecast_ids", tuple(self.forecast_ids))

    @property
    def provisional(self) -> bool:
        """True below `MIN_SAMPLE`. Recorded, not acted on."""
        return self.n < MIN_SAMPLE

    @property
    def note(self) -> str:
        return WEAKNESS_NOTES[self.kind]

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "scope": self.scope,
                "detail": self.detail, "measured": self.measured,
                "threshold": self.threshold, "n": self.n,
                "provisional": self.provisional, "note": self.note,
                "model_version": self.model_version,
                "forecast_ids": list(self.forecast_ids[:20])}


@dataclass(frozen=True)
class WeaknessThresholds:
    """Where each line is drawn. Frozen and recorded with every scan."""

    #: Ratio of recent MAE to earlier MAE above which deterioration is called.
    deterioration_ratio: float = 1.25
    #: Ratio of a stratum's MAE to the overall MAE.
    stratum_ratio: float = 1.30
    #: Absolute coverage error, in points.
    max_coverage_error: float = 10.0
    #: Coverage gap between the narrowest and widest interval terciles.
    excess_confidence_gap: float = 15.0
    #: Fraction of declined windows whose move was ordinary.
    unnecessary_abstention_rate: float = 0.7
    #: Fraction of answered windows whose error exceeded the large-error line.
    insufficient_abstention_rate: float = 0.2
    #: Multiple of the median absolute error that counts as a large error.
    large_error_multiple: float = 3.0
    #: Realised-minus-modelled cost, in basis points.
    max_cost_overrun_bps: float = 2.0
    #: Fraction of wanted trades that failed to fill.
    max_unfilled_rate: float = 0.1

    def to_dict(self) -> dict:
        return {"deterioration_ratio": self.deterioration_ratio,
                "stratum_ratio": self.stratum_ratio,
                "max_coverage_error": self.max_coverage_error,
                "excess_confidence_gap": self.excess_confidence_gap,
                "unnecessary_abstention_rate": self.unnecessary_abstention_rate,
                "insufficient_abstention_rate":
                    self.insufficient_abstention_rate,
                "large_error_multiple": self.large_error_multiple,
                "max_cost_overrun_bps": self.max_cost_overrun_bps,
                "max_unfilled_rate": self.max_unfilled_rate}


def _by(records: Sequence[ForecastEvidence], key) -> dict[str, list]:
    out: dict[str, list] = {}
    for record in records:
        out.setdefault(key(record), []).append(record)
    return out


def _stratum_findings(records: Sequence[ForecastEvidence], *, key, kind,
                      label: str, limits: WeaknessThresholds
                      ) -> list[WeaknessFinding]:
    """One weakness per stratum whose MAE is materially worse than the whole."""
    overall = mean_absolute_error(records)
    if overall is None or overall <= 0:
        return []
    findings = []
    for name, group in sorted(_by(records, key).items()):
        group_mae = mean_absolute_error(group)
        if group_mae is None:
            continue
        ratio = group_mae / overall
        if ratio > limits.stratum_ratio:
            findings.append(WeaknessFinding(
                kind=kind, scope=f"{label}={name}",
                detail=f"MAE {group_mae:.6f} against {overall:.6f} overall "
                       f"({ratio:.2f}x)",
                measured=ratio, threshold=limits.stratum_ratio,
                n=len(group),
                forecast_ids=tuple(r.forecast_id for r in group),
                model_version=group[0].model_version))
    return findings


def detect_weaknesses(records: Sequence[ForecastEvidence], *,
                      thresholds: WeaknessThresholds | None = None
                      ) -> list[WeaknessFinding]:
    """The ten conditions, each measured against a stated threshold.

    Every finding is returned, including provisional ones. Filtering them here
    would mean a caller could not tell "we looked and found nothing" from "we
    found something and did not have enough of it" — and those lead to
    different next actions.
    """
    limits = thresholds or WeaknessThresholds()
    records = list(records)
    if not records:
        return []
    findings: list[WeaknessFinding] = []

    # 1. deterioration — recent half against earlier half, same model version.
    for version, group in sorted(_by(records, lambda r: r.model_version).items()):
        ordered = sorted(group, key=lambda r: r.matured_at)
        half = len(ordered) // 2
        if half < 2:
            continue
        earlier = mean_absolute_error(ordered[:half])
        recent = mean_absolute_error(ordered[half:])
        if earlier and recent and earlier > 0:
            ratio = recent / earlier
            if ratio > limits.deterioration_ratio:
                findings.append(WeaknessFinding(
                    kind=WeaknessKind.MODEL_DETERIORATION,
                    scope=f"model={version}",
                    detail=f"recent MAE {recent:.6f} against earlier "
                           f"{earlier:.6f} ({ratio:.2f}x)",
                    measured=ratio, threshold=limits.deterioration_ratio,
                    n=len(ordered),
                    forecast_ids=tuple(r.forecast_id for r in ordered[half:]),
                    model_version=version))

    # 2, 7, 8. per-stratum weakness — regime, instrument, timeframe.
    findings += _stratum_findings(records, key=lambda r: r.regime,
                                  kind=WeaknessKind.REGIME_WEAKNESS,
                                  label="regime", limits=limits)
    findings += _stratum_findings(records, key=lambda r: r.instrument_key,
                                  kind=WeaknessKind.INSTRUMENT_FAILURE,
                                  label="instrument", limits=limits)
    findings += _stratum_findings(records, key=lambda r: r.timeframe,
                                  kind=WeaknessKind.TIMEFRAME_FAILURE,
                                  label="timeframe", limits=limits)

    # 3. calibration.
    error = calibration_error(records)
    if error is not None and abs(error) > limits.max_coverage_error:
        scored = [r for r in records if r.interval_covered is not None]
        findings.append(WeaknessFinding(
            kind=WeaknessKind.POOR_CALIBRATION, scope="all",
            detail=f"coverage error {error:+.1f} points "
                   f"({'too wide' if error > 0 else 'too narrow'})",
            measured=abs(error), threshold=limits.max_coverage_error,
            n=len(scored),
            forecast_ids=tuple(r.forecast_id for r in scored),
            model_version=records[0].model_version))

    # 4. excess confidence — narrow intervals missing more than wide ones.
    banded = sorted((r for r in records
                     if r.interval_width is not None
                     and r.interval_covered is not None),
                    key=lambda r: r.interval_width)
    if len(banded) >= 6:
        third = len(banded) // 3
        narrow, wide = banded[:third], banded[-third:]
        narrow_coverage = sum(1 for r in narrow if r.interval_covered) / len(narrow)
        wide_coverage = sum(1 for r in wide if r.interval_covered) / len(wide)
        gap = (wide_coverage - narrow_coverage) * 100.0
        if gap > limits.excess_confidence_gap:
            findings.append(WeaknessFinding(
                kind=WeaknessKind.EXCESS_CONFIDENCE, scope="all",
                detail=f"narrowest third covers {narrow_coverage:.0%}, widest "
                       f"third covers {wide_coverage:.0%}",
                measured=gap, threshold=limits.excess_confidence_gap,
                n=len(banded),
                forecast_ids=tuple(r.forecast_id for r in narrow),
                model_version=records[0].model_version))

    # 5. unnecessary abstention.
    declined = [r for r in records if r.abstained]
    reference = typical_move(records)
    if declined and reference:
        ordinary = [r for r in declined
                    if r.abstention_avoided_error(typical_move=reference) is False]
        rate = len(ordinary) / len(declined)
        if rate > limits.unnecessary_abstention_rate:
            findings.append(WeaknessFinding(
                kind=WeaknessKind.UNNECESSARY_ABSTENTION, scope="all",
                detail=f"{len(ordinary)} of {len(declined)} declines were "
                       f"followed by a move at or below the median "
                       f"({reference:.6f})",
                measured=rate, threshold=limits.unnecessary_abstention_rate,
                n=len(declined),
                forecast_ids=tuple(r.forecast_id for r in ordinary),
                model_version=records[0].model_version))

    # 6. insufficient abstention.
    answered = [r for r in records if r.absolute_error is not None]
    if answered:
        errors = sorted(r.absolute_error for r in answered)
        median_error = statistics.median(errors)
        line = median_error * limits.large_error_multiple
        blown = [r for r in answered if r.absolute_error > line]
        rate = len(blown) / len(answered)
        if line > 0 and rate > limits.insufficient_abstention_rate:
            findings.append(WeaknessFinding(
                kind=WeaknessKind.INSUFFICIENT_ABSTENTION, scope="all",
                detail=f"{len(blown)} of {len(answered)} answered forecasts "
                       f"exceeded {limits.large_error_multiple:.0f}x the median "
                       f"error and carried no abstention",
                measured=rate, threshold=limits.insufficient_abstention_rate,
                n=len(answered),
                forecast_ids=tuple(r.forecast_id for r in blown),
                model_version=records[0].model_version))

    # 9. data quality — degraded inputs producing worse forecasts.
    degraded = [r for r in records if r.forecast.data_quality is not DataQuality.OK]
    clean = [r for r in records if r.forecast.data_quality is DataQuality.OK]
    degraded_mae, clean_mae = mean_absolute_error(degraded), mean_absolute_error(clean)
    if degraded_mae and clean_mae and clean_mae > 0:
        ratio = degraded_mae / clean_mae
        if ratio > limits.stratum_ratio:
            findings.append(WeaknessFinding(
                kind=WeaknessKind.DATA_QUALITY_FAILURE, scope="degraded_inputs",
                detail=f"MAE {degraded_mae:.6f} on degraded inputs against "
                       f"{clean_mae:.6f} on clean ({ratio:.2f}x)",
                measured=ratio, threshold=limits.stratum_ratio,
                n=len(degraded),
                forecast_ids=tuple(r.forecast_id for r in degraded),
                model_version=degraded[0].model_version))

    # 10. execution model — cost overruns and unfilled orders.
    overruns = [r.cost_overrun_bps for r in records
                if r.cost_overrun_bps is not None]
    if overruns:
        mean_overrun = statistics.fmean(overruns)
        if mean_overrun > limits.max_cost_overrun_bps:
            measured = [r for r in records if r.cost_overrun_bps is not None]
            findings.append(WeaknessFinding(
                kind=WeaknessKind.EXECUTION_MODEL_ERROR, scope="costs",
                detail=f"realised cost exceeds modelled by "
                       f"{mean_overrun:.2f} bp on average",
                measured=mean_overrun, threshold=limits.max_cost_overrun_bps,
                n=len(overruns),
                forecast_ids=tuple(r.forecast_id for r in measured),
                model_version=records[0].model_version))
    wanted = [r for r in records if r.strategy_used]
    if wanted:
        unfilled = [r for r in wanted
                    if r.trade_outcome is TradeOutcome.UNFILLED]
        rate = len(unfilled) / len(wanted)
        if rate > limits.max_unfilled_rate:
            findings.append(WeaknessFinding(
                kind=WeaknessKind.EXECUTION_MODEL_ERROR, scope="fills",
                detail=f"{len(unfilled)} of {len(wanted)} wanted trades did "
                       f"not fill",
                measured=rate, threshold=limits.max_unfilled_rate,
                n=len(wanted),
                forecast_ids=tuple(r.forecast_id for r in unfilled),
                model_version=records[0].model_version))

    return findings


# ---------------------------------------------------------------------------
# the journal
# ---------------------------------------------------------------------------

class EvidenceJournal:
    """Pending forecasts in, matured evidence out. Append-only.

    Deliberately in-memory and explicit rather than wired to `olympus.store`:
    this is the object an experiment runs over, and an experiment that reached
    a shared store would be an experiment that could write to one.
    `to_dict`/`from_dict` are how it crosses a process boundary.
    """

    __slots__ = ("_pending", "_matured")

    def __init__(self, records: Sequence[ForecastEvidence] = ()):
        self._pending: dict[str, PendingForecast] = {}
        self._matured: list[ForecastEvidence] = []
        for record in records:
            self._matured.append(record)

    def record(self, forecast: PendingForecast) -> PendingForecast:
        if forecast.forecast_id in self._pending:
            raise ConfigurationError("this forecast is already pending",
                                     forecast_id=forecast.forecast_id)
        if any(r.forecast_id == forecast.forecast_id for r in self._matured):
            raise ConfigurationError(
                "this forecast has already matured; re-recording it would let "
                "one forecast be scored twice",
                forecast_id=forecast.forecast_id)
        self._pending[forecast.forecast_id] = forecast
        return forecast

    def mature(self, forecast_id: str, **kwargs) -> ForecastEvidence:
        """Mature a pending forecast and move it to the evidence set."""
        pending = self._pending.get(forecast_id)
        if pending is None:
            raise ConfigurationError("no such pending forecast",
                                     forecast_id=forecast_id)
        evidence = pending.mature(**kwargs)
        del self._pending[forecast_id]
        self._matured.append(evidence)
        return evidence

    def due(self, now: datetime) -> tuple[PendingForecast, ...]:
        """Pending forecasts whose horizon has elapsed."""
        moment = ensure_utc(now, field_name="now")
        return tuple(f for f in self._pending.values() if f.is_mature(moment))

    @property
    def pending(self) -> tuple[PendingForecast, ...]:
        return tuple(self._pending.values())

    @property
    def matured(self) -> tuple[ForecastEvidence, ...]:
        return tuple(self._matured)

    def __len__(self) -> int:
        return len(self._matured)

    def __iter__(self) -> Iterator[ForecastEvidence]:
        return iter(self._matured)

    def for_model(self, model_version: str) -> tuple[ForecastEvidence, ...]:
        return tuple(r for r in self._matured
                     if r.model_version == model_version)

    def weaknesses(self, *, thresholds: WeaknessThresholds | None = None
                   ) -> list[WeaknessFinding]:
        return detect_weaknesses(self._matured, thresholds=thresholds)

    def summary(self) -> EvidenceSummary:
        return summarise(self._matured)

    def strata(self) -> dict[str, EvidenceSummary]:
        """Per-regime, per-instrument and per-timeframe summaries.

        Reported alongside the overall one because Phase 2 established that an
        average hides the stratum a live system meets first.
        """
        out: dict[str, EvidenceSummary] = {"all": self.summary()}
        for label, key in (("regime", lambda r: r.regime),
                           ("instrument", lambda r: r.instrument_key),
                           ("timeframe", lambda r: r.timeframe)):
            for name, group in sorted(_by(self._matured, key).items()):
                scope = f"{label}={name}"
                out[scope] = summarise(group, scope=scope)
        return out

    def report(self) -> dict:
        findings = self.weaknesses()
        return {"schema_version": EVIDENCE_SCHEMA_VERSION,
                "pending": len(self._pending),
                "matured": len(self._matured),
                "summary": self.summary().to_dict(),
                "strata": {k: v.to_dict() for k, v in self.strata().items()},
                "weaknesses": [f.to_dict() for f in findings],
                "actionable": [f.to_dict() for f in findings
                               if not f.provisional],
                "provisional": [f.kind.value for f in findings if f.provisional]}


__all__ = ["EVIDENCE_SCHEMA_VERSION", "MIN_SAMPLE", "TradeOutcome",
           "WeaknessKind", "WEAKNESS_NOTES", "PendingForecast",
           "ForecastEvidence", "calibration_error", "mean_absolute_error",
           "abstention_rate", "typical_move", "EvidenceSummary", "summarise",
           "WeaknessFinding", "WeaknessThresholds", "detect_weaknesses",
           "EvidenceJournal"]
