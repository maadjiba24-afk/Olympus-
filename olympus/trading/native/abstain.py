"""The nine reasons a native forecast may decline to answer.

Abstention is an *output*, not an error path. A model that always answers is
not more useful than one that sometimes declines — it is a model whose failures
arrive as confident numbers, which is strictly worse, because a caller can act
on "no forecast" and cannot act correctly on a wrong one.

So each reason below is a check that runs, produces a measured value, compares
it against a stated threshold, and records both. `AbstentionDecision` carries
the reasons and the evidence, and `benchmark.py` scores abstention rate beside
every other metric so a model cannot win by declining the hard cases.

The nine
--------
============================  =========================================
`stale_input`                 the last bar closed too long before `as_of`
`missing_channels`            a channel the checkpoint consumes is absent
`instrument_out_of_distribution`  the instrument was not in the training set
`excessive_dispersion`        the predicted interval is too wide to act on
`poor_calibration`            recorded coverage error exceeds its tolerance
`unsupported_regime`          the current regime had too little training data
`unsupported_horizon`         the requested horizon is not the trained one
`incompatible_version`        architecture or task schema does not match
`unverified_checkpoint`       the content hash does not recompute
============================  =========================================

Two of them deserve their reasoning stated, because both are places where the
obvious implementation is wrong.

**`excessive_dispersion` is measured against an empirical reference, not an
analytic one.** A 5% interval is enormous for a bond and narrow for a small-cap
on an earnings day, so the check has to be relative. The obvious relative
reference — the window's own per-bar sigma, scaled by the square root of the
horizon — is *wrong*, and measuring it is how that was found: on an
autocorrelated series the true h-step dispersion is far larger than `sigma·√h`,
so the check fired on every forecast of a model whose intervals were in fact
covering 86% against a nominal 80%. The square-root rule assumes independent
increments, and a market that had independent increments would not be worth
forecasting.

So the reference is `ServingContext.typical_terminal_spread`: the empirical
p10–p90 spread of *actual* h-step returns in the training set, rescaled by how
this window's volatility compares with the training median. The caller computes
it and passes `expected_dispersion`; this module compares and does not assume.

**`unverified_checkpoint` fails closed and is checked first.** If the hash does
not recompute, nothing else about the checkpoint can be trusted — including the
thresholds this module would use to run the other eight checks. Ordering that
check last would mean evaluating a corrupt model's opinion of its own
reliability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when a reason's *meaning* changes, so a recorded abstention stays
#: interpretable against the policy that produced it.
ABSTENTION_SCHEMA_VERSION = 1


class AbstentionReason(str, Enum):
    """Values are stable identifiers written into forecast records."""

    STALE_INPUT = "stale_input"
    MISSING_CHANNELS = "missing_channels"
    INSTRUMENT_OUT_OF_DISTRIBUTION = "instrument_out_of_distribution"
    EXCESSIVE_DISPERSION = "excessive_dispersion"
    POOR_CALIBRATION = "poor_calibration"
    UNSUPPORTED_REGIME = "unsupported_regime"
    UNSUPPORTED_HORIZON = "unsupported_horizon"
    INCOMPATIBLE_VERSION = "incompatible_version"
    UNVERIFIED_CHECKPOINT = "unverified_checkpoint"


#: Human-readable statement of what each reason means, for a record a person
#: reads a year later. Kept beside the enum so a new reason cannot ship
#: undescribed — `AbstentionPolicy` asserts the mapping is total.
REASON_DESCRIPTIONS: Mapping[AbstentionReason, str] = {
    AbstentionReason.STALE_INPUT:
        "the most recent bar closed longer ago than the model's staleness "
        "tolerance, so the forecast would describe a market that has moved on",
    AbstentionReason.MISSING_CHANNELS:
        "a channel this checkpoint was trained to consume is absent, and "
        "substituting a value for it would be a fabricated observation",
    AbstentionReason.INSTRUMENT_OUT_OF_DISTRIBUTION:
        "this instrument was not in the training set and the model carries an "
        "instrument-specific component",
    AbstentionReason.EXCESSIVE_DISPERSION:
        "the predicted interval is much wider than the spread this "
        "instrument's returns actually show in this volatility regime, so the "
        "forecast carries little information",
    AbstentionReason.POOR_CALIBRATION:
        "the checkpoint's recorded coverage error exceeds its tolerance, so "
        "its stated uncertainty does not mean what it says",
    AbstentionReason.UNSUPPORTED_REGIME:
        "the current regime had too few training windows for this model to "
        "have learned it",
    AbstentionReason.UNSUPPORTED_HORIZON:
        "the requested horizon differs from the one the model was trained for",
    AbstentionReason.INCOMPATIBLE_VERSION:
        "the checkpoint's architecture or task-schema version does not match "
        "the running code, so its weights belong to a different function",
    AbstentionReason.UNVERIFIED_CHECKPOINT:
        "the checkpoint's content hash does not recompute, so nothing about "
        "it can be trusted",
}


@dataclass(frozen=True)
class AbstentionThresholds:
    """Where each line is drawn. Recorded in the checkpoint manifest."""

    #: Multiples of the bar interval the last bar may lag `as_of` by.
    max_staleness_bars: float = 2.0
    #: Predicted interval width as a multiple of the *expected* width for this
    #: instrument in this volatility regime — see the module docstring on why
    #: the analytic sqrt(horizon) reference was replaced by an empirical one.
    max_dispersion_ratio: float = 3.0
    #: Absolute coverage error, in coverage points, the checkpoint may carry.
    max_coverage_error: float = 15.0
    #: Minimum training windows a regime needed for the model to claim it.
    min_regime_support: int = 30
    #: Reconstruction-error percentile above which a window counts as OOD.
    max_ood_score: float = 0.99

    def __post_init__(self):
        for name in ("max_staleness_bars", "max_dispersion_ratio",
                     "max_coverage_error"):
            if float(getattr(self, name)) <= 0:
                raise ConfigurationError(f"{name} must be > 0",
                                         **{name: getattr(self, name)})
        if int(self.min_regime_support) < 0:
            raise ConfigurationError("min_regime_support must be >= 0")
        if not 0.0 < self.max_ood_score <= 1.0:
            raise ConfigurationError("max_ood_score must be in (0, 1]")

    def to_dict(self) -> dict:
        return {"max_staleness_bars": self.max_staleness_bars,
                "max_dispersion_ratio": self.max_dispersion_ratio,
                "max_coverage_error": self.max_coverage_error,
                "min_regime_support": self.min_regime_support,
                "max_ood_score": self.max_ood_score}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AbstentionThresholds":
        return cls(max_staleness_bars=float(data.get("max_staleness_bars", 2.0)),
                   max_dispersion_ratio=float(data.get("max_dispersion_ratio", 3.0)),
                   max_coverage_error=float(data.get("max_coverage_error", 15.0)),
                   min_regime_support=int(data.get("min_regime_support", 30)),
                   max_ood_score=float(data.get("max_ood_score", 0.99)))


@dataclass(frozen=True)
class AbstentionFinding:
    """One check that fired, with the number that made it fire."""

    reason: AbstentionReason
    detail: str
    measured: float | None = None
    threshold: float | None = None
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "reason", AbstentionReason(self.reason))
        object.__setattr__(self, "context", dict(self.context))

    def to_dict(self) -> dict:
        return {"reason": self.reason.value, "detail": self.detail,
                "measured": self.measured, "threshold": self.threshold,
                "context": jsonable(dict(self.context)),
                "meaning": REASON_DESCRIPTIONS[self.reason]}


@dataclass(frozen=True)
class AbstentionDecision:
    """Whether to answer, why not, and what was measured either way.

    Carries the *passing* measurements too. A record showing "dispersion ratio
    2.1 against a limit of 6.0" is what lets someone later ask whether the
    limit is right; a record that only says "did not abstain" does not.
    """

    abstained: bool
    findings: tuple[AbstentionFinding, ...] = ()
    measurements: Mapping[str, float] = field(default_factory=dict)
    thresholds: Mapping[str, float] = field(default_factory=dict)
    schema_version: int = ABSTENTION_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "measurements", dict(self.measurements))
        object.__setattr__(self, "thresholds", dict(self.thresholds))
        if self.abstained and not self.findings:
            raise ConfigurationError(
                "an abstention with no reason is indistinguishable from a bug; "
                "if the model declined, something has to say why")
        if self.findings and not self.abstained:
            raise ConfigurationError(
                "findings were recorded but the decision was to answer",
                findings=[f.reason.value for f in self.findings])

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f.reason.value for f in self.findings)

    @property
    def primary(self) -> AbstentionReason | None:
        """The first finding. Checks run in severity order, so this is the one
        to show a human."""
        return self.findings[0].reason if self.findings else None

    def to_dict(self) -> dict:
        return {"abstained": self.abstained,
                "reasons": list(self.reasons),
                "primary": self.primary.value if self.primary else None,
                "findings": [f.to_dict() for f in self.findings],
                "measurements": dict(self.measurements),
                "thresholds": dict(self.thresholds),
                "schema_version": self.schema_version}


#: Severity order. `unverified_checkpoint` is first because if it fires,
#: nothing else about the checkpoint — including the thresholds the other eight
#: checks would use — can be trusted.
CHECK_ORDER: tuple[AbstentionReason, ...] = (
    AbstentionReason.UNVERIFIED_CHECKPOINT,
    AbstentionReason.INCOMPATIBLE_VERSION,
    AbstentionReason.UNSUPPORTED_HORIZON,
    AbstentionReason.MISSING_CHANNELS,
    AbstentionReason.STALE_INPUT,
    AbstentionReason.INSTRUMENT_OUT_OF_DISTRIBUTION,
    AbstentionReason.UNSUPPORTED_REGIME,
    AbstentionReason.POOR_CALIBRATION,
    AbstentionReason.EXCESSIVE_DISPERSION,
)


class AbstentionPolicy:
    """Runs all nine checks and returns a decision.

    Stateless and side-effect free. It reads what it is given and never asks
    the model to re-run anything, so a decision can be reconstructed from the
    record without the model.
    """

    def __init__(self, thresholds: AbstentionThresholds | None = None):
        self.thresholds = thresholds or AbstentionThresholds()
        missing = [r for r in AbstentionReason if r not in REASON_DESCRIPTIONS]
        if missing:                                      # pragma: no cover
            raise ConfigurationError(
                "a reason ships without a description",
                missing=[r.value for r in missing])
        if set(CHECK_ORDER) != set(AbstentionReason):    # pragma: no cover
            raise ConfigurationError("CHECK_ORDER does not cover every reason")

    def evaluate(self, *,
                 checkpoint_verified: bool = True,
                 checkpoint_hash: str = "",
                 recomputed_hash: str = "",
                 architecture_version: str = "",
                 expected_architecture_version: str = "",
                 task_schema_version: int = 0,
                 expected_task_schema_version: int = 0,
                 requested_horizon: int = 0,
                 trained_horizon: int = 0,
                 missing_channels: Sequence[str] = (),
                 last_bar_close: datetime | None = None,
                 as_of: datetime | None = None,
                 bar_seconds: float = 0.0,
                 instrument_key: str = "",
                 trained_instruments: Sequence[str] = (),
                 has_instrument_embedding: bool = False,
                 dispersion: float | None = None,
                 expected_dispersion: float | None = None,
                 coverage_error: float | None = None,
                 regime: str = "",
                 regime_support: Mapping[str, int] | None = None,
                 ood_score: float | None = None,
                 ) -> AbstentionDecision:
        """Every check, in severity order. Returns as soon as it is decided
        *what* to report, but runs every check so the record is complete."""
        findings: list[AbstentionFinding] = []
        measurements: dict[str, float] = {}
        limits = self.thresholds

        # 1. checkpoint integrity — fail closed, and first.
        if not checkpoint_verified or (
                checkpoint_hash and recomputed_hash
                and checkpoint_hash != recomputed_hash):
            findings.append(AbstentionFinding(
                reason=AbstentionReason.UNVERIFIED_CHECKPOINT,
                detail="the checkpoint's content hash does not recompute",
                context={"declared": checkpoint_hash[:16],
                         "recomputed": recomputed_hash[:16]}))

        # 2. version compatibility.
        version_problems = {}
        if (expected_architecture_version
                and architecture_version != expected_architecture_version):
            version_problems["architecture"] = {
                "checkpoint": architecture_version,
                "running": expected_architecture_version}
        if (expected_task_schema_version
                and task_schema_version != expected_task_schema_version):
            version_problems["task_schema"] = {
                "checkpoint": task_schema_version,
                "running": expected_task_schema_version}
        if version_problems:
            findings.append(AbstentionFinding(
                reason=AbstentionReason.INCOMPATIBLE_VERSION,
                detail="the checkpoint was produced by a different version of "
                       "this code, so its weights belong to a different function",
                context=version_problems))

        # 3. horizon.
        if requested_horizon and trained_horizon and requested_horizon != trained_horizon:
            findings.append(AbstentionFinding(
                reason=AbstentionReason.UNSUPPORTED_HORIZON,
                detail="the model emits a fixed horizon and cannot be asked "
                       "for another one",
                measured=float(requested_horizon),
                threshold=float(trained_horizon)))

        # 4. missing channels.
        absent = tuple(missing_channels)
        if absent:
            findings.append(AbstentionFinding(
                reason=AbstentionReason.MISSING_CHANNELS,
                detail="a channel this checkpoint consumes is absent",
                measured=float(len(absent)), threshold=0.0,
                context={"missing": list(absent)}))

        # 5. staleness.
        if last_bar_close is not None and as_of is not None and bar_seconds > 0:
            lag = (ensure_utc(as_of, field_name="as_of")
                   - ensure_utc(last_bar_close, field_name="last_bar_close")
                   ).total_seconds()
            bars_late = lag / bar_seconds
            measurements["staleness_bars"] = bars_late
            if bars_late > limits.max_staleness_bars:
                findings.append(AbstentionFinding(
                    reason=AbstentionReason.STALE_INPUT,
                    detail="the last bar closed too long before the forecast "
                           "instant",
                    measured=bars_late, threshold=limits.max_staleness_bars))

        # 6. instrument out of distribution.
        #    Only meaningful when the model carries an instrument-specific
        #    component. An instrument-agnostic model applied to a new symbol is
        #    doing exactly what it was built for.
        if has_instrument_embedding and instrument_key and trained_instruments:
            if instrument_key not in set(trained_instruments):
                findings.append(AbstentionFinding(
                    reason=AbstentionReason.INSTRUMENT_OUT_OF_DISTRIBUTION,
                    detail="this instrument was not in the training set and "
                           "the model has a per-instrument embedding",
                    context={"instrument": instrument_key,
                             "trained_on": len(trained_instruments)}))

        # 7. regime support.
        if regime and regime_support is not None:
            support = int(regime_support.get(regime, 0))
            measurements["regime_support"] = float(support)
            if support < limits.min_regime_support:
                findings.append(AbstentionFinding(
                    reason=AbstentionReason.UNSUPPORTED_REGIME,
                    detail="this regime had too few training windows for the "
                           "model to have learned it",
                    measured=float(support),
                    threshold=float(limits.min_regime_support),
                    context={"regime": regime}))

        # 8. calibration.
        if coverage_error is not None:
            measurements["coverage_error"] = abs(float(coverage_error))
            if abs(float(coverage_error)) > limits.max_coverage_error:
                findings.append(AbstentionFinding(
                    reason=AbstentionReason.POOR_CALIBRATION,
                    detail="the checkpoint's recorded coverage error exceeds "
                           "its tolerance, so its stated uncertainty does not "
                           "mean what it says",
                    measured=abs(float(coverage_error)),
                    threshold=limits.max_coverage_error))

        # 9. dispersion, against the empirically expected width.
        if dispersion is not None:
            measurements["dispersion"] = float(dispersion)
            expected = float(expected_dispersion or 0.0)
            if expected > 0:
                measurements["expected_dispersion"] = expected
                ratio = float(dispersion) / expected
                measurements["dispersion_ratio"] = ratio
                if ratio > limits.max_dispersion_ratio:
                    findings.append(AbstentionFinding(
                        reason=AbstentionReason.EXCESSIVE_DISPERSION,
                        detail="the predicted interval is much wider than the "
                               "spread this instrument's returns actually show "
                               "in this volatility regime",
                        measured=ratio,
                        threshold=limits.max_dispersion_ratio))

        if ood_score is not None:
            measurements["ood_score"] = float(ood_score)

        order = list(CHECK_ORDER)
        findings.sort(key=lambda f: order.index(f.reason))
        return AbstentionDecision(abstained=bool(findings),
                                  findings=tuple(findings),
                                  measurements=measurements,
                                  thresholds=limits.to_dict())

    def describe(self) -> dict:
        return {"schema_version": ABSTENTION_SCHEMA_VERSION,
                "thresholds": self.thresholds.to_dict(),
                "reasons": {r.value: REASON_DESCRIPTIONS[r]
                            for r in CHECK_ORDER},
                "check_order": [r.value for r in CHECK_ORDER]}


__all__ = ["ABSTENTION_SCHEMA_VERSION", "AbstentionReason",
           "REASON_DESCRIPTIONS", "AbstentionThresholds", "AbstentionFinding",
           "AbstentionDecision", "CHECK_ORDER", "AbstentionPolicy"]
