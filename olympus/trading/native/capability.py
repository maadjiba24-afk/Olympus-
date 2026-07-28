"""The capability register: what exists, what is evidenced, what may be enabled.

> A capability is not complete merely because an interface exists.

That sentence is the module. Every capability declares eight facts about
itself, and `production_eligible` is **computed** from them — there is no field,
argument or method that sets it. A capability whose interface is written, whose
tests pass and which has never seen real data is `IMPLEMENTED` with
`RealDataEvaluation.NONE`, and it will report itself ineligible however
confident its author was.

Why a register rather than a docstring
--------------------------------------
Because the alternative fails silently. A capability described as "supported" in
a README, enabled by a config flag, and never evaluated is indistinguishable at
runtime from one that works. Here `CapabilityRegistry.enable` refuses a
capability whose evidence does not support it and says which fact is missing —
so an unsupported capability cannot be turned on by accident, and turning one on
deliberately requires `force=True` and lands an explicit override in the record.

The eight facts
---------------
============================  ===========================================
`implementation`              designed / partial / implemented
`dataset`                     is the data reachable at all
`unit_tests`                  none / partial / adversarial
`historical_evaluation`       measured on a historical corpus
`real_data_evaluation`        measured on real market data
`paper_trading_evaluation`    exercised against a paper broker
`limitations`                 at least one, always
`production_eligible`         **computed**, never set
============================  ===========================================

`limitations` being mandatory is deliberate. A capability that lists none has
not been thought about, and "none known" is a statement that somebody has to
type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..errors import ConfigurationError

#: Bumped when the meaning of a status field changes, so an older record is
#: detectably older rather than partially read.
CAPABILITY_SCHEMA_VERSION = 1


class Implementation(str, Enum):
    DESIGNED = "designed"          # written down; no code
    PARTIAL = "partial"            # some of it runs
    IMPLEMENTED = "implemented"    # all of the declared surface runs


class DataAvailability(str, Enum):
    #: Reachable and present.
    AVAILABLE = "available"
    #: Computable from what is present, by code Olympus owns.
    DERIVABLE = "derivable"
    #: Olympus owns this data itself — the portfolio, the fill history.
    INTERNAL = "internal"
    #: The upstream field exists; Olympus does not fetch it.
    NOT_INGESTED = "not_ingested"
    #: No provider is reachable from this environment.
    UNREACHABLE = "unreachable"


class TestStatus(str, Enum):
    NONE = "none"
    HAPPY_PATH = "happy_path"
    #: Tests exercise the failure modes, not only the success path.
    ADVERSARIAL = "adversarial"


class EvaluationStatus(str, Enum):
    NONE = "none"
    #: Measured on series this repository generated.
    SYNTHETIC = "synthetic"
    #: Measured on a historical corpus that was not generated here.
    HISTORICAL = "historical"
    #: Measured live, or on a paper broker fed by real quotes.
    LIVE = "live"
    #: Cannot be measured here, with a stated reason.
    BLOCKED = "blocked"


#: What `production_eligible` requires. Stated as data so the rule is readable
#: rather than buried in a conditional, and so a test can assert the rule
#: itself rather than only its consequences.
ELIGIBILITY_RULE: Mapping[str, Any] = {
    "implementation": Implementation.IMPLEMENTED,
    "unit_tests": TestStatus.ADVERSARIAL,
    "historical_evaluation": (EvaluationStatus.HISTORICAL, EvaluationStatus.LIVE),
    "real_data_evaluation": (EvaluationStatus.HISTORICAL, EvaluationStatus.LIVE),
    "paper_trading_evaluation": (EvaluationStatus.LIVE,),
    "limitations": "at least one stated",
}


@dataclass(frozen=True)
class CapabilityStatus:
    """The eight facts. `production_eligible` is computed from the other seven."""

    name: str
    summary: str
    implementation: Implementation
    dataset: DataAvailability
    unit_tests: TestStatus
    historical_evaluation: EvaluationStatus = EvaluationStatus.NONE
    real_data_evaluation: EvaluationStatus = EvaluationStatus.NONE
    paper_trading_evaluation: EvaluationStatus = EvaluationStatus.NONE
    limitations: tuple[str, ...] = ()
    #: What would have to change for this to become eligible. Written for the
    #: person deciding whether to invest in it.
    blocked_by: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self):
        if not str(self.name).strip():
            raise ConfigurationError("a capability must be named")
        if not str(self.summary).strip():
            raise ConfigurationError("a capability must describe itself",
                                     capability=self.name)
        object.__setattr__(self, "implementation", Implementation(self.implementation))
        object.__setattr__(self, "dataset", DataAvailability(self.dataset))
        object.__setattr__(self, "unit_tests", TestStatus(self.unit_tests))
        for name in ("historical_evaluation", "real_data_evaluation",
                     "paper_trading_evaluation"):
            object.__setattr__(self, name,
                               EvaluationStatus(getattr(self, name)))
        for name in ("limitations", "blocked_by", "modules", "tests"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not self.limitations:
            raise ConfigurationError(
                "a capability must state at least one limitation; 'none known' "
                "is a statement and somebody has to type it",
                capability=self.name)
        # A blocked evaluation with no reason is indistinguishable from one
        # nobody attempted.
        blocked = [n for n in ("historical_evaluation", "real_data_evaluation",
                               "paper_trading_evaluation")
                   if getattr(self, n) is EvaluationStatus.BLOCKED]
        if blocked and not self.blocked_by:
            raise ConfigurationError(
                "an evaluation marked BLOCKED must say what is blocking it",
                capability=self.name, blocked=blocked)

    @property
    def production_eligible(self) -> bool:
        """Computed. There is no argument that sets this.

        Every one of the seven has to hold. A capability that is implemented,
        adversarially tested and measured on a historical corpus is still
        ineligible without a paper-trading result, because the failure modes
        that only appear against a live venue are exactly the ones a historical
        evaluation cannot show.
        """
        return (self.implementation is Implementation.IMPLEMENTED
                and self.unit_tests is TestStatus.ADVERSARIAL
                and self.historical_evaluation in
                (EvaluationStatus.HISTORICAL, EvaluationStatus.LIVE)
                and self.real_data_evaluation in
                (EvaluationStatus.HISTORICAL, EvaluationStatus.LIVE)
                and self.paper_trading_evaluation is EvaluationStatus.LIVE
                and bool(self.limitations))

    @property
    def missing_for_production(self) -> tuple[str, ...]:
        """Which of the seven fail. Empty when eligible."""
        out = []
        if self.implementation is not Implementation.IMPLEMENTED:
            out.append(f"implementation is {self.implementation.value}")
        if self.unit_tests is not TestStatus.ADVERSARIAL:
            out.append(f"unit tests are {self.unit_tests.value}")
        if self.historical_evaluation not in (EvaluationStatus.HISTORICAL,
                                              EvaluationStatus.LIVE):
            out.append(f"historical evaluation is "
                       f"{self.historical_evaluation.value}")
        if self.real_data_evaluation not in (EvaluationStatus.HISTORICAL,
                                             EvaluationStatus.LIVE):
            out.append(f"real-data evaluation is "
                       f"{self.real_data_evaluation.value}")
        if self.paper_trading_evaluation is not EvaluationStatus.LIVE:
            out.append(f"paper-trading evaluation is "
                       f"{self.paper_trading_evaluation.value}")
        if not self.limitations:                         # pragma: no cover
            out.append("no limitations stated")
        return tuple(out)

    @property
    def usable_for_research(self) -> bool:
        """Weaker bar: does this produce numbers worth looking at at all?

        Implemented and adversarially tested. Research use is where a
        capability with no real-data evidence legitimately lives, and having a
        separate predicate for it is what stops "not production eligible" being
        read as "not usable".
        """
        return (self.implementation is Implementation.IMPLEMENTED
                and self.unit_tests is TestStatus.ADVERSARIAL)

    @property
    def evidence_class(self) -> str:
        """The strongest evidence behind this capability, as one word."""
        for status, label in ((self.paper_trading_evaluation, "paper_trading"),
                              (self.real_data_evaluation, "real_data"),
                              (self.historical_evaluation, "historical")):
            if status in (EvaluationStatus.HISTORICAL, EvaluationStatus.LIVE):
                return label
        for status in (self.historical_evaluation, self.real_data_evaluation):
            if status is EvaluationStatus.SYNTHETIC:
                return "synthetic"
        if self.unit_tests is not TestStatus.NONE:
            return "unit_tested"
        return "none"

    def to_dict(self) -> dict:
        return {"name": self.name, "summary": self.summary,
                "implementation": self.implementation.value,
                "dataset": self.dataset.value,
                "unit_tests": self.unit_tests.value,
                "historical_evaluation": self.historical_evaluation.value,
                "real_data_evaluation": self.real_data_evaluation.value,
                "paper_trading_evaluation": self.paper_trading_evaluation.value,
                "limitations": list(self.limitations),
                "blocked_by": list(self.blocked_by),
                "modules": list(self.modules), "tests": list(self.tests),
                "production_eligible": self.production_eligible,
                "usable_for_research": self.usable_for_research,
                "missing_for_production": list(self.missing_for_production),
                "evidence_class": self.evidence_class,
                "schema_version": self.schema_version}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityStatus":
        return cls(name=str(data["name"]), summary=str(data["summary"]),
                   implementation=Implementation(data["implementation"]),
                   dataset=DataAvailability(data["dataset"]),
                   unit_tests=TestStatus(data["unit_tests"]),
                   historical_evaluation=EvaluationStatus(
                       data.get("historical_evaluation", "none")),
                   real_data_evaluation=EvaluationStatus(
                       data.get("real_data_evaluation", "none")),
                   paper_trading_evaluation=EvaluationStatus(
                       data.get("paper_trading_evaluation", "none")),
                   limitations=tuple(data.get("limitations") or ()),
                   blocked_by=tuple(data.get("blocked_by") or ()),
                   modules=tuple(data.get("modules") or ()),
                   tests=tuple(data.get("tests") or ()))


class CapabilityRefused(ConfigurationError):
    """A capability was enabled whose evidence does not support it.

    A distinct type because this is the one configuration error a caller may
    legitimately want to catch and route to an operator rather than treat as a
    bug: "this needs a decision" rather than "this is broken".
    """


class CapabilityRegistry:
    """The register, and the gate on enabling anything in it.

    Enabling is explicit and recorded. `enable` refuses a capability that is not
    `usable_for_research`, and refuses a *production* enable for anything not
    `production_eligible` — with `force=True` as the deliberate override, which
    lands in `overrides` so nobody can later claim it was implicit.
    """

    __slots__ = ("_statuses", "_enabled", "_overrides")

    def __init__(self, statuses: Sequence[CapabilityStatus] = ()):
        self._statuses: dict[str, CapabilityStatus] = {}
        self._enabled: dict[str, str] = {}
        self._overrides: dict[str, str] = {}
        for status in statuses:
            self.register(status)

    def register(self, status: CapabilityStatus) -> "CapabilityRegistry":
        if status.name in self._statuses:
            raise ConfigurationError("capability already registered",
                                     capability=status.name)
        self._statuses[status.name] = status
        return self

    # -- access ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._statuses)

    def __iter__(self) -> Iterator[CapabilityStatus]:
        return iter(self._statuses.values())

    def __contains__(self, name: object) -> bool:
        return name in self._statuses

    def __getitem__(self, name: str) -> CapabilityStatus:
        try:
            return self._statuses[name]
        except KeyError:
            raise ConfigurationError("unknown capability", capability=name,
                                     known=sorted(self._statuses)) from None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._statuses)

    def production_eligible(self) -> tuple[str, ...]:
        return tuple(s.name for s in self if s.production_eligible)

    def research_usable(self) -> tuple[str, ...]:
        return tuple(s.name for s in self if s.usable_for_research)

    def blocked(self) -> Mapping[str, tuple[str, ...]]:
        return {s.name: s.blocked_by for s in self if s.blocked_by}

    # -- the gate ----------------------------------------------------------

    def enable(self, name: str, *, mode: str = "research",
               reason: str = "", force: bool = False) -> CapabilityStatus:
        """Turn a capability on, or refuse and say which fact is missing.

        `mode="research"` needs it implemented and adversarially tested.
        `mode="production"` needs all seven. `force=True` overrides — and is
        recorded, because an override nobody can find is an override that will
        be forgotten.
        """
        status = self[name]
        if mode not in ("research", "production"):
            raise ConfigurationError("mode must be 'research' or 'production'",
                                     mode=mode)

        if mode == "production":
            ok, missing = status.production_eligible, status.missing_for_production
        else:
            ok = status.usable_for_research
            missing = tuple(m for m in status.missing_for_production
                            if m.startswith(("implementation", "unit tests")))

        if not ok and not force:
            raise CapabilityRefused(
                "this capability's evidence does not support enabling it",
                capability=name, mode=mode, missing=list(missing),
                blocked_by=list(status.blocked_by))
        if not ok and force:
            if not reason.strip():
                raise ConfigurationError(
                    "forcing an unsupported capability requires a reason; an "
                    "override nobody can find is an override that will be "
                    "forgotten", capability=name)
            self._overrides[name] = reason
        self._enabled[name] = mode
        return status

    def disable(self, name: str) -> None:
        self._enabled.pop(name, None)
        self._overrides.pop(name, None)

    def is_enabled(self, name: str) -> bool:
        return name in self._enabled

    def mode(self, name: str) -> str:
        return self._enabled.get(name, "disabled")

    @property
    def enabled(self) -> Mapping[str, str]:
        return dict(self._enabled)

    @property
    def overrides(self) -> Mapping[str, str]:
        """Capabilities enabled against their own evidence, and why."""
        return dict(self._overrides)

    # -- reporting ---------------------------------------------------------

    def report(self) -> dict:
        return {"schema_version": CAPABILITY_SCHEMA_VERSION,
                "capabilities": {s.name: s.to_dict() for s in self},
                "production_eligible": list(self.production_eligible()),
                "research_usable": list(self.research_usable()),
                "enabled": dict(self._enabled),
                "overrides": dict(self._overrides),
                "blocked": {k: list(v) for k, v in self.blocked().items()},
                "eligibility_rule": {
                    k: (v.value if isinstance(v, Enum)
                        else [x.value for x in v] if isinstance(v, tuple)
                        else v)
                    for k, v in ELIGIBILITY_RULE.items()}}

    def table(self) -> str:
        """Fixed-width, for a report. The right-hand column is the conclusion."""
        header = (f"{'capability':<26}{'impl':>12}{'data':>14}{'tests':>13}"
                  f"{'hist':>11}{'real':>10}{'paper':>9}{'prod':>7}")
        lines = [header, "-" * len(header)]
        for status in self:
            lines.append(
                f"{status.name:<26}{status.implementation.value:>12}"
                f"{status.dataset.value:>14}{status.unit_tests.value:>13}"
                f"{status.historical_evaluation.value:>11}"
                f"{status.real_data_evaluation.value:>10}"
                f"{status.paper_trading_evaluation.value:>9}"
                f"{('yes' if status.production_eligible else 'NO'):>7}")
        eligible = self.production_eligible()
        lines.append("")
        lines.append(f"production eligible: {', '.join(eligible) or 'NONE'}")
        lines.append(f"research usable:     "
                     f"{', '.join(self.research_usable()) or 'none'}")
        if self._overrides:
            for name, reason in sorted(self._overrides.items()):
                lines.append(f"OVERRIDE {name}: {reason}")
        return "\n".join(lines)

    def to_json(self) -> str:                            # pragma: no cover
        return json.dumps(self.report(), indent=2)


# ===========================================================================
# the nine Phase-3 capabilities
# ===========================================================================
#
# Filled in from what is actually true, not from what was built. Every one is
# `IMPLEMENTED` and `ADVERSARIAL`; **none is production eligible**, because
# none has a real-data or paper-trading evaluation and no market-data provider
# is reachable (blocker B1). `production_eligible` computes that; nothing here
# asserts it.

def native_capabilities() -> CapabilityRegistry:
    """The register, as of this commit. Read `table()` for the summary."""
    B1 = "no market-data provider is reachable from this environment (B1)"
    NO_PAPER = ("no paper-trading run has been performed against real quotes; "
                "the paper broker is fed by synthetic bars")

    return CapabilityRegistry([
        CapabilityStatus(
            name="multi_timeframe",
            summary="Joint reasoning across eight declared scales, with every "
                    "bar classified closed / partial / delayed / absent.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.NOT_INGESTED,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/timeframes.py",),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "only one timeframe (1h) of synthetic data exists, so the "
                "ladder has been exercised on constructed series and never on "
                "a real multi-scale feed",
                "tick is declared and permanently unavailable — it is a stream, "
                "not a bar, and nothing here consumes one",
                "no model consumes more than the base scale yet; the ladder "
                "produces features and the multi-task model does not read them",
            ),
            blocked_by=(B1, "no tick or sub-minute feed exists")),

        CapabilityStatus(
            name="cross_asset",
            summary="Declared relationship graph with seven edge kinds, and "
                    "features computed strictly as of the prediction instant.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.UNREACHABLE,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/crossasset.py", "native/dataset.py"),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "no reference-data source is reachable, so every graph tested "
                "has been constructed by hand",
                "relationships are declared rather than estimated, which is "
                "correct — an estimated edge uses the future to find itself — "
                "and means the capability is only as good as its reference data",
                "one instrument's worth of reachable bars, and it is synthetic",
            ),
            blocked_by=(B1, "no instrument reference data is reachable")),

        CapabilityStatus(
            name="order_book_liquidity",
            summary="Book snapshots, spread, depth, imbalance, aggressor flow, "
                    "fill probability, slippage, impact, deterioration — and "
                    "the gate that stops a positive forecast being tradable "
                    "by itself.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.NOT_INGESTED,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/microstructure.py",),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "**nothing here is calibrated.** Fill probability, slippage "
                "and impact all run on declared default parameters and report "
                "`calibrated=False`; the numbers have the right shape and the "
                "wrong scale",
                "the impact coefficient could be fitted from `outcomes.py`'s "
                "recorded fills and has not been — that is the one calibration "
                "this system could do today",
                "no bid, ask or depth is ingested, so every snapshot tested "
                "was constructed",
            ),
            blocked_by=(B1, "no order-book feed is reachable",
                        "no fill history has been used to fit the impact model")),

        CapabilityStatus(
            name="event_awareness",
            summary="Event calendar with sanitised external text, timing-only "
                    "features, and a hard boundary between events and controls.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.NOT_INGESTED,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/events.py", "sentiment.py"),
            tests=("tests/test_trading_native_capabilities.py",
                   "tests/test_trading_native_robustness.py"),
            limitations=(
                "no economic or corporate calendar is reachable; every event "
                "tested was constructed",
                "content is deliberately not modelled — timing and declared "
                "importance only — because a numeric surprise without vintages "
                "is a look-ahead of days",
                "the text boundary rests on `sentiment.sanitise_external_text`, "
                "which is a denylist: it neutralises what it recognises",
            ),
            blocked_by=(B1, "no event calendar is reachable")),

        CapabilityStatus(
            name="portfolio_aware",
            summary="Forecasts conditioned on positions, concentration, "
                    "correlation, exposure, marginal risk, drawdown "
                    "contribution, hedges and liquidity requirements — "
                    "read-only by construction.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.INTERNAL,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.NONE,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/portfolio_context.py", "portfolio.py"),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "correlations are supplied, not estimated here — a portfolio "
                "view is a snapshot and correlation needs a return history",
                "marginal risk is the cheap version: the sign of the change in "
                "gross exposure, not a covariance-aware contribution",
                "the portfolio state is real, but the prices marking it come "
                "from synthetic bars, so every exposure figure is real "
                "arithmetic over unreal prices",
            ),
            blocked_by=("real marks need real market data (B1)",)),

        CapabilityStatus(
            name="regime_specialists",
            summary="Eight specialists plus a general fallback, with every "
                    "routing decision recorded and evaluable against outcomes.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.DERIVABLE,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/specialists.py", "regime.py"),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "**no specialist has been trained.** The router, the fallback "
                "logic and the routing record are complete; the specialists "
                "themselves are registered slots",
                "the routing rules encode a judgement about what each regime "
                "looks like, and that judgement has not been validated against "
                "outcomes",
                "the regime classifier needs a long warm-up, so at the "
                "lookbacks used here it reports UNKNOWN and the generalist "
                "answers everything",
            ),
            blocked_by=("training a specialist per regime needs enough data "
                        "per regime, which needs " + B1,)),

        CapabilityStatus(
            name="scenario_generation",
            summary="Six probabilistic scenarios per forecast, summing to one, "
                    "each with drivers and falsification conditions.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.DERIVABLE,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/scenarios.py",),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "the three conditional scenarios take **declared** "
                "probabilities: a return distribution contains no information "
                "about whether the exchange will halt",
                "scenarios are bands, not simulated paths — producing paths "
                "would need a generative model this system does not have",
                "no scenario set has been scored against what actually "
                "happened, so the probabilities are untested",
            ),
            blocked_by=("scoring scenario probabilities needs outcomes over a "
                        "real corpus (" + B1 + ")",)),

        CapabilityStatus(
            name="explainability",
            summary="Closed-enumeration reason codes with measurements, "
                    "uncertainty sources, data limitations, risk factors and "
                    "falsification conditions. Prose is generated from codes.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.DERIVABLE,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.NONE,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/explain.py",),
            tests=("tests/test_trading_native_capabilities.py",),
            limitations=(
                "reason codes are emitted by rules over the available context, "
                "not by attribution over the model's own computation — they "
                "say what conditions held, not which input moved the output",
                "no faithfulness check exists: nothing verifies that a stated "
                "driver actually drove the number",
                "`dominant_uncertainty` returns `None` unless the emitter "
                "ranked its reasons, which most call sites do not",
            ),
            blocked_by=("gradient- or perturbation-based attribution is "
                        "implementable and has not been built",)),

        CapabilityStatus(
            name="robustness",
            summary="Thirteen adversarial conditions exercised end to end: "
                    "missing channels, delay, corruption, volatility shocks, "
                    "outages, regime change, new instruments, abnormal "
                    "spreads, flash crashes, gaps, manipulated text, "
                    "dependency failure and serving restart.",
            implementation=Implementation.IMPLEMENTED,
            dataset=DataAvailability.DERIVABLE,
            unit_tests=TestStatus.ADVERSARIAL,
            historical_evaluation=EvaluationStatus.SYNTHETIC,
            real_data_evaluation=EvaluationStatus.BLOCKED,
            paper_trading_evaluation=EvaluationStatus.NONE,
            modules=("native/abstain.py", "native/serve.py",
                     "native/timeframes.py", "native/microstructure.py"),
            tests=("tests/test_trading_native_robustness.py",),
            limitations=(
                "every adversarial condition is *constructed*: a flash crash "
                "here is a synthetic series with a hole in it, not one that "
                "happened",
                "the tests establish that the system degrades safely, not that "
                "it degrades well — safe degradation is refusing to answer, and "
                "a system that refuses everything would also pass",
                "no chaos testing against a live venue has been done",
            ),
            blocked_by=(NO_PAPER, B1)),
    ])


__all__ = ["CAPABILITY_SCHEMA_VERSION", "Implementation", "DataAvailability",
           "TestStatus", "EvaluationStatus", "ELIGIBILITY_RULE",
           "CapabilityStatus", "CapabilityRefused", "CapabilityRegistry",
           "native_capabilities"]
