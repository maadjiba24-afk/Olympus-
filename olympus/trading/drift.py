"""Continuous evaluation: catching a component that used to work.

The dangerous state for a trading system is not a model that is obviously
broken — that gets noticed. It is a model that was genuinely good, is quietly
no longer good, and keeps trading because it has a reputation. This module
exists to make "it performed well historically" carry no weight at all: every
component's licence to operate expires, and continuing to operate requires
current evidence rather than a record.

Two mechanisms.

**Drift detection** compares a reference distribution with a recent one across
four kinds — data, feature, model output, and calibration — using statistics
computed in pure stdlib (population stability index and a two-sample
Kolmogorov–Smirnov statistic). Neither is clever. Both are auditable, which
matters more here than power: an operator has to be able to check the number.

**The deterioration ladder** turns evidence into a state:

    HEALTHY → FLAGGED → RESTRICTED → SHADOW → DISABLED

Descending the ladder is autonomous. Olympus may demote a deteriorating
analytical component on its own judgement, without asking, because the failure
mode of waiting for a human is worse than the failure mode of an unnecessary
demotion. Ascending it is not autonomous under any circumstances:
`reinstate()` requires an operator actor and refuses an autonomous one. That
asymmetry is the whole safety argument of this module, and
`test_trading_drift.py` asserts it directly.

One deliberate strictness: `staleness` alone can flag a component. A model
whose evaluation is six weeks old is flagged even if every metric looks
excellent, because a metric computed six weeks ago is a claim about six weeks
ago.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Regime, ensure_utc, jsonable
from .errors import ConfigurationError, GovernanceViolation
from .governance import Action, Actor, authorise
from .storekeys import KeyedStore

_NS = "trading.drift"

#: Population-stability-index bands. These are the conventional thresholds and
#: are stated as constants so a report can cite the number it used rather than
#: asserting "significant drift" from a hidden cut-off.
PSI_MINOR = 0.10
PSI_MAJOR = 0.25

#: Below this many observations on either side, no drift verdict is issued.
#: Distribution comparisons on small samples find drift reliably and wrongly.
MIN_DRIFT_SAMPLE = 30

#: How long a component's evaluation stays valid before staleness alone flags it.
DEFAULT_EVALUATION_TTL = timedelta(days=14)


class DriftKind(str, Enum):
    #: The inputs changed — a new venue, a different tick size, a regime shift
    #: that moves the price distribution.
    DATA = "data"
    #: A derived feature changed distribution even though inputs did not.
    #: Usually a bug in the feature, which is why it is separated from DATA.
    FEATURE = "feature"
    #: The model's outputs changed distribution. May be correct (the market
    #: changed) or not (the checkpoint changed underneath).
    MODEL = "model"
    #: Confidence stopped meaning what it meant. The most consequential kind:
    #: an uncalibrated confidence silently rescales every position.
    CALIBRATION = "calibration"
    #: Operational, not statistical.
    LATENCY = "latency"
    SLIPPAGE = "slippage"
    FAILURE_RATE = "failure_rate"
    LIQUIDITY = "liquidity"
    CORRELATION = "correlation"
    MICROSTRUCTURE = "microstructure"


class Severity(str, Enum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    #: Past a hard threshold. The only severity that can force DISABLED.
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# statistics — deliberately simple and checkable
# ---------------------------------------------------------------------------


def _finite(values: Iterable[Any]) -> list[float]:
    out = []
    for value in values:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def quantile_edges(values: Sequence[float], bins: int = 10) -> list[float]:
    """Bin edges from the reference sample's own quantiles.

    Quantile bins rather than equal-width: equal-width bins on a heavy-tailed
    price series put almost every observation in one bucket, and a PSI computed
    over that is a number about nothing.
    """
    ordered = sorted(_finite(values))
    if len(ordered) < 2:
        raise ConfigurationError("need at least two values to form bins")
    bins = max(2, int(bins))
    edges = [ordered[0]]
    for i in range(1, bins):
        idx = min(len(ordered) - 1, int(round(i * (len(ordered) - 1) / bins)))
        edges.append(ordered[idx])
    edges.append(ordered[-1])
    # Collapse duplicate edges (a spiky distribution) so bin widths stay valid.
    deduped = [edges[0]]
    for edge in edges[1:]:
        if edge > deduped[-1]:
            deduped.append(edge)
    if len(deduped) < 2:
        raise ConfigurationError("reference sample is constant; PSI undefined")
    return deduped


def _histogram(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    counts = [0] * (len(edges) - 1)
    for value in values:
        # Values outside the reference range fall into the end bins: that is a
        # drift signal in itself and must not be discarded.
        if value <= edges[0]:
            counts[0] += 1
            continue
        if value >= edges[-1]:
            counts[-1] += 1
            continue
        for i in range(len(edges) - 1):
            if edges[i] < value <= edges[i + 1]:
                counts[i] += 1
                break
    total = sum(counts) or 1
    return [c / total for c in counts]


def population_stability_index(reference: Sequence[Any], current: Sequence[Any],
                               *, bins: int = 10, floor: float = 1e-4) -> float | None:
    """PSI between two samples. `None` when either is too small to mean anything.

    `floor` replaces empty bins so the logarithm stays finite. Without it a
    single empty bin produces infinite drift, which is technically true and
    operationally useless.
    """
    ref = _finite(reference)
    cur = _finite(current)
    if len(ref) < MIN_DRIFT_SAMPLE or len(cur) < MIN_DRIFT_SAMPLE:
        return None
    try:
        edges = quantile_edges(ref, bins)
    except ConfigurationError:
        return None
    p = _histogram(ref, edges)
    q = _histogram(cur, edges)
    total = 0.0
    for pi, qi in zip(p, q):
        pi = max(pi, floor)
        qi = max(qi, floor)
        total += (qi - pi) * math.log(qi / pi)
    return total


def ks_statistic(reference: Sequence[Any], current: Sequence[Any]) -> float | None:
    """Two-sample Kolmogorov–Smirnov statistic: the largest CDF gap."""
    ref = sorted(_finite(reference))
    cur = sorted(_finite(current))
    if len(ref) < MIN_DRIFT_SAMPLE or len(cur) < MIN_DRIFT_SAMPLE:
        return None
    i = j = 0
    largest = 0.0
    while i < len(ref) and j < len(cur):
        if ref[i] <= cur[j]:
            i += 1
        else:
            j += 1
        largest = max(largest, abs(i / len(ref) - j / len(cur)))
    return largest


def ks_critical(n_ref: int, n_cur: int, alpha: float = 0.05) -> float:
    """Asymptotic critical value. Cited alongside the statistic, never hidden."""
    coefficient = {0.10: 1.22, 0.05: 1.36, 0.01: 1.63}.get(round(alpha, 2), 1.36)
    return coefficient * math.sqrt((n_ref + n_cur) / (n_ref * n_cur))


def mean_shift(reference: Sequence[Any], current: Sequence[Any]) -> float | None:
    """Shift in means, in reference standard deviations."""
    ref = _finite(reference)
    cur = _finite(current)
    if len(ref) < 2 or not cur:
        return None
    spread = statistics.pstdev(ref)
    if spread <= 0:
        return None
    return (statistics.fmean(cur) - statistics.fmean(ref)) / spread


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftSignal:
    """One measured comparison. Carries its own numbers so it can be checked."""

    kind: DriftKind
    subject: str
    metric: str
    severity: Severity
    statistic: float | None = None
    critical: float | None = None
    reference_n: int = 0
    current_n: int = 0
    reference_value: float | None = None
    current_value: float | None = None
    detail: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", DriftKind(self.kind))
        object.__setattr__(self, "severity", Severity(self.severity))

    @property
    def drifted(self) -> bool:
        return self.severity in (Severity.MAJOR, Severity.CRITICAL)

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "subject": self.subject,
                "metric": self.metric, "severity": self.severity.value,
                "statistic": self.statistic, "critical": self.critical,
                "reference_n": self.reference_n, "current_n": self.current_n,
                "reference_value": self.reference_value,
                "current_value": self.current_value, "detail": self.detail}

    def __str__(self) -> str:                            # pragma: no cover
        return (f"{self.severity.value.upper()} {self.kind.value} drift in "
                f"{self.subject}.{self.metric}: {self.detail}")


def _severity_from_psi(psi: float) -> Severity:
    if psi < PSI_MINOR:
        return Severity.NONE
    if psi < PSI_MAJOR:
        return Severity.MINOR
    return Severity.MAJOR


def detect_distribution_drift(reference: Sequence[Any], current: Sequence[Any],
                              *, kind: DriftKind, subject: str, metric: str,
                              bins: int = 10) -> DriftSignal:
    """Compare two samples with PSI and KS. Both reported, neither alone trusted.

    Where they disagree the stricter wins, and the detail says so — two
    statistics disagreeing is information for the reader, not something to
    average away.
    """
    ref = _finite(reference)
    cur = _finite(current)
    psi = population_stability_index(ref, cur, bins=bins)
    ks = ks_statistic(ref, cur)
    if psi is None or ks is None:
        return DriftSignal(
            kind=kind, subject=subject, metric=metric, severity=Severity.NONE,
            reference_n=len(ref), current_n=len(cur),
            detail=f"insufficient sample (need {MIN_DRIFT_SAMPLE} on each side, "
                   f"have {len(ref)} and {len(cur)}); no verdict issued")
    critical = ks_critical(len(ref), len(cur))
    severity = _severity_from_psi(psi)
    ks_says_drift = ks > critical
    if ks_says_drift and severity is Severity.NONE:
        severity = Severity.MINOR
    detail = (f"PSI {psi:.3f} (minor≥{PSI_MINOR}, major≥{PSI_MAJOR}); "
              f"KS {ks:.3f} vs critical {critical:.3f} at α=0.05")
    if ks_says_drift != (psi >= PSI_MINOR):
        detail += "; the two statistics disagree — treat as provisional"
    return DriftSignal(
        kind=kind, subject=subject, metric=metric, severity=severity,
        statistic=psi, critical=PSI_MAJOR, reference_n=len(ref),
        current_n=len(cur),
        reference_value=statistics.fmean(ref) if ref else None,
        current_value=statistics.fmean(cur) if cur else None, detail=detail)


def detect_metric_regression(reference: Sequence[Any], current: Sequence[Any],
                             *, kind: DriftKind, subject: str, metric: str,
                             lower_is_better: bool = True,
                             minor: float = 0.15, major: float = 0.35,
                             critical: float = 0.75) -> DriftSignal:
    """Detect a metric getting worse — latency, slippage, failure rate, error.

    Distinct from distribution drift: here the *direction* matters. A latency
    distribution that shifted downward is not a problem, and a detector that
    flagged it would train operators to ignore the detector.
    """
    ref = _finite(reference)
    cur = _finite(current)
    if len(ref) < 2 or len(cur) < 2:
        return DriftSignal(kind=kind, subject=subject, metric=metric,
                           severity=Severity.NONE, reference_n=len(ref),
                           current_n=len(cur),
                           detail="insufficient sample; no verdict issued")
    ref_mean = statistics.fmean(ref)
    cur_mean = statistics.fmean(cur)
    if ref_mean == 0:
        change = 0.0 if cur_mean == 0 else math.inf
    else:
        change = (cur_mean - ref_mean) / abs(ref_mean)
    worse = change if lower_is_better else -change
    if worse >= critical:
        severity = Severity.CRITICAL
    elif worse >= major:
        severity = Severity.MAJOR
    elif worse >= minor:
        severity = Severity.MINOR
    else:
        severity = Severity.NONE
    direction = "worse" if worse > 0 else "better"
    return DriftSignal(
        kind=kind, subject=subject, metric=metric, severity=severity,
        statistic=worse if math.isfinite(worse) else None, critical=major,
        reference_n=len(ref), current_n=len(cur), reference_value=ref_mean,
        current_value=cur_mean,
        detail=f"{metric} moved {abs(change):.1%} {direction} "
               f"({ref_mean:.6g} → {cur_mean:.6g})")


def detect_calibration_drift(reference_confidence: Sequence[Any],
                             reference_correct: Sequence[Any],
                             current_confidence: Sequence[Any],
                             current_correct: Sequence[Any], *,
                             subject: str) -> DriftSignal:
    """Has stated confidence stopped predicting correctness?

    Measured as the change in |mean confidence − hit rate|. A model that is 80%
    confident and right 80% of the time is calibrated; one that is 80% confident
    and right 50% of the time is not, and the *change* in that gap is what tells
    you whether it used to be.
    """
    def gap(confidence, correct) -> float | None:
        conf = _finite(confidence)
        hits = [1.0 if bool(c) else 0.0 for c in correct]
        if len(conf) < MIN_DRIFT_SAMPLE or len(hits) < MIN_DRIFT_SAMPLE:
            return None
        n = min(len(conf), len(hits))
        return abs(statistics.fmean(conf[:n]) - statistics.fmean(hits[:n]))

    before = gap(reference_confidence, reference_correct)
    after = gap(current_confidence, current_correct)
    if before is None or after is None:
        return DriftSignal(kind=DriftKind.CALIBRATION, subject=subject,
                           metric="confidence_gap", severity=Severity.NONE,
                           detail="insufficient sample; no verdict issued")
    delta = after - before
    if after >= 0.30:
        severity = Severity.CRITICAL
    elif delta >= 0.15:
        severity = Severity.MAJOR
    elif delta >= 0.05:
        severity = Severity.MINOR
    else:
        severity = Severity.NONE
    return DriftSignal(
        kind=DriftKind.CALIBRATION, subject=subject, metric="confidence_gap",
        severity=severity, statistic=delta, critical=0.15,
        reference_value=before, current_value=after,
        reference_n=len(_finite(reference_confidence)),
        current_n=len(_finite(current_confidence)),
        detail=f"confidence-to-accuracy gap {before:.3f} → {after:.3f}; a gap "
               "at or above 0.30 means stated confidence no longer carries "
               "information")


def detect_correlation_change(reference_pairs: Sequence[tuple[Any, Any]],
                              current_pairs: Sequence[tuple[Any, Any]], *,
                              subject: str) -> DriftSignal:
    """Correlation regime change between two series.

    Matters because every concentration and correlated-exposure limit in
    `risk.py` assumes a correlation structure. When correlations converge in a
    stress event, a portfolio that looked diversified stops being diversified,
    and the limits that were sized for it are the wrong size.
    """
    def correlation(pairs) -> float | None:
        xs = _finite(p[0] for p in pairs)
        ys = _finite(p[1] for p in pairs)
        n = min(len(xs), len(ys))
        if n < MIN_DRIFT_SAMPLE:
            return None
        try:
            return statistics.correlation(xs[:n], ys[:n])
        except statistics.StatisticsError:
            return None

    before = correlation(reference_pairs)
    after = correlation(current_pairs)
    if before is None or after is None:
        return DriftSignal(kind=DriftKind.CORRELATION, subject=subject,
                           metric="pearson_r", severity=Severity.NONE,
                           detail="insufficient sample; no verdict issued")
    delta = abs(after - before)
    if delta >= 0.5:
        severity = Severity.MAJOR
    elif delta >= 0.25:
        severity = Severity.MINOR
    else:
        severity = Severity.NONE
    # Convergence toward 1 is the dangerous direction: it removes the
    # diversification a limit was sized against.
    if after >= 0.85 and before < 0.85:
        severity = Severity.MAJOR
    return DriftSignal(
        kind=DriftKind.CORRELATION, subject=subject, metric="pearson_r",
        severity=severity, statistic=delta, critical=0.5,
        reference_value=before, current_value=after,
        detail=f"correlation {before:+.2f} → {after:+.2f}"
               + ("; converged toward 1, so exposures that were treated as "
                  "diversified no longer are" if after >= 0.85 > before else ""))


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------


class ComponentState(str, Enum):
    """Descending is autonomous; ascending is not."""

    HEALTHY = "healthy"
    #: Still operating; someone should look.
    FLAGGED = "flagged"
    #: Operating with reduced size / narrower instrument set.
    RESTRICTED = "restricted"
    #: Producing output that is recorded and compared but reaches no venue.
    SHADOW = "shadow"
    #: Not running.
    DISABLED = "disabled"


_LADDER = (ComponentState.HEALTHY, ComponentState.FLAGGED,
           ComponentState.RESTRICTED, ComponentState.SHADOW,
           ComponentState.DISABLED)
_RANK = {state: i for i, state in enumerate(_LADDER)}


def is_demotion(before: ComponentState, after: ComponentState) -> bool:
    return _RANK[after] > _RANK[before]


@dataclass(frozen=True)
class DeteriorationThresholds:
    """When a signal forces which rung. Frozen and hashable, like `RiskLimits`.

    Named thresholds rather than inline magic numbers because an operator has
    to be able to answer "why was this disabled" with a citation.
    """

    #: Any MAJOR signal flags. Two flag *and* restrict.
    major_signals_to_restrict: int = 2
    #: Any CRITICAL signal moves straight to shadow.
    critical_signals_to_shadow: int = 1
    #: Two criticals, or a critical while already in shadow, disables.
    critical_signals_to_disable: int = 2
    #: Hard performance floors. Crossing one disables regardless of anything else.
    min_directional_accuracy: float | None = 0.40
    max_drawdown: float | None = 0.25
    max_failure_rate: float | None = 0.20
    #: A component whose evaluation is older than this is flagged on age alone.
    evaluation_ttl: timedelta = DEFAULT_EVALUATION_TTL

    def to_dict(self) -> dict:
        return {"major_signals_to_restrict": self.major_signals_to_restrict,
                "critical_signals_to_shadow": self.critical_signals_to_shadow,
                "critical_signals_to_disable": self.critical_signals_to_disable,
                "min_directional_accuracy": self.min_directional_accuracy,
                "max_drawdown": self.max_drawdown,
                "max_failure_rate": self.max_failure_rate,
                "evaluation_ttl_s": self.evaluation_ttl.total_seconds()}


@dataclass(frozen=True)
class ComponentHealth:
    """A component's current rung, and the evidence that put it there."""

    component_id: str
    kind: str                        # "model" | "strategy" | "feature" | …
    state: ComponentState
    since: datetime
    reasons: tuple[str, ...] = ()
    signals: tuple[DriftSignal, ...] = ()
    last_evaluated_at: datetime | None = None
    changed_by: str = "system"

    def __post_init__(self):
        object.__setattr__(self, "state", ComponentState(self.state))
        object.__setattr__(self, "since", ensure_utc(self.since, field_name="since"))
        if self.last_evaluated_at is not None:
            object.__setattr__(self, "last_evaluated_at",
                               ensure_utc(self.last_evaluated_at,
                                          field_name="last_evaluated_at"))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "signals", tuple(self.signals))

    @property
    def operating(self) -> bool:
        """May this component's output reach a trading decision?"""
        return self.state in (ComponentState.HEALTHY, ComponentState.FLAGGED,
                              ComponentState.RESTRICTED)

    def to_dict(self) -> dict:
        return {"component_id": self.component_id, "kind": self.kind,
                "state": self.state.value, "since": jsonable(self.since),
                "reasons": list(self.reasons),
                "signals": [s.to_dict() for s in self.signals],
                "last_evaluated_at": jsonable(self.last_evaluated_at),
                "changed_by": self.changed_by, "operating": self.operating}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ComponentHealth":
        when = data.get("last_evaluated_at")
        return cls(component_id=str(data.get("component_id", "")),
                   kind=str(data.get("kind", "")),
                   state=ComponentState(data.get("state", ComponentState.HEALTHY.value)),
                   since=datetime.fromisoformat(data["since"]),
                   reasons=tuple(data.get("reasons") or ()),
                   signals=tuple(DriftSignal(**{**s, "kind": DriftKind(s["kind"]),
                                                "severity": Severity(s["severity"])})
                                 for s in data.get("signals") or ()),
                   last_evaluated_at=(datetime.fromisoformat(when)
                                      if isinstance(when, str) and when else None),
                   changed_by=str(data.get("changed_by", "system")))


@dataclass(frozen=True)
class PerformanceSnapshot:
    """Current measured performance. Every field optional, none defaulted.

    `None` means unmeasured, and an unmeasured hard threshold is *not* treated
    as passed — `assess` reports it as unverified so a component cannot stay
    healthy by never being measured.
    """

    directional_accuracy: float | None = None
    max_drawdown: float | None = None
    failure_rate: float | None = None
    net_return: float | None = None
    sample: int = 0

    def to_dict(self) -> dict:
        return {"directional_accuracy": self.directional_accuracy,
                "max_drawdown": self.max_drawdown,
                "failure_rate": self.failure_rate,
                "net_return": self.net_return, "sample": self.sample}


def assess(component_id: str, *, kind: str, signals: Sequence[DriftSignal],
           performance: PerformanceSnapshot | None = None,
           current: ComponentState = ComponentState.HEALTHY,
           last_evaluated_at: datetime | None = None,
           thresholds: DeteriorationThresholds | None = None,
           now: datetime | None = None,
           clock: Clock | None = None) -> ComponentHealth:
    """Decide which rung a component belongs on. Pure, deterministic, no I/O.

    Never *raises* the component: the returned state is the worse of the
    current state and what the evidence justifies. Recovery is an operator act,
    handled by `DeteriorationMonitor.reinstate`.
    """
    thresholds = thresholds or DeteriorationThresholds()
    now = ensure_utc(now or (clock or default_clock()).now(), field_name="now")
    reasons: list[str] = []
    proposed = ComponentState.HEALTHY

    majors = [s for s in signals if s.severity is Severity.MAJOR]
    criticals = [s for s in signals if s.severity is Severity.CRITICAL]

    if majors:
        proposed = ComponentState.FLAGGED
        reasons.append(f"{len(majors)} major drift signal(s): "
                       + ", ".join(sorted(f"{s.kind.value}/{s.metric}" for s in majors)))
    if len(majors) >= thresholds.major_signals_to_restrict:
        proposed = ComponentState.RESTRICTED
    if len(criticals) >= thresholds.critical_signals_to_shadow:
        proposed = ComponentState.SHADOW
        reasons.append(f"{len(criticals)} critical drift signal(s): "
                       + ", ".join(sorted(f"{s.kind.value}/{s.metric}" for s in criticals)))
    if len(criticals) >= thresholds.critical_signals_to_disable:
        proposed = ComponentState.DISABLED

    # Hard floors. These outrank everything above, including a component that
    # looks fine on drift: a strategy in a 30% drawdown is not healthy because
    # its feature distributions are stable.
    if performance is not None:
        checks = (
            ("directional_accuracy", performance.directional_accuracy,
             thresholds.min_directional_accuracy, "below"),
            ("max_drawdown", performance.max_drawdown, thresholds.max_drawdown, "above"),
            ("failure_rate", performance.failure_rate, thresholds.max_failure_rate, "above"),
        )
        for name, value, limit, direction in checks:
            if limit is None:
                continue
            if value is None:
                reasons.append(f"{name} unmeasured; hard threshold unverified")
                if proposed is ComponentState.HEALTHY:
                    proposed = ComponentState.FLAGGED
                continue
            breached = value < limit if direction == "below" else value > limit
            if breached:
                proposed = ComponentState.DISABLED
                reasons.append(
                    f"hard threshold crossed: {name} {value:.4g} is {direction} "
                    f"{limit:.4g}")

    # Age alone is enough to flag. "It performed well historically" is not a
    # licence to keep operating.
    if last_evaluated_at is not None:
        age = now - ensure_utc(last_evaluated_at, field_name="last_evaluated_at")
        if age > thresholds.evaluation_ttl:
            if _RANK[proposed] < _RANK[ComponentState.FLAGGED]:
                proposed = ComponentState.FLAGGED
            reasons.append(
                f"last evaluated {age.days}d ago, past the "
                f"{thresholds.evaluation_ttl.days}d validity window; a stale "
                "metric is a claim about the past")
    else:
        if _RANK[proposed] < _RANK[ComponentState.FLAGGED]:
            proposed = ComponentState.FLAGGED
        reasons.append("never evaluated; no current evidence of fitness")

    final = proposed if _RANK[proposed] > _RANK[current] else current
    if final is current and not reasons:
        reasons.append("no deterioration detected")
    return ComponentHealth(
        component_id=component_id, kind=kind, state=final, since=now,
        reasons=tuple(reasons), signals=tuple(signals),
        last_evaluated_at=last_evaluated_at, changed_by="system")


class DeteriorationMonitor:
    """Persistent component health, with an asymmetric transition rule."""

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS,
                 thresholds: DeteriorationThresholds | None = None):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace
        self.thresholds = thresholds or DeteriorationThresholds()

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def health(self, component_id: str) -> ComponentHealth | None:
        body = self._store().get((component_id,))
        return ComponentHealth.from_dict(body) if body else None

    def _write(self, health: ComponentHealth) -> ComponentHealth:
        self._store().put((health.component_id,), health.to_dict())
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.DRIFT_DETECTED, health.to_dict(),
                                  actor=health.changed_by,
                                  subject=health.component_id)
            except Exception:                            # noqa: BLE001
                pass
        return health

    def components(self) -> list[str]:
        return sorted(parts[0] for parts in self._store().keys())

    def evaluate(self, component_id: str, *, kind: str,
                 signals: Sequence[DriftSignal] = (),
                 performance: PerformanceSnapshot | None = None,
                 last_evaluated_at: datetime | None = None
                 ) -> ComponentHealth:
        """Assess and persist. Autonomous — this is `RESTRICT_COMPONENT`.

        Deliberately requires no actor. Demoting a deteriorating analytical
        component is one of the powers Phase 10 grants Olympus outright, and
        putting an approval in front of it would mean a broken model keeps
        trading while someone is asleep.
        """
        existing = self.health(component_id)
        current = existing.state if existing else ComponentState.HEALTHY
        health = assess(component_id, kind=kind, signals=signals,
                        performance=performance, current=current,
                        last_evaluated_at=(last_evaluated_at
                                           or (existing.last_evaluated_at if existing else None)),
                        thresholds=self.thresholds, now=self.clock.now())
        if existing is not None and health.state is existing.state:
            # Refresh the evidence without resetting `since`: how long a
            # component has been degraded is itself the signal an operator reads.
            health = ComponentHealth(
                component_id=component_id, kind=kind, state=health.state,
                since=existing.since, reasons=health.reasons,
                signals=health.signals,
                last_evaluated_at=health.last_evaluated_at,
                changed_by=health.changed_by)
        return self._write(health)

    def restrict(self, component_id: str, *, kind: str, to: ComponentState,
                 reason: str, actor: Actor | None = None) -> ComponentHealth:
        """Move a component *down* the ladder explicitly.

        Refuses to move it up. An autonomous actor asking to restrict is
        authorised; asking to relax is a `GovernanceViolation` raised here
        rather than in `reinstate`, so the mistake is caught at the call that
        made it.
        """
        to = ComponentState(to)
        existing = self.health(component_id)
        current = existing.state if existing else ComponentState.HEALTHY
        if not is_demotion(current, to):
            raise GovernanceViolation(
                "restrict() only moves a component to a more restricted state; "
                "relaxing one requires an operator via reinstate()",
                component_id=component_id, current=current.value,
                requested=to.value)
        actor = actor or Actor.autonomous("drift-monitor")
        authorise(Action.RESTRICT_COMPONENT, actor, subject=component_id,
                  clock=self.clock, audit=self.audit)
        return self._write(ComponentHealth(
            component_id=component_id, kind=kind, state=to,
            since=self.clock.now(), reasons=(reason,),
            signals=existing.signals if existing else (),
            last_evaluated_at=existing.last_evaluated_at if existing else None,
            changed_by=actor.name))

    def reinstate(self, component_id: str, *, kind: str, to: ComponentState,
                  reason: str, actor: Actor) -> ComponentHealth:
        """Move a component *up*. Operator only, with evidence.

        The load-bearing refusal of this module. An autonomous actor calling
        this gets a `GovernanceViolation`, which is what stops a self-evolving
        system from concluding that its own deteriorating model has recovered.
        """
        to = ComponentState(to)
        existing = self.health(component_id)
        current = existing.state if existing else ComponentState.HEALTHY
        if is_demotion(current, to):
            raise ConfigurationError(
                "reinstate() moves a component to a less restricted state; use "
                "restrict() to go the other way", component_id=component_id,
                current=current.value, requested=to.value)
        if not str(reason).strip():
            raise ConfigurationError(
                "reinstatement requires a reason; 'it looks better now' has to "
                "be written down to be reviewable", component_id=component_id)
        authorise(Action.PROMOTE_CAPABILITY, actor, subject=component_id,
                  clock=self.clock, audit=self.audit)
        return self._write(ComponentHealth(
            component_id=component_id, kind=kind, state=to,
            since=self.clock.now(), reasons=(f"reinstated by {actor.name}: {reason}",),
            signals=(), last_evaluated_at=self.clock.now(),
            changed_by=actor.name))

    def operating(self) -> list[str]:
        return [c for c in self.components()
                if (h := self.health(c)) is not None and h.operating]

    def degraded(self) -> list[ComponentHealth]:
        out = [h for c in self.components() if (h := self.health(c)) is not None
               and h.state is not ComponentState.HEALTHY]
        return sorted(out, key=lambda h: (-_RANK[h.state], h.component_id))

    def report(self) -> dict:
        by_state: dict[str, int] = {}
        for component_id in self.components():
            health = self.health(component_id)
            if health is None:
                continue
            by_state[health.state.value] = by_state.get(health.state.value, 0) + 1
        return {"components": len(self.components()), "by_state": by_state,
                "degraded": [h.component_id for h in self.degraded()],
                "thresholds": self.thresholds.to_dict()}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"DeteriorationMonitor(namespace={self.namespace!r})"


# ---------------------------------------------------------------------------
# scheduled evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationJob:
    """A named, periodic measurement. Declarative so the set is inspectable.

    Enumerable on purpose, like `audit.EventType`: a required evaluation that
    nothing runs should be findable by reading the list rather than by noticing
    its absence in a report.
    """

    name: str
    kind: DriftKind
    #: Which slice: "by_instrument", "by_timeframe", "by_regime", "global".
    grouping: str
    interval: timedelta
    description: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind.value,
                "grouping": self.grouping,
                "interval_s": self.interval.total_seconds(),
                "description": self.description}


HOURLY = timedelta(hours=1)
DAILY = timedelta(days=1)
WEEKLY = timedelta(days=7)

#: The standing evaluation schedule. Every measurement the Phase 10 spec names,
#: mapped to a grouping and a cadence.
STANDARD_JOBS: tuple[EvaluationJob, ...] = (
    EvaluationJob("forecast_by_instrument", DriftKind.MODEL, "by_instrument", DAILY,
                  "Forecast error and directional accuracy per instrument."),
    EvaluationJob("forecast_by_timeframe", DriftKind.MODEL, "by_timeframe", DAILY,
                  "Forecast performance per timeframe."),
    EvaluationJob("forecast_by_regime", DriftKind.MODEL, "by_regime", DAILY,
                  "Forecast performance per market regime."),
    EvaluationJob("strategy_by_regime", DriftKind.MODEL, "by_regime", DAILY,
                  "Strategy P&L per regime, gross and net."),
    EvaluationJob("cost_impact", DriftKind.SLIPPAGE, "global", DAILY,
                  "Performance before and after costs; the gap is the "
                  "execution tax."),
    EvaluationJob("model_drift", DriftKind.MODEL, "by_instrument", DAILY,
                  "Distribution of model outputs vs the reference window."),
    EvaluationJob("data_drift", DriftKind.DATA, "by_instrument", HOURLY,
                  "Distribution of incoming market data."),
    EvaluationJob("feature_drift", DriftKind.FEATURE, "by_instrument", DAILY,
                  "Distribution of derived features."),
    EvaluationJob("calibration_drift", DriftKind.CALIBRATION, "global", DAILY,
                  "Whether stated confidence still predicts correctness."),
    EvaluationJob("failure_rate", DriftKind.FAILURE_RATE, "global", HOURLY,
                  "Rising error and rejection rates."),
    EvaluationJob("latency", DriftKind.LATENCY, "global", HOURLY,
                  "Decision-to-submission and venue round-trip latency."),
    EvaluationJob("slippage", DriftKind.SLIPPAGE, "by_instrument", DAILY,
                  "Realised vs expected fill price."),
    EvaluationJob("liquidity", DriftKind.LIQUIDITY, "by_instrument", DAILY,
                  "Depth and volume available at the sizes actually traded."),
    EvaluationJob("correlation", DriftKind.CORRELATION, "global", WEEKLY,
                  "Pairwise correlations; convergence invalidates "
                  "diversification assumptions in the risk limits."),
    EvaluationJob("microstructure", DriftKind.MICROSTRUCTURE, "by_instrument", WEEKLY,
                  "Spread, tick size and quote-update characteristics."),
)


class EvaluationSchedule:
    """Tracks which jobs are due. Pure bookkeeping — it runs nothing itself.

    Separating "what is due" from "what happens" keeps the scheduler testable
    without a job runner, and keeps a job's side effects out of a class whose
    correctness is about timing.
    """

    def __init__(self, jobs: Sequence[EvaluationJob] = STANDARD_JOBS, *,
                 clock: Clock | None = None):
        self.clock = clock or default_clock()
        self.jobs = tuple(jobs)
        self._last: dict[str, datetime] = {}

    def due(self, now: datetime | None = None) -> list[EvaluationJob]:
        now = ensure_utc(now or self.clock.now(), field_name="now")
        out = []
        for job in self.jobs:
            last = self._last.get(job.name)
            if last is None or now - last >= job.interval:
                out.append(job)
        return out

    def mark_ran(self, name: str, when: datetime | None = None) -> None:
        if not any(j.name == name for j in self.jobs):
            raise ConfigurationError("unknown evaluation job", name=name,
                                     known=[j.name for j in self.jobs])
        self._last[name] = ensure_utc(when or self.clock.now(), field_name="when")

    def to_dict(self) -> dict:
        return {"jobs": [j.to_dict() for j in self.jobs],
                "last_ran": {k: jsonable(v) for k, v in sorted(self._last.items())}}


__all__ = [
    "DriftKind", "Severity", "DriftSignal", "ComponentState", "ComponentHealth",
    "DeteriorationThresholds", "PerformanceSnapshot", "DeteriorationMonitor",
    "assess", "is_demotion", "population_stability_index", "ks_statistic",
    "ks_critical", "mean_shift", "quantile_edges", "detect_distribution_drift",
    "detect_metric_regression", "detect_calibration_drift",
    "detect_correlation_change", "EvaluationJob", "EvaluationSchedule",
    "STANDARD_JOBS", "PSI_MINOR", "PSI_MAJOR", "MIN_DRIFT_SAMPLE",
    "DEFAULT_EVALUATION_TTL", "HOURLY", "DAILY", "WEEKLY",
]
