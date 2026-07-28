"""Matched evaluation: Olympus against an external reference, and against everything else.

> The purpose of this phase is to determine whether the Olympus-native market
> model is genuinely superior, complementary or inferior to the external reference model.
> Do not begin with the assumption that Olympus is better.

This module is built so that the assumption *cannot* be made. Three mechanisms
do that work, and each of them can produce an answer nobody here wants.

**The verdict is computed, and one of its branches is unreachable by effort.**
`MatchedReport.verdict` returns `INSUFFICIENT_EVIDENCE` whenever the reference arm
did not run — whatever the other five arms did. There is no argument that
overrides it, because the question this phase asks is *about the reference*, and five
arms beating each other does not answer it. A harness that reported "Olympus
clearly superior" from a run with no reference in it would be answering a
different question in the same words.

**An unavailable arm is carried, not dropped.**
`ArmAvailability` records why an arm could not run and which blocker caused it.
A report that silently omitted the arm it could not build would look like a
complete comparison. `MatchedReport.missing_arms` is in every rendering.

**The fairness contract is enforced rather than intended.**
`FairnessContract` fingerprints the twelve things Phase 5 requires to be
identical — instruments, window, prediction timestamps, horizon, input
availability, data quality, splits, costs, risk limits, strategy rules,
execution assumptions — and `compare_arms` **raises** when two arms carry
different fingerprints. Comparing a challenger measured on cheap costs against
an incumbent measured on realistic ones is the most common way research
produces an improvement that does not exist, and it is not detectable after the
fact from the numbers alone.

Input advantage is a separate axis from architecture
-----------------------------------------------------
> If Olympus uses order-book and event data while the reference receives only candles,
> report a candle-only Olympus comparison and a full-feature Olympus
> comparison.

`InputClass` makes that structural: every arm declares what it was allowed to
see, and `compare_arms` refuses a cross-class comparison unless the caller says
`allow_input_advantage=True`, in which case the advantage is recorded on the
comparison. A candle-only Olympus losing and a full-feature Olympus winning is
a finding about *data*, and reporting it as a finding about architecture would
be the phase's central error.

Why the incumbent is unnamed here
----------------------------------
The arm this phase exists to compare against is `EXTERNAL_REFERENCE`, and that
is not evasion — it is gate G2. A native module carrying a competitor's name in
an enum value is a native module whose vocabulary is shaped by that competitor,
and `tests/test_trading_independence.py` refuses it. The identity lives in the
adapter that owns it, which supplies the label through
`matched_reference_label()`; this is the same resolution Phase 2 reached when
`evaluation.py` needed to name its opponent. Reports and scripts sit outside the
independence boundary and name it plainly, so nothing is hidden from a reader —
only from the import graph.

Multiple comparisons
--------------------
Six arms over fifteen forecast metrics over eight strata is several hundred
tests, and at α = 0.05 roughly one in twenty of them is significant by
accident. `holm_bonferroni` adjusts the family and `Comparison.significant`
reads the *adjusted* p-value. The raw one is kept beside it, because the size
of the correction is itself information about how much searching was done.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError
from ..evaluate import SignificanceResult, paired_bootstrap

#: Bumped when the contract, the arm set or the metric set changes.
MATCHED_SCHEMA_VERSION = 1

#: Minimum paired observations before a comparison is reportable at all.
MIN_PAIRED = 30


class ArmKind(str, Enum):
    """The six systems Phase 5 requires to be compared.

    The incumbent is `EXTERNAL_REFERENCE` — see the module docstring on why
    this module does not name it.
    """

    PERSISTENCE = "persistence"
    STATISTICAL = "statistical"
    MACHINE_LEARNING = "machine_learning"
    EXTERNAL_REFERENCE = "external_reference"
    OLYMPUS_NATIVE = "olympus_native"
    ENSEMBLE = "ensemble"


#: All six. A report missing any of them says so rather than renumbering.
REQUIRED_ARMS: tuple[ArmKind, ...] = tuple(ArmKind)


class InputClass(str, Enum):
    """What an arm was allowed to see. The axis that separates architecture
    quality from additional-data advantage."""

    #: OHLCV only. The class every arm can be run in, and therefore the only
    #: class in which an architecture claim can be made.
    CANDLES_ONLY = "candles_only"
    #: Candles plus channels Olympus derives from them itself — realised
    #: volatility, regime label, calendar. No new source of information; a
    #: representation advantage rather than a data advantage.
    DERIVED = "derived"
    #: Plus order book, trades and events. A genuine data advantage.
    FULL_FEATURE = "full_feature"


INPUT_CLASS_NOTES: Mapping[InputClass, str] = {
    InputClass.CANDLES_ONLY:
        "OHLCV only — the class in which every arm can run, and therefore the "
        "only one in which a claim about architecture is meaningful",
    InputClass.DERIVED:
        "candles plus quantities Olympus computes from them; no new "
        "information enters, so a gain here is representation rather than data",
    InputClass.FULL_FEATURE:
        "plus order book, aggressor flow and events — information the "
        "candle-only arms genuinely do not have",
}


class Verdict(str, Enum):
    """The six decision categories, and nothing else."""

    OLYMPUS_CLEARLY_SUPERIOR = "olympus_clearly_superior"
    OLYMPUS_SUPERIOR_IN_REGIMES = "olympus_superior_only_in_specific_regimes"
    OLYMPUS_COMPLEMENTARY = "olympus_complementary_to_reference"
    INDISTINGUISHABLE = "statistically_indistinguishable"
    REFERENCE_SUPERIOR = "external_reference_superior"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class Promotion(str, Enum):
    """What follows from the verdict. Also six, and also computed."""

    OLYMPUS_REPLACES_REFERENCE = "olympus_replaces_the_reference"
    OLYMPUS_ROUTES_SELECTED_REGIMES = "olympus_routes_selected_regimes"
    ENSEMBLE = "olympus_and_reference_ensemble"
    REFERENCE_REMAINS_CHAMPION = "the_reference_remains_champion"
    SIMPLE_BASELINE_PREFERRED = "both_rejected_in_favour_of_a_simple_baseline"
    NO_DECISION = "no_promotion_decision_possible"


# ===========================================================================
# the fairness contract
# ===========================================================================

@dataclass(frozen=True)
class FairnessContract:
    """The twelve things every arm must share, as one fingerprintable record.

    Fingerprinted rather than compared field by field at each call site: a
    check somebody has to remember to write is a check that will be missing
    from the one comparison that mattered.
    """

    instruments: tuple[str, ...]
    first_ts: datetime
    last_ts: datetime
    #: Hash over the exact prediction instants. Two arms scored on windows that
    #: merely *overlap* are not matched, and a count alone would not show it.
    prediction_timestamps_hash: str
    n_prediction_timestamps: int
    horizon: int
    lookback: int
    input_availability_rule: str
    data_quality_rule: str
    split_scheme: str
    n_folds: int
    embargo_bars: int
    cost_model: Mapping[str, Any]
    risk_limits: Mapping[str, Any]
    strategy_rules: str
    execution_assumptions: str
    schema_version: int = MATCHED_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "instruments", tuple(self.instruments))
        for name in ("first_ts", "last_ts"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        object.__setattr__(self, "cost_model", dict(self.cost_model))
        object.__setattr__(self, "risk_limits", dict(self.risk_limits))
        if not self.instruments:
            raise ConfigurationError("a contract must name its instruments")
        if self.n_prediction_timestamps < 1:
            raise ConfigurationError("a contract needs prediction timestamps")
        for name in ("input_availability_rule", "data_quality_rule",
                     "split_scheme", "strategy_rules", "execution_assumptions"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(
                    f"{name} must be stated; an unstated rule is one nobody "
                    f"can check two arms shared", field=name)
        if not self.cost_model:
            raise ConfigurationError(
                "a contract must carry its cost model by value; a reference to "
                "one that can also change is not a matched comparison")

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "instruments": list(self.instruments),
                "first_ts": jsonable(self.first_ts),
                "last_ts": jsonable(self.last_ts),
                "prediction_timestamps_hash": self.prediction_timestamps_hash,
                "n_prediction_timestamps": self.n_prediction_timestamps,
                "horizon": self.horizon, "lookback": self.lookback,
                "input_availability_rule": self.input_availability_rule,
                "data_quality_rule": self.data_quality_rule,
                "split_scheme": self.split_scheme, "n_folds": self.n_folds,
                "embargo_bars": self.embargo_bars,
                "cost_model": dict(self.cost_model),
                "risk_limits": dict(self.risk_limits),
                "strategy_rules": self.strategy_rules,
                "execution_assumptions": self.execution_assumptions}

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def differences(self, other: "FairnessContract") -> tuple[str, ...]:
        """Which fields differ. What a reader needs when a comparison raises."""
        mine, theirs = self.to_dict(), other.to_dict()
        return tuple(f"{key}: {mine[key]!r} vs {theirs[key]!r}"
                     for key in sorted(mine)
                     if mine[key] != theirs.get(key))


def timestamps_hash(timestamps: Sequence[datetime]) -> str:
    """Order-independent digest of the exact prediction instants."""
    ordered = sorted(ensure_utc(t, field_name="ts").isoformat()
                     for t in timestamps)
    return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()[:32]


# ===========================================================================
# arms
# ===========================================================================

@dataclass(frozen=True)
class ArmAvailability:
    """Whether an arm ran, and — when it did not — why, by blocker."""

    available: bool
    reason: str = ""
    blockers: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if not self.available and not str(self.reason).strip():
            raise ConfigurationError(
                "an unavailable arm must say why; an arm that is simply "
                "absent reads as an arm nobody thought of")

    def to_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason,
                "blockers": list(self.blockers)}


@dataclass(frozen=True)
class ArmSpec:
    """One system under test: what it is, what it saw, whether it ran."""

    kind: ArmKind
    name: str
    input_class: InputClass
    availability: ArmAvailability
    parameters: int | None = None
    #: Identity of the weights, when there are any. Empty for a rule.
    model_hash: str = ""
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", ArmKind(self.kind))
        object.__setattr__(self, "input_class", InputClass(self.input_class))
        if not str(self.name).strip():
            raise ConfigurationError("an arm must be named")

    @property
    def available(self) -> bool:
        return self.availability.available

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "name": self.name,
                "input_class": self.input_class.value,
                "input_class_note": INPUT_CLASS_NOTES[self.input_class],
                "availability": self.availability.to_dict(),
                "parameters": self.parameters, "model_hash": self.model_hash,
                "notes": self.notes}


def unavailable(kind: ArmKind, name: str, *, reason: str,
                blockers: Sequence[str] = (),
                input_class: InputClass = InputClass.CANDLES_ONLY) -> ArmSpec:
    """An arm that could not be built. Carried in the report, never dropped."""
    return ArmSpec(kind=kind, name=name, input_class=input_class,
                   availability=ArmAvailability(available=False, reason=reason,
                                                blockers=tuple(blockers)))


# ===========================================================================
# metrics
# ===========================================================================

def _opt(values: Iterable[Any]) -> list[float]:
    return [float(v) for v in values
            if v is not None and isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(float(v))]


@dataclass(frozen=True)
class ForecastMetrics:
    """The fifteen Phase 5 names. Every one optional, `None` when unmeasured.

    Optional throughout because the arms genuinely differ in what they emit: a
    persistence rule has no Brier score and no regime head, and reporting zero
    for those would make it look calibrated and make the model that does emit
    them look worse by comparison.
    """

    n: int = 0
    n_answered: int = 0
    mae: float | None = None
    rmse: float | None = None
    directional_accuracy: float | None = None
    quantile_loss: float | None = None
    brier: float | None = None
    calibration_error: float | None = None
    volatility_error: float | None = None
    regime_accuracy: float | None = None
    drawdown_risk_quality: float | None = None
    abstention_precision: float | None = None
    abstention_recall: float | None = None
    coverage: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    memory_kb: int | None = None
    reliability: float | None = None

    @property
    def measured(self) -> tuple[str, ...]:
        return tuple(name for name in _FORECAST_FIELDS
                     if getattr(self, name) is not None)

    @property
    def unmeasured(self) -> tuple[str, ...]:
        return tuple(name for name in _FORECAST_FIELDS
                     if getattr(self, name) is None)

    def to_dict(self) -> dict:
        payload = {name: getattr(self, name) for name in _FORECAST_FIELDS}
        payload.update({"n": self.n, "n_answered": self.n_answered,
                        "unmeasured": list(self.unmeasured)})
        return payload


_FORECAST_FIELDS: tuple[str, ...] = (
    "mae", "rmse", "directional_accuracy", "quantile_loss", "brier",
    "calibration_error", "volatility_error", "regime_accuracy",
    "drawdown_risk_quality", "abstention_precision", "abstention_recall",
    "coverage", "latency_ms_p50", "memory_kb", "reliability")

#: Which direction is better. Data, so a reader can check the comparison logic
#: without reading it. `coverage` is absent on purpose: closeness to nominal is
#: what matters, and `calibration_error` already carries that as a distance.
FORECAST_DIRECTION: Mapping[str, str] = {
    "mae": "lower", "rmse": "lower", "directional_accuracy": "higher",
    "quantile_loss": "lower", "brier": "lower", "calibration_error": "lower",
    "volatility_error": "lower", "regime_accuracy": "higher",
    "drawdown_risk_quality": "higher", "abstention_precision": "higher",
    "abstention_recall": "higher", "latency_ms_p50": "lower",
    "memory_kb": "lower", "reliability": "higher",
}


@dataclass(frozen=True)
class TradingMetrics:
    """The eighteen Phase 5 names, from one matched strategy applied to all."""

    total_return: float | None = None
    annualised_return: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    calmar: float | None = None
    profit_factor: float | None = None
    turnover: float | None = None
    fees: float | None = None
    slippage: float | None = None
    exposure: float | None = None
    trade_count: int = 0
    #: Non-overlapping periods the metrics were computed from, and how many
    #: overlapping windows were dropped to get there.
    n_periods: int = 0
    n_dropped_overlapping: int = 0
    win_rate: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    #: Mean of the worst 5% of trade returns. `None` below twenty trades — a
    #: tail estimated from one observation is not an estimate.
    tail_loss: float | None = None

    @property
    def unmeasured(self) -> tuple[str, ...]:
        return tuple(name for name in _TRADING_FIELDS
                     if getattr(self, name) is None)

    def to_dict(self) -> dict:
        payload = {name: getattr(self, name) for name in _TRADING_FIELDS}
        payload.update({"trade_count": self.trade_count,
                        "n_periods": self.n_periods,
                        "n_dropped_overlapping": self.n_dropped_overlapping,
                        "unmeasured": list(self.unmeasured)})
        return payload


_TRADING_FIELDS: tuple[str, ...] = (
    "total_return", "annualised_return", "sharpe", "sortino", "max_drawdown",
    "calmar", "profit_factor", "turnover", "fees", "slippage", "exposure",
    "win_rate", "average_win", "average_loss", "tail_loss")

TRADING_DIRECTION: Mapping[str, str] = {
    "total_return": "higher", "annualised_return": "higher",
    "sharpe": "higher", "sortino": "higher", "max_drawdown": "lower",
    "calmar": "higher", "profit_factor": "higher", "turnover": "lower",
    "fees": "lower", "slippage": "lower", "exposure": "higher",
    "win_rate": "higher", "average_win": "higher", "average_loss": "higher",
    "tail_loss": "higher",
}


# ===========================================================================
# per-window records
# ===========================================================================

@dataclass(frozen=True)
class WindowRecord:
    """One arm's output for one prediction instant, plus what happened.

    The unit of the paired comparison. Keyed by `key`, so two arms are paired
    on the *same instant* rather than by position — two arms with different
    abstention behaviour produce different-length series, and zipping them
    would pair one arm's window with another arm's different window.
    """

    key: str
    instrument: str
    ts: datetime
    regime: str
    #: Realised cumulative log return over the horizon.
    realised: float
    #: `None` when the arm declined or could not answer.
    predicted: float | None = None
    quantiles: Mapping[str, float] = field(default_factory=dict)
    nominal_coverage: float | None = None
    predicted_volatility: float | None = None
    realised_volatility: float | None = None
    direction_probability: float | None = None
    predicted_regime: str = ""
    drawdown_probability: float | None = None
    realised_drawdown: float | None = None
    abstained: bool = False
    failed: bool = False
    latency_ms: float | None = None
    #: Strategy output under the matched rule: -1, 0 or +1.
    position: int = 0
    #: Net return after costs from taking that position.
    net_return: float | None = None
    cost_paid: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "ts", ensure_utc(self.ts, field_name="ts"))
        object.__setattr__(self, "quantiles", dict(self.quantiles))

    @property
    def answered(self) -> bool:
        return self.predicted is not None and not self.abstained and not self.failed

    @property
    def absolute_error(self) -> float | None:
        return abs(self.predicted - self.realised) if self.answered else None

    @property
    def covered(self) -> bool | None:
        if not self.answered or len(self.quantiles) < 2:
            return None
        values = sorted(self.quantiles.values())
        return values[0] <= self.realised <= values[-1]

    def to_dict(self) -> dict:
        return {"key": self.key, "instrument": self.instrument,
                "ts": jsonable(self.ts), "regime": self.regime,
                "realised": self.realised, "predicted": self.predicted,
                "abstained": self.abstained, "failed": self.failed,
                "position": self.position, "net_return": self.net_return,
                "absolute_error": self.absolute_error, "covered": self.covered}


def _pinball(quantiles: Mapping[str, float], realised: float) -> float | None:
    from ..evaluate import quantile_level
    total, count = 0.0, 0
    for label, value in quantiles.items():
        level = quantile_level(label)
        if level is None:
            continue
        delta = realised - float(value)
        total += max(level * delta, (level - 1.0) * delta)
        count += 1
    return total / count if count else None


def forecast_metrics(records: Sequence[WindowRecord], *,
                     memory_kb: int | None = None) -> ForecastMetrics:
    """The fifteen, from the records. Every one `None` when unmeasurable.

    Accuracy metrics are scoped to **answered** rows. Including a decline as
    zero error credits the arm with perfect accuracy on every window it
    refused, which is how declining becomes a strategy for winning benchmarks.
    """
    rows = list(records)
    if not rows:
        return ForecastMetrics()
    answered = [r for r in rows if r.answered]
    errors = [r.absolute_error for r in answered]

    covered = [r.covered for r in answered if r.covered is not None]
    nominal = _opt(r.nominal_coverage for r in answered)
    coverage = (sum(1 for c in covered if c) / len(covered)) if covered else None
    calibration = (abs(coverage - statistics.fmean(nominal)) * 100.0
                   if coverage is not None and nominal else None)

    directions = [(r.predicted > 0) == (r.realised > 0) for r in answered
                  if r.predicted != 0.0 and r.realised != 0.0]
    pinballs = _opt(_pinball(r.quantiles, r.realised) for r in answered)

    # Brier over the direction head, when there is one.
    brier_terms = [(r.direction_probability - (1.0 if r.realised > 0 else 0.0)) ** 2
                   for r in answered if r.direction_probability is not None]
    volatility_terms = [abs(r.predicted_volatility - r.realised_volatility)
                        for r in answered
                        if r.predicted_volatility is not None
                        and r.realised_volatility is not None]
    regime_terms = [r.predicted_regime == r.regime for r in answered
                    if r.predicted_regime]

    # Drawdown-risk quality: rank correlation between the predicted
    # probability and the realised drawdown. A probability that does not order
    # the outcomes is a probability that says nothing, whatever its calibration.
    drawdown_quality = _rank_correlation(
        [(r.drawdown_probability, r.realised_drawdown) for r in answered
         if r.drawdown_probability is not None and r.realised_drawdown is not None])

    precision, recall = _abstention_quality(rows)
    latencies = sorted(_opt(r.latency_ms for r in rows))
    failures = sum(1 for r in rows if r.failed)

    def percentile(values: Sequence[float], q: float) -> float | None:
        if not values:
            return None
        return values[min(len(values) - 1, int(q * (len(values) - 1)))]

    return ForecastMetrics(
        n=len(rows), n_answered=len(answered),
        mae=statistics.fmean(errors) if errors else None,
        rmse=(math.sqrt(statistics.fmean([e * e for e in errors]))
              if errors else None),
        directional_accuracy=(sum(1 for d in directions if d) / len(directions)
                              if directions else None),
        quantile_loss=statistics.fmean(pinballs) if pinballs else None,
        brier=statistics.fmean(brier_terms) if brier_terms else None,
        calibration_error=calibration,
        volatility_error=(statistics.fmean(volatility_terms)
                          if volatility_terms else None),
        regime_accuracy=(sum(1 for r in regime_terms if r) / len(regime_terms)
                         if regime_terms else None),
        drawdown_risk_quality=drawdown_quality,
        abstention_precision=precision, abstention_recall=recall,
        coverage=coverage,
        latency_ms_p50=percentile(latencies, 0.5),
        latency_ms_p95=percentile(latencies, 0.95),
        memory_kb=memory_kb,
        reliability=1.0 - failures / len(rows))


def _rank_correlation(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Spearman, by hand. `None` below ten pairs or with no variation."""
    clean = [(float(a), float(b)) for a, b in pairs
             if math.isfinite(a) and math.isfinite(b)]
    if len(clean) < 10:
        return None

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    left, right = ranks([p[0] for p in clean]), ranks([p[1] for p in clean])
    mean_left, mean_right = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a - mean_left) * (b - mean_right)
                    for a, b in zip(left, right))
    denominator = math.sqrt(
        sum((a - mean_left) ** 2 for a in left)
        * sum((b - mean_right) ** 2 for b in right))
    return numerator / denominator if denominator > 0 else None


def _abstention_quality(records: Sequence[WindowRecord]
                        ) -> tuple[float | None, float | None]:
    """Precision and recall against "the move was in the top tercile".

    The positive class has to be defined by something, and the realised move is
    the only thing available that an arm did not choose. Precision: of the
    windows this arm declined, how many were genuinely hard. Recall: of the
    genuinely hard windows, how many did it decline.

    `None` for an arm that never declines — not 1.0 and not 0.0. An arm with no
    abstention has no abstention quality, and scoring it either way puts it in
    a ranking it is not in.
    """
    rows = [r for r in records if not r.failed]
    declined = [r for r in rows if r.abstained]
    if not declined or len(rows) < 3:
        return None, None
    moves = sorted(abs(r.realised) for r in rows)
    threshold = moves[int(len(moves) * 2 / 3)]
    hard = [r for r in rows if abs(r.realised) >= threshold]
    if not hard:
        return None, None
    true_positive = sum(1 for r in declined if abs(r.realised) >= threshold)
    return true_positive / len(declined), true_positive / len(hard)


def non_overlapping(records: Sequence[WindowRecord], *, horizon: int
                    ) -> tuple[list[WindowRecord], int]:
    """Sample every `horizon`-th window per instrument. `(kept, dropped)`.

    Windows produced with stride 1 overlap: consecutive ones share all but one
    input bar, and their horizons cover the same future returns. Treating them
    as independent trading periods counts the same move `horizon` times, which
    inflates the return, shrinks the apparent volatility and multiplies Sharpe
    by roughly the square root of the overlap.

    The first version of this module did exactly that and reported a Sharpe of
    +38 for an autoregression on a synthetic series. That number is what a
    methodological error looks like from the inside: nothing raised, and the
    arms were still ranked correctly relative to one another.
    """
    kept: list[WindowRecord] = []
    by_instrument: dict[str, list[WindowRecord]] = {}
    for record in records:
        by_instrument.setdefault(record.instrument, []).append(record)
    for rows in by_instrument.values():
        rows.sort(key=lambda r: r.ts)
        kept.extend(rows[::max(1, horizon)])
    kept.sort(key=lambda r: (r.instrument, r.ts))
    return kept, len(records) - len(kept)


def trading_metrics(records: Sequence[WindowRecord], *, horizon: int = 1,
                    bars_per_year: float = 24 * 365) -> TradingMetrics:
    """The eighteen, from the matched strategy's net returns.

    Computed over **non-overlapping** periods. `horizon` is what makes them
    non-overlapping and defaults to 1 only so the function is callable on a
    series that is already disjoint; every caller with stride-1 windows must
    pass the real horizon.
    """
    usable = [r for r in records if r.net_return is not None]
    rows, dropped = non_overlapping(usable, horizon=horizon)
    rows.sort(key=lambda r: r.ts)
    if not rows:
        return TradingMetrics(n_dropped_overlapping=dropped)
    returns = [r.net_return for r in rows]
    traded = [r for r in rows if r.position != 0]
    wins = [r.net_return for r in traded if r.net_return > 0]
    losses = [r.net_return for r in traded if r.net_return < 0]

    equity, peak, worst = 0.0, 0.0, 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    drawdown = abs(worst)

    spread = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    downside = [min(0.0, value) for value in returns]
    downside_spread = (math.sqrt(statistics.fmean([d * d for d in downside]))
                       if len(downside) > 1 else 0.0)
    # Each retained row covers `horizon` bars, so the year holds
    # `bars_per_year / horizon` of them. Annualising by the bar count instead
    # would multiply every return by the overlap factor.
    periods_per_year = bars_per_year / max(1, horizon)
    scale = math.sqrt(periods_per_year) if len(returns) > 1 else 0.0
    mean = statistics.fmean(returns)
    annualised = mean * periods_per_year

    turnover = sum(abs(rows[i].position - rows[i - 1].position)
                   for i in range(1, len(rows))) + abs(rows[0].position)
    costs = _opt(r.cost_paid for r in rows)
    tail = None
    if len(traded) >= 20:
        ordered = sorted(r.net_return for r in traded)
        tail = statistics.fmean(ordered[:max(1, len(ordered) // 20)])

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return TradingMetrics(
        total_return=equity,
        annualised_return=annualised,
        sharpe=(mean / spread * scale) if spread > 0 else None,
        sortino=(mean / downside_spread * scale) if downside_spread > 0 else None,
        max_drawdown=drawdown,
        calmar=(annualised / drawdown) if drawdown > 0 else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        turnover=turnover / len(rows),
        fees=sum(costs) if costs else None,
        slippage=None,          # separated from fees only when a book exists
        exposure=len(traded) / len(rows),
        trade_count=len(traded),
        n_periods=len(rows), n_dropped_overlapping=dropped,
        win_rate=(len(wins) / len(traded)) if traded else None,
        average_win=statistics.fmean(wins) if wins else None,
        average_loss=statistics.fmean(losses) if losses else None,
        tail_loss=tail)


# ===========================================================================
# the matched strategy
# ===========================================================================

@dataclass(frozen=True)
class StrategyRule:
    """One rule, applied identically to every arm.

    Identical by construction rather than by convention: `apply` takes the arm's
    prediction and nothing about the arm, so there is no place for an arm to be
    traded differently.
    """

    #: Predicted return must exceed this multiple of the round-trip cost.
    edge_multiple: float = 1.5
    round_trip_bps: float = 4.0
    #: An arm that declines takes no position. Not a short, and not a hold.
    abstain_is_flat: bool = True

    def describe(self) -> str:
        return (f"long when the predicted horizon return exceeds "
                f"{self.edge_multiple}x the {self.round_trip_bps}bp round-trip "
                f"cost, short when it is below the negative of that, flat "
                f"otherwise; an abstention is flat; one unit; held for the "
                f"horizon; costs charged on every position change")

    def apply(self, predicted: float | None) -> int:
        if predicted is None:
            return 0
        threshold = self.edge_multiple * self.round_trip_bps / 10_000.0
        if predicted > threshold:
            return 1
        if predicted < -threshold:
            return -1
        return 0

    def net(self, position: int, realised: float) -> tuple[float, float]:
        """`(net return, cost paid)` for one window."""
        if position == 0:
            return 0.0, 0.0
        cost = self.round_trip_bps / 10_000.0
        return position * realised - cost, cost

    def to_dict(self) -> dict:
        return {"edge_multiple": self.edge_multiple,
                "round_trip_bps": self.round_trip_bps,
                "abstain_is_flat": self.abstain_is_flat,
                "description": self.describe()}


# ===========================================================================
# results
# ===========================================================================

@dataclass(frozen=True)
class ArmResult:
    """One arm's complete measurement under one contract."""

    spec: ArmSpec
    contract_fingerprint: str
    records: tuple[WindowRecord, ...] = ()
    forecast: ForecastMetrics = field(default_factory=ForecastMetrics)
    trading: TradingMetrics = field(default_factory=TradingMetrics)
    fit_seconds: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "records", tuple(self.records))
        if self.spec.available and not self.records:
            raise ConfigurationError(
                "an arm marked available produced no records; mark it "
                "unavailable with a reason instead",
                arm=self.spec.name)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(r.key for r in self.records)

    def by_regime(self) -> dict[str, tuple[WindowRecord, ...]]:
        out: dict[str, list[WindowRecord]] = {}
        for record in self.records:
            out.setdefault(record.regime or "unknown", []).append(record)
        return {k: tuple(v) for k, v in sorted(out.items())}

    def by_instrument(self) -> dict[str, tuple[WindowRecord, ...]]:
        out: dict[str, list[WindowRecord]] = {}
        for record in self.records:
            out.setdefault(record.instrument, []).append(record)
        return {k: tuple(v) for k, v in sorted(out.items())}

    def by_period(self, n: int = 3) -> dict[str, tuple[WindowRecord, ...]]:
        """Contiguous thirds of the evaluation window, oldest first."""
        ordered = sorted(self.records, key=lambda r: r.ts)
        if not ordered:
            return {}
        size = max(1, len(ordered) // n)
        out = {}
        for index in range(n):
            start = index * size
            end = len(ordered) if index == n - 1 else (index + 1) * size
            if start < len(ordered):
                out[f"period_{index + 1}"] = tuple(ordered[start:end])
        return out

    def to_dict(self) -> dict:
        return {"spec": self.spec.to_dict(),
                "contract_fingerprint": self.contract_fingerprint,
                "n_records": len(self.records),
                "forecast": self.forecast.to_dict(),
                "trading": self.trading.to_dict(),
                "fit_seconds": self.fit_seconds}


# ===========================================================================
# statistics
# ===========================================================================

def holm_bonferroni(p_values: Sequence[float], *, alpha: float = 0.05
                    ) -> list[float]:
    """Step-down adjusted p-values, in the input order.

    Holm rather than Bonferroni because Bonferroni over several hundred tests
    would leave nothing significant and the whole comparison would report
    "indistinguishable" by arithmetic rather than by measurement. Holm controls
    the same family-wise error rate and is uniformly more powerful.
    """
    values = [float(p) for p in p_values]
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    adjusted = [0.0] * len(values)
    running = 0.0
    for rank, index in enumerate(order):
        candidate = min(1.0, (len(values) - rank) * values[index])
        running = max(running, candidate)
        adjusted[index] = running
    return adjusted


@dataclass(frozen=True)
class Comparison:
    """One arm against one reference on one metric, with both p-values."""

    arm: str
    reference: str
    metric: str
    scope: str
    n_paired: int
    gain: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    p_adjusted: float | None = None
    #: True when the arm saw more than the reference did. A significant gain
    #: with this set is a finding about data, not about architecture.
    input_advantage: str = ""

    @property
    def usable(self) -> bool:
        return self.n_paired >= MIN_PAIRED and self.p_value is not None

    @property
    def significant(self) -> bool:
        """Reads the **adjusted** p-value, and requires the interval to
        exclude zero. Both, because a p-value near the threshold with an
        interval straddling zero is what a small sample manufactures."""
        if not self.usable or self.p_adjusted is None:
            return False
        if self.ci_low is None or self.ci_high is None:
            return False
        return self.p_adjusted < 0.05 and (self.ci_low > 0 or self.ci_high < 0)

    @property
    def better(self) -> bool | None:
        if not self.significant or self.gain is None:
            return None
        return self.gain > 0

    def to_dict(self) -> dict:
        return {"arm": self.arm, "reference": self.reference,
                "metric": self.metric, "scope": self.scope,
                "n_paired": self.n_paired, "gain": self.gain,
                "ci_low": self.ci_low, "ci_high": self.ci_high,
                "p_value": self.p_value, "p_adjusted": self.p_adjusted,
                "usable": self.usable, "significant": self.significant,
                "better": self.better, "input_advantage": self.input_advantage}


def _paired(arm: ArmResult, reference: ArmResult
            ) -> list[tuple[WindowRecord, WindowRecord]]:
    index = {record.key: record for record in reference.records}
    return [(record, index[record.key]) for record in arm.records
            if record.key in index]


def compare_arms(arm: ArmResult, reference: ArmResult, *,
                 metrics: Sequence[str] = ("mae", "quantile_loss",
                                           "net_return"),
                 scope: str = "all", seed: int = 0,
                 allow_input_advantage: bool = False) -> list[Comparison]:
    """Paired bootstrap on the windows both arms answered.

    **Raises** when the two arms were measured under different contracts. A
    difference in cost model or split scheme is invisible in the resulting
    numbers and is the single most effective way to manufacture an improvement,
    so it is refused here rather than noted in a report nobody reads to the end.
    """
    if arm.contract_fingerprint != reference.contract_fingerprint:
        raise ConfigurationError(
            "these arms were not measured under the same contract; comparing "
            "them would compare the harnesses rather than the models",
            arm=arm.spec.name, reference=reference.spec.name,
            arm_contract=arm.contract_fingerprint,
            reference_contract=reference.contract_fingerprint)

    advantage = ""
    if arm.spec.input_class is not reference.spec.input_class:
        advantage = (f"{arm.spec.name} saw {arm.spec.input_class.value}; "
                     f"{reference.spec.name} saw "
                     f"{reference.spec.input_class.value}")
        if not allow_input_advantage:
            raise ConfigurationError(
                "these arms did not see the same inputs; a comparison across "
                "input classes measures data advantage rather than "
                "architecture. Pass allow_input_advantage=True to record it "
                "as such.", detail=advantage)

    pairs = _paired(arm, reference)
    out: list[Comparison] = []
    for metric in metrics:
        gains, count = _gains_for(metric, pairs)
        if len(gains) < 2:
            out.append(Comparison(
                arm=arm.spec.name, reference=reference.spec.name,
                metric=metric, scope=scope, n_paired=count, gain=None,
                ci_low=None, ci_high=None, p_value=None,
                input_advantage=advantage))
            continue
        test: SignificanceResult = paired_bootstrap(gains, name=metric, seed=seed)
        out.append(Comparison(
            arm=arm.spec.name, reference=reference.spec.name, metric=metric,
            scope=scope, n_paired=len(gains),
            gain=statistics.fmean(gains), ci_low=test.ci_low,
            ci_high=test.ci_high, p_value=test.p_value,
            input_advantage=advantage))
    return out


def _gains_for(metric: str, pairs: Sequence[tuple[WindowRecord, WindowRecord]]
               ) -> tuple[list[float], int]:
    """Per-window differences, signed so positive always means the arm is
    better. Only windows both arms answered contribute."""
    gains: list[float] = []
    for mine, theirs in pairs:
        if metric == "mae":
            if mine.absolute_error is None or theirs.absolute_error is None:
                continue
            gains.append(theirs.absolute_error - mine.absolute_error)
        elif metric == "quantile_loss":
            left = _pinball(mine.quantiles, mine.realised) if mine.answered else None
            right = (_pinball(theirs.quantiles, theirs.realised)
                     if theirs.answered else None)
            if left is None or right is None:
                continue
            gains.append(right - left)
        elif metric == "net_return":
            if mine.net_return is None or theirs.net_return is None:
                continue
            gains.append(mine.net_return - theirs.net_return)
        else:                                            # pragma: no cover
            raise ConfigurationError("unknown paired metric", metric=metric)
    return gains, len(pairs)


def adjust_family(comparisons: Sequence[Comparison]) -> list[Comparison]:
    """Apply Holm across the whole family and return comparisons carrying both
    p-values. The size of the family is itself reported, because a correction
    over four tests and one over four hundred are different claims."""
    usable = [c for c in comparisons if c.p_value is not None]
    adjusted = holm_bonferroni([c.p_value for c in usable])
    lookup = {id(c): value for c, value in zip(usable, adjusted)}
    return [Comparison(
        arm=c.arm, reference=c.reference, metric=c.metric, scope=c.scope,
        n_paired=c.n_paired, gain=c.gain, ci_low=c.ci_low, ci_high=c.ci_high,
        p_value=c.p_value, p_adjusted=lookup.get(id(c)),
        input_advantage=c.input_advantage) for c in comparisons]


# ===========================================================================
# the verdict
# ===========================================================================

@dataclass(frozen=True)
class MatchedReport:
    """Every arm, every comparison, and a verdict computed from both."""

    contract: FairnessContract
    arms: tuple[ArmResult, ...]
    comparisons: tuple[Comparison, ...] = ()
    #: Regime name -> the comparisons scoped to it.
    regime_comparisons: tuple[Comparison, ...] = ()
    dataset_hashes: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    schema_version: int = MATCHED_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "arms", tuple(self.arms))
        object.__setattr__(self, "comparisons", tuple(self.comparisons))
        object.__setattr__(self, "regime_comparisons",
                           tuple(self.regime_comparisons))
        object.__setattr__(self, "dataset_hashes", dict(self.dataset_hashes))
        object.__setattr__(self, "notes", tuple(self.notes))
        fingerprint = self.contract.fingerprint()
        wrong = [a.spec.name for a in self.arms
                 if a.spec.available and a.contract_fingerprint != fingerprint]
        if wrong:
            raise ConfigurationError(
                "an available arm carries a different contract fingerprint "
                "from the report it is in", arms=wrong)

    def arm(self, kind: ArmKind | str) -> ArmResult | None:
        wanted = ArmKind(kind)
        return next((a for a in self.arms if a.spec.kind is wanted), None)

    @property
    def available_arms(self) -> tuple[str, ...]:
        return tuple(a.spec.name for a in self.arms if a.spec.available)

    @property
    def missing_arms(self) -> Mapping[str, str]:
        """Required arms that did not run, and why. In every rendering."""
        present = {a.spec.kind for a in self.arms if a.spec.available}
        out = {}
        for kind in REQUIRED_ARMS:
            if kind in present:
                continue
            existing = self.arm(kind)
            out[kind.value] = (existing.spec.availability.reason if existing
                               else "not constructed by this run")
        return out

    @property
    def reference_ran(self) -> bool:
        arm = self.arm(ArmKind.EXTERNAL_REFERENCE)
        return bool(arm and arm.spec.available)

    @property
    def verdict(self) -> Verdict:
        """Computed. `INSUFFICIENT_EVIDENCE` when the reference did not run.

        There is no argument that changes this and no branch that reaches
        another verdict without a reference arm, because the question is about
        the reference. Five arms beating each other is a different finding,
        reported separately by `baseline_verdict`.
        """
        if not self.reference_ran:
            return Verdict.INSUFFICIENT_EVIDENCE
        olympus = self.arm(ArmKind.OLYMPUS_NATIVE)
        if olympus is None or not olympus.spec.available:
            return Verdict.INSUFFICIENT_EVIDENCE

        against = [c for c in self.comparisons
                   if c.arm == olympus.spec.name
                   and c.reference == self.arm(ArmKind.EXTERNAL_REFERENCE).spec.name]
        usable = [c for c in against if c.usable]
        if len(usable) < 2:
            return Verdict.INSUFFICIENT_EVIDENCE

        wins = [c for c in usable if c.better is True]
        losses = [c for c in usable if c.better is False]
        if losses and not wins:
            return Verdict.REFERENCE_SUPERIOR
        if not wins:
            regime_wins = [c for c in self.regime_comparisons
                           if c.arm == olympus.spec.name and c.better is True]
            if regime_wins:
                return Verdict.OLYMPUS_SUPERIOR_IN_REGIMES
            return Verdict.INDISTINGUISHABLE
        if wins and losses:
            return Verdict.OLYMPUS_COMPLEMENTARY
        ensemble = self.arm(ArmKind.ENSEMBLE)
        if ensemble is not None and ensemble.spec.available:
            better_together = [c for c in self.comparisons
                               if c.arm == ensemble.spec.name
                               and c.better is True]
            if better_together:
                return Verdict.OLYMPUS_COMPLEMENTARY
        return Verdict.OLYMPUS_CLEARLY_SUPERIOR

    @property
    def baseline_verdict(self) -> Mapping[str, Any]:
        """The question that *can* be answered here: does the native model beat
        the simple arms? Reported separately so it is never read as the answer
        to the comparison against the external reference."""
        olympus = self.arm(ArmKind.OLYMPUS_NATIVE)
        if olympus is None or not olympus.spec.available:
            return {"answerable": False,
                    "reason": "the Olympus arm did not run"}
        simple = [ArmKind.PERSISTENCE, ArmKind.STATISTICAL,
                  ArmKind.MACHINE_LEARNING]
        names = {self.arm(k).spec.name for k in simple
                 if self.arm(k) and self.arm(k).spec.available}
        relevant = [c for c in self.comparisons
                    if c.arm == olympus.spec.name and c.reference in names]
        usable = [c for c in relevant if c.usable]
        wins = [c.to_dict() for c in usable if c.better is True]
        losses = [c.to_dict() for c in usable if c.better is False]
        return {"answerable": bool(usable),
                "references": sorted(names),
                "n_comparisons": len(usable),
                "significant_wins": wins,
                "significant_losses": losses,
                "conclusion": (
                    "the native model does not beat the simple arms on any "
                    "metric at the corrected significance level"
                    if usable and not wins else
                    "the native model beats a simple arm on at least one metric"
                    if wins else
                    "no usable comparison against a simple arm")}

    @property
    def promotion(self) -> Promotion:
        """What follows. Also computed, and `NO_DECISION` without a verdict."""
        verdict = self.verdict
        if verdict is Verdict.INSUFFICIENT_EVIDENCE:
            return Promotion.NO_DECISION
        if verdict is Verdict.OLYMPUS_CLEARLY_SUPERIOR:
            return Promotion.OLYMPUS_REPLACES_REFERENCE
        if verdict is Verdict.OLYMPUS_SUPERIOR_IN_REGIMES:
            return Promotion.OLYMPUS_ROUTES_SELECTED_REGIMES
        if verdict is Verdict.OLYMPUS_COMPLEMENTARY:
            return Promotion.ENSEMBLE
        if verdict is Verdict.REFERENCE_SUPERIOR:
            return Promotion.REFERENCE_REMAINS_CHAMPION
        return Promotion.REFERENCE_REMAINS_CHAMPION

    @property
    def n_comparisons(self) -> int:
        return len(self.comparisons) + len(self.regime_comparisons)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "contract": self.contract.to_dict(),
                "contract_fingerprint": self.contract.fingerprint(),
                "dataset_hashes": dict(self.dataset_hashes),
                "arms": [a.to_dict() for a in self.arms],
                "available_arms": list(self.available_arms),
                "missing_arms": dict(self.missing_arms),
                "reference_ran": self.reference_ran,
                "comparisons": [c.to_dict() for c in self.comparisons],
                "regime_comparisons": [c.to_dict()
                                       for c in self.regime_comparisons],
                "n_comparisons": self.n_comparisons,
                "verdict": self.verdict.value,
                "baseline_verdict": dict(self.baseline_verdict),
                "promotion": self.promotion.value,
                "notes": list(self.notes)}

    def metric_table(self) -> str:                       # pragma: no cover
        header = (f"{'arm':<24}{'n':>6}{'ans':>6}{'MAE':>11}{'RMSE':>11}"
                  f"{'dir':>7}{'qloss':>11}{'cov':>7}{'calib':>8}{'net':>11}")
        lines = [header, "-" * len(header)]

        def cell(value, spec="{:.5f}"):
            return "-" if value is None else spec.format(value)

        for result in self.arms:
            if not result.spec.available:
                lines.append(f"{result.spec.name:<24}"
                             f"{'  UNAVAILABLE — ' + result.spec.availability.reason[:60]}")
                continue
            f, t = result.forecast, result.trading
            lines.append(
                f"{result.spec.name:<24}{f.n:>6}{f.n_answered:>6}"
                f"{cell(f.mae):>11}{cell(f.rmse):>11}"
                f"{cell(f.directional_accuracy, '{:.3f}'):>7}"
                f"{cell(f.quantile_loss):>11}"
                f"{cell(f.coverage, '{:.3f}'):>7}"
                f"{cell(f.calibration_error, '{:+.1f}'):>8}"
                f"{cell(t.total_return, '{:+.5f}'):>11}")
        return "\n".join(lines)


__all__ = ["MATCHED_SCHEMA_VERSION", "MIN_PAIRED", "ArmKind", "REQUIRED_ARMS",
           "InputClass", "INPUT_CLASS_NOTES", "Verdict", "Promotion",
           "FairnessContract", "timestamps_hash", "ArmAvailability", "ArmSpec",
           "unavailable", "ForecastMetrics", "FORECAST_DIRECTION",
           "TradingMetrics", "TRADING_DIRECTION", "WindowRecord",
           "forecast_metrics", "non_overlapping", "trading_metrics",
           "StrategyRule", "ArmResult",
           "holm_bonferroni", "Comparison", "compare_arms", "adjust_family",
           "MatchedReport"]
