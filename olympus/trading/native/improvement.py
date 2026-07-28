"""Whether Olympus is getting better, and what may not be used to claim it.

> Measure whether Olympus becomes stronger through: lower forecast error,
> better calibration, better abstention quality, greater robustness, reduced
> drawdown, improved risk-adjusted returns, lower execution costs, lower
> failure rates, better drift detection, faster rollback, better performance on
> unseen instruments and regimes, simpler models achieving equivalent
> performance.
>
> Do not measure progress by code volume, number of proposals or number of
> capabilities alone.

The second paragraph is the one with teeth, so it is enforced rather than
noted. `FORBIDDEN_PROGRESS_METRICS` names the quantities that must never enter
a verdict, and `measure()` **raises** when one appears in its input. Not
ignores — raises. A metric silently dropped is a metric somebody will keep
computing and quoting.

The three that are easy to fake are the three that are checked hardest:

**Abstention quality is not the abstention rate.** A model that declines
everything has a perfect error record and is worthless. `abstention_quality`
here is selectivity — how much larger the realised move was on declined windows
than on answered ones — so declining indiscriminately scores *worse*, not
better. This is the same quantity Phase 2's evaluation renamed from
`error_avoided` after it was found to be gameable.

**Parsimony is a metric, not a tie-break.** `simpler_at_equal_performance`
records whether the parameter count fell while the primary metric held. Without
it, every improvement is an addition, and a system that only ever adds is a
system whose complexity ratchets in one direction forever.

**Unseen instruments and unseen regimes are separate metrics.** The framework's
existing `unseen_regime_performance` covers half of what Phase 4 asks for.
Generalising to a new regime on a known instrument and generalising to a new
instrument are different abilities and a system can have one without the other.

The verdict starts at UNPROVEN
------------------------------
Not at NOT_IMPROVING, which would read as a finding, and not at IMPROVING,
which would be a claim. An unmeasured metric is `None` and is reported in
`unmeasured` — never zero, and never quietly excluded from the denominator.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from ..errors import ConfigurationError
from .evidence import (ForecastEvidence, MIN_SAMPLE, calibration_error,
                       mean_absolute_error)

#: Bumped when the metric set changes.
IMPROVEMENT_SCHEMA_VERSION = 1


class Direction(str, Enum):
    LOWER = "lower"
    HIGHER = "higher"


class NativeMetric(str, Enum):
    """The twelve Phase 4 names, one member each."""

    FORECAST_ERROR = "forecast_error"
    CALIBRATION_GAP = "calibration_gap"
    ABSTENTION_QUALITY = "abstention_quality"
    ROBUSTNESS_SCORE = "robustness_score"
    MAX_DRAWDOWN = "max_drawdown"
    RISK_ADJUSTED_RETURN = "risk_adjusted_return"
    EXECUTION_COST = "execution_cost"
    FAILURE_RATE = "failure_rate"
    DRIFT_DETECTION_SECONDS = "drift_detection_seconds"
    ROLLBACK_SECONDS = "rollback_seconds"
    UNSEEN_GENERALISATION = "unseen_generalisation"
    SIMPLER_AT_EQUAL_PERFORMANCE = "simpler_at_equal_performance"


#: Which way is better for each. Data, so the direction is reviewable rather
#: than buried in a comparison.
METRIC_DIRECTION: Mapping[NativeMetric, Direction] = {
    NativeMetric.FORECAST_ERROR: Direction.LOWER,
    NativeMetric.CALIBRATION_GAP: Direction.LOWER,
    NativeMetric.ABSTENTION_QUALITY: Direction.HIGHER,
    NativeMetric.ROBUSTNESS_SCORE: Direction.HIGHER,
    NativeMetric.MAX_DRAWDOWN: Direction.LOWER,
    NativeMetric.RISK_ADJUSTED_RETURN: Direction.HIGHER,
    NativeMetric.EXECUTION_COST: Direction.LOWER,
    NativeMetric.FAILURE_RATE: Direction.LOWER,
    NativeMetric.DRIFT_DETECTION_SECONDS: Direction.LOWER,
    NativeMetric.ROLLBACK_SECONDS: Direction.LOWER,
    NativeMetric.UNSEEN_GENERALISATION: Direction.HIGHER,
    NativeMetric.SIMPLER_AT_EQUAL_PERFORMANCE: Direction.HIGHER,
}

#: What each metric means, in the terms it is actually computed in. Written
#: out because several of these have a tempting wrong definition.
METRIC_NOTES: Mapping[NativeMetric, str] = {
    NativeMetric.FORECAST_ERROR:
        "mean absolute error over answered forecasts only; including declined "
        "rows as zero credits the model with accuracy on windows it refused",
    NativeMetric.CALIBRATION_GAP:
        "absolute realised-minus-nominal coverage, in points, over the "
        "forecasts that carried an interval",
    NativeMetric.ABSTENTION_QUALITY:
        "selectivity — the ratio of the median realised move on declined "
        "windows to the median on answered ones. NOT the abstention rate: a "
        "model that declines everything has no answered rows to compare "
        "against and scores None, not a high number",
    NativeMetric.ROBUSTNESS_SCORE:
        "fraction of the thirteen adversarial conditions the system degrades "
        "explicitly under, rather than the fraction it survives",
    NativeMetric.MAX_DRAWDOWN:
        "peak-to-trough on the equity the forecasts were traded into; `None` "
        "when nothing was traded, never zero",
    NativeMetric.RISK_ADJUSTED_RETURN:
        "mean net return over its own standard deviation, after costs",
    NativeMetric.EXECUTION_COST:
        "realised cost in basis points, including the overrun against what "
        "was modelled",
    NativeMetric.FAILURE_RATE:
        "fraction of forecast requests that raised, timed out or returned an "
        "unusable result — distinct from the abstention rate, which is the "
        "system working",
    NativeMetric.DRIFT_DETECTION_SECONDS:
        "time from a deterioration beginning to the monitor flagging it",
    NativeMetric.ROLLBACK_SECONDS:
        "time from the decision to roll back to the previously approved "
        "version serving again",
    NativeMetric.UNSEEN_GENERALISATION:
        "performance on instruments and regimes absent from training, "
        "reported as a ratio to in-sample performance; the two halves are "
        "separately available in `unseen_detail`",
    NativeMetric.SIMPLER_AT_EQUAL_PERFORMANCE:
        "1.0 when the parameter count fell and the primary metric did not "
        "regress; 0.0 when complexity grew without a matching gain",
}

#: Quantities that must never enter a verdict about whether Olympus is
#: improving. Naming them is the enforcement: `measure()` raises when one turns
#: up, rather than dropping it quietly and letting somebody keep quoting it.
FORBIDDEN_PROGRESS_METRICS: frozenset[str] = frozenset({
    "lines_of_code", "modules_added", "files_added", "code_volume",
    "proposals_generated", "proposals_submitted", "hypotheses_generated",
    "capabilities_added", "capabilities_declared", "experiments_run",
    "challengers_generated", "commits", "tests_added",
})


def assert_not_volume_metric(name: str) -> str:
    """Refuse a quantity that measures activity rather than improvement."""
    if str(name) in FORBIDDEN_PROGRESS_METRICS:
        raise ConfigurationError(
            "this measures how much Olympus did, not whether it got better; "
            "a system optimising it improves by generating more work",
            metric=name, forbidden=sorted(FORBIDDEN_PROGRESS_METRICS))
    return str(name)


class Verdict(str, Enum):
    IMPROVING = "improving"
    NOT_IMPROVING = "not_improving"
    #: The default and the honest state before there is enough evidence.
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class MetricChange:
    """One metric across two periods, with both values kept."""

    metric: NativeMetric
    before: float | None
    after: float | None
    n_before: int = 0
    n_after: int = 0
    detail: str = ""

    def __post_init__(self):
        object.__setattr__(self, "metric", NativeMetric(self.metric))

    @property
    def measured(self) -> bool:
        return self.before is not None and self.after is not None

    @property
    def usable(self) -> bool:
        """Measured *and* on enough observations in both periods."""
        return (self.measured
                and min(self.n_before, self.n_after) >= MIN_SAMPLE)

    @property
    def improved(self) -> bool | None:
        """`None` when unmeasured. Never False, which would read as a finding."""
        if not self.measured:
            return None
        if METRIC_DIRECTION[self.metric] is Direction.LOWER:
            return self.after < self.before
        return self.after > self.before

    @property
    def relative_change(self) -> float | None:
        if not self.measured or not self.before:
            return None
        return (self.after - self.before) / abs(self.before)

    def to_dict(self) -> dict:
        return {"metric": self.metric.value,
                "direction": METRIC_DIRECTION[self.metric].value,
                "before": self.before, "after": self.after,
                "n_before": self.n_before, "n_after": self.n_after,
                "measured": self.measured, "usable": self.usable,
                "improved": self.improved,
                "relative_change": self.relative_change,
                "detail": self.detail, "means": METRIC_NOTES[self.metric]}


@dataclass(frozen=True)
class ImprovementReport:
    """The verdict, and everything it rests on — including what was not seen."""

    changes: tuple[MetricChange, ...] = ()
    #: Anything the caller supplied that this module refused to score, with why.
    excluded: Mapping[str, str] = field(default_factory=dict)
    unseen_detail: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = IMPROVEMENT_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "excluded", dict(self.excluded))
        object.__setattr__(self, "unseen_detail", dict(self.unseen_detail))

    @property
    def measured(self) -> tuple[MetricChange, ...]:
        return tuple(c for c in self.changes if c.measured)

    @property
    def usable(self) -> tuple[MetricChange, ...]:
        return tuple(c for c in self.changes if c.usable)

    @property
    def unmeasured(self) -> tuple[str, ...]:
        measured = {c.metric for c in self.measured}
        return tuple(sorted(m.value for m in NativeMetric if m not in measured))

    @property
    def improved(self) -> tuple[str, ...]:
        return tuple(c.metric.value for c in self.measured if c.improved)

    @property
    def regressed(self) -> tuple[str, ...]:
        return tuple(c.metric.value for c in self.measured
                     if c.improved is False)

    @property
    def verdict(self) -> Verdict:
        """Computed, and conservative.

        `IMPROVING` needs at least three usable metrics, a majority of the
        measured ones improved, and **nothing regressed**. The last condition
        is the strict one and it is deliberate: a change that improves four
        things and breaks one is a change with a finding in it, and calling
        that "improving" is how the finding stops being discussed.
        """
        if len(self.usable) < 3:
            return Verdict.UNPROVEN
        if self.regressed:
            return Verdict.NOT_IMPROVING
        if len(self.improved) * 2 > len(self.measured):
            return Verdict.IMPROVING
        return Verdict.NOT_IMPROVING

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "verdict": self.verdict.value,
                "changes": [c.to_dict() for c in self.changes],
                "improved": list(self.improved),
                "regressed": list(self.regressed),
                "unmeasured": list(self.unmeasured),
                "n_usable": len(self.usable),
                "excluded": dict(self.excluded),
                "unseen_detail": dict(self.unseen_detail),
                "forbidden_metrics": sorted(FORBIDDEN_PROGRESS_METRICS)}

    def describe(self) -> str:                           # pragma: no cover
        lines = [f"verdict: {self.verdict.value}", ""]
        for change in self.changes:
            mark = ("--" if not change.measured
                    else "up" if change.improved else "DOWN")
            lines.append(f"  [{mark:>4}] {change.metric.value:<32}"
                         f"{change.before} -> {change.after}")
        if self.unmeasured:
            lines.append("")
            lines.append("unmeasured: " + ", ".join(self.unmeasured))
        if self.excluded:
            lines.append("excluded:   " + ", ".join(sorted(self.excluded)))
        return "\n".join(lines)


def _number(source: Mapping[str, Any], key: str) -> float | None:
    value = source.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None       # NaN is not a measurement


def measure(before: Mapping[str, Any], after: Mapping[str, Any], *,
            counts: Mapping[str, tuple[int, int]] | None = None,
            unseen_detail: Mapping[str, Any] | None = None
            ) -> ImprovementReport:
    """Score the twelve. Raises on a volume metric rather than ignoring one."""
    excluded: dict[str, str] = {}
    for source in (before, after):
        for key in source:
            if key in FORBIDDEN_PROGRESS_METRICS:
                assert_not_volume_metric(key)
    sizes = dict(counts or {})
    changes = []
    for metric in NativeMetric:
        n_before, n_after = sizes.get(metric.value, (0, 0))
        changes.append(MetricChange(
            metric=metric, before=_number(before, metric.value),
            after=_number(after, metric.value),
            n_before=n_before, n_after=n_after))
    for key in sorted(set(before) | set(after)):
        if key not in {m.value for m in NativeMetric}:
            excluded[key] = "not one of the twelve improvement metrics"
    return ImprovementReport(changes=tuple(changes), excluded=excluded,
                             unseen_detail=dict(unseen_detail or {}))


# ---------------------------------------------------------------------------
# computing the measurable subset from evidence
# ---------------------------------------------------------------------------

def abstention_quality(records: Sequence[ForecastEvidence]) -> float | None:
    """Selectivity: median declined move over median answered move.

    `None` when either side is empty — a model with no declines has no
    abstention quality, and reporting 1.0 would say it was averagely selective
    rather than that it never selected.

    A model that declines everything has no answered rows and scores `None`,
    not a high number. That asymmetry is the whole point of the metric.
    """
    declined = [abs(r.realised_return) for r in records if r.abstained]
    answered = [abs(r.realised_return) for r in records if r.answered]
    if not declined or not answered:
        return None
    reference = statistics.median(answered)
    if reference <= 0:
        return None
    return statistics.median(declined) / reference


def execution_cost(records: Sequence[ForecastEvidence]) -> float | None:
    costs = [r.realised_cost_bps for r in records
             if r.realised_cost_bps is not None]
    return statistics.fmean(costs) if costs else None


def risk_adjusted_return(records: Sequence[ForecastEvidence]) -> float | None:
    """Mean over standard deviation of realised trade return, after costs.

    Over traded rows only. A forecast nobody acted on has no return to be
    adjusted, and counting it as zero would flatter a model that was never
    trusted enough to trade.
    """
    returns = [r.trade_return for r in records
               if r.strategy_used and r.trade_return is not None]
    if len(returns) < 2:
        return None
    spread = statistics.pstdev(returns)
    return (statistics.fmean(returns) / spread) if spread > 0 else None


def max_drawdown(records: Sequence[ForecastEvidence]) -> float | None:
    """Peak-to-trough of cumulative traded return. `None` when nothing traded."""
    returns = [r.trade_return for r in sorted(records, key=lambda x: x.matured_at)
               if r.strategy_used and r.trade_return is not None]
    if not returns:
        return None
    equity, peak, worst = 0.0, 0.0, 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def unseen_generalisation(records: Sequence[ForecastEvidence], *,
                          trained_instruments: Sequence[str],
                          trained_regimes: Sequence[str]
                          ) -> tuple[float | None, dict]:
    """In-sample MAE over out-of-sample MAE, with the two halves reported.

    Above 1.0 means the model does *better* out of sample, which usually means
    the out-of-sample set was easier rather than that the model generalises —
    so the halves are returned alongside and neither is presented alone.
    """
    known_instruments = set(trained_instruments)
    known_regimes = set(trained_regimes)
    seen = [r for r in records
            if r.instrument_key in known_instruments and r.regime in known_regimes]
    unseen_instrument = [r for r in records
                         if r.instrument_key not in known_instruments]
    unseen_regime = [r for r in records
                     if r.instrument_key in known_instruments
                     and r.regime not in known_regimes]
    unseen = unseen_instrument + unseen_regime

    seen_mae = mean_absolute_error(seen)
    unseen_mae = mean_absolute_error(unseen)
    detail = {
        "seen_mae": seen_mae, "n_seen": len(seen),
        "unseen_instrument_mae": mean_absolute_error(unseen_instrument),
        "n_unseen_instrument": len(unseen_instrument),
        "unseen_regime_mae": mean_absolute_error(unseen_regime),
        "n_unseen_regime": len(unseen_regime),
        "note": "a ratio above 1.0 more often means the unseen set was easier "
                "than that the model generalises; read the halves",
    }
    if not seen_mae or not unseen_mae or unseen_mae <= 0:
        return None, detail
    return seen_mae / unseen_mae, detail


def from_evidence(before: Sequence[ForecastEvidence],
                  after: Sequence[ForecastEvidence], *,
                  trained_instruments: Sequence[str] = (),
                  trained_regimes: Sequence[str] = (),
                  extra_before: Mapping[str, Any] | None = None,
                  extra_after: Mapping[str, Any] | None = None
                  ) -> ImprovementReport:
    """Compute what the evidence journal can supply, and only that.

    Four of the twelve — robustness, failure rate, drift-detection time and
    rollback time — are not properties of a forecast and are not invented here.
    They arrive through `extra_before`/`extra_after` from the robustness suite,
    the serving layer, `drift.DeteriorationMonitor` and
    `rollback.DeploymentLedger` respectively, and are reported as *unmeasured*
    when they are absent.
    """
    def snapshot(records, extra):
        ratio, detail = unseen_generalisation(
            records, trained_instruments=trained_instruments,
            trained_regimes=trained_regimes)
        gap = calibration_error(records)
        values = {
            NativeMetric.FORECAST_ERROR.value: mean_absolute_error(records),
            NativeMetric.CALIBRATION_GAP.value: abs(gap) if gap is not None else None,
            NativeMetric.ABSTENTION_QUALITY.value: abstention_quality(records),
            NativeMetric.EXECUTION_COST.value: execution_cost(records),
            NativeMetric.RISK_ADJUSTED_RETURN.value: risk_adjusted_return(records),
            NativeMetric.MAX_DRAWDOWN.value: max_drawdown(records),
            NativeMetric.UNSEEN_GENERALISATION.value: ratio,
        }
        for key, value in dict(extra or {}).items():
            assert_not_volume_metric(key)
            values[key] = value
        return {k: v for k, v in values.items() if v is not None}, detail

    before_values, _ = snapshot(before, extra_before)
    after_values, detail = snapshot(after, extra_after)
    counts = {m.value: (len(before), len(after)) for m in NativeMetric}
    return measure(before_values, after_values, counts=counts,
                   unseen_detail=detail)


def to_framework_metrics(report: ImprovementReport) -> dict:
    """Translate into `evolution.IMPROVEMENT_METRICS` names.

    So a native result can be fed to `evolution.measure_improvement` and land
    in the same cycle record as everything else. Metrics with no counterpart
    are dropped from the translation and listed under `native_only` rather
    than being silently lost.
    """
    mapping = {
        NativeMetric.FORECAST_ERROR: "forecast_error",
        NativeMetric.CALIBRATION_GAP: "calibration_gap",
        NativeMetric.ABSTENTION_QUALITY: "abstention_quality",
        NativeMetric.MAX_DRAWDOWN: "max_drawdown",
        NativeMetric.RISK_ADJUSTED_RETURN: "risk_adjusted_return",
        NativeMetric.EXECUTION_COST: "slippage",
        NativeMetric.FAILURE_RATE: "operational_failures",
        NativeMetric.DRIFT_DETECTION_SECONDS: "incident_detection_seconds",
        NativeMetric.ROLLBACK_SECONDS: "recovery_seconds",
        NativeMetric.UNSEEN_GENERALISATION: "unseen_regime_performance",
    }
    before, after = {}, {}
    for change in report.measured:
        name = mapping.get(change.metric)
        if name is None:
            continue
        before[name], after[name] = change.before, change.after
    return {"before": before, "after": after,
            "native_only": sorted(m.value for m in NativeMetric
                                  if m not in mapping)}


__all__ = ["IMPROVEMENT_SCHEMA_VERSION", "Direction", "NativeMetric",
           "METRIC_DIRECTION", "METRIC_NOTES", "FORBIDDEN_PROGRESS_METRICS",
           "assert_not_volume_metric", "Verdict", "MetricChange",
           "ImprovementReport", "measure", "abstention_quality",
           "execution_cost", "risk_adjusted_return", "max_drawdown",
           "unseen_generalisation", "from_evidence", "to_framework_metrics"]
