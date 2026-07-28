"""The champion–challenger gate: twelve stages, and who may move between them.

> A challenger may progress only through: static validation, unit tests,
> leakage tests, historical evaluation, walk-forward evaluation, baseline
> comparison, robustness tests, calibration tests, paper trading, shadow mode,
> human review, restricted promotion.

The order is the contract. `advance()` accepts only the next stage, so a
challenger cannot reach paper trading without a leakage test — not because
somebody would skip it deliberately, but because a pipeline that permits
skipping will skip under time pressure and nobody will notice which stage was
dropped.

What Olympus may do here, and what it may not
----------------------------------------------
Autonomous, and exercised by this module:

* run any of stages 1–10 and record the result
* **reject** a challenger at any stage
* **restrict** a deteriorating incumbent
* **demote** it
* **roll back** to a version a human already approved
* engage a safety shutdown

Human-only, and not implemented here at all:

* promote, deploy, enable live trading, change risk limits, expand
  permissions, clear a kill switch, reach broker credentials, modify the
  kernel

The asymmetry is the same one `governance.py` already encodes and the reasoning
is the same: stopping something is cheap when wrong and expensive when late.
`promote()` exists and its first act is `governance.authorise`, which raises
for any non-operator actor. There is no `force`, no `skip_review`, and no
argument that makes an autonomous actor acceptable — the check is on the
actor's kind, and an autonomous engine has no way to become an operator.

Restricted promotion is a promotion with a shape
------------------------------------------------
Stage 12 is not "in production". `Restriction` carries the instruments, the
maximum size, the expiry and the review date, and `ChallengerRun.active_at`
is computed from the clock rather than stored — an approval that quietly
outlives its expiry is the failure this prevents. An expired restricted
deployment is not a deployment.

Nothing here can hide a failure
--------------------------------
`GateLedger` appends. `StageResult` has no mutator, `reject()` is terminal
under the same challenger id, and there is no delete. `concealment_check()`
walks the ledger and reports any challenger whose recorded stages do not form
the prefix of `STAGE_ORDER` — which is what a removed failure would look like.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError

#: Bumped when the stage list or its ordering changes.
PROMOTION_SCHEMA_VERSION = 1


class GateStage(str, Enum):
    """The twelve, in the order they must be passed."""

    STATIC_VALIDATION = "static_validation"
    UNIT_TESTS = "unit_tests"
    LEAKAGE_TESTS = "leakage_tests"
    HISTORICAL_EVALUATION = "historical_evaluation"
    WALK_FORWARD_EVALUATION = "walk_forward_evaluation"
    BASELINE_COMPARISON = "baseline_comparison"
    ROBUSTNESS_TESTS = "robustness_tests"
    CALIBRATION_TESTS = "calibration_tests"
    PAPER_TRADING = "paper_trading"
    SHADOW_MODE = "shadow_mode"
    HUMAN_REVIEW = "human_review"
    RESTRICTED_PROMOTION = "restricted_promotion"


STAGE_ORDER: tuple[GateStage, ...] = (
    GateStage.STATIC_VALIDATION, GateStage.UNIT_TESTS,
    GateStage.LEAKAGE_TESTS, GateStage.HISTORICAL_EVALUATION,
    GateStage.WALK_FORWARD_EVALUATION, GateStage.BASELINE_COMPARISON,
    GateStage.ROBUSTNESS_TESTS, GateStage.CALIBRATION_TESTS,
    GateStage.PAPER_TRADING, GateStage.SHADOW_MODE,
    GateStage.HUMAN_REVIEW, GateStage.RESTRICTED_PROMOTION,
)

_RANK: Mapping[GateStage, int] = {stage: i for i, stage in enumerate(STAGE_ORDER)}

#: Stages Olympus may run and record by itself. Running them is analysis;
#: passing them is not permission.
AUTONOMOUS_STAGES: frozenset[GateStage] = frozenset(STAGE_ORDER[:10])

#: Stages requiring a named operator carrying a token.
HUMAN_STAGES: frozenset[GateStage] = frozenset(STAGE_ORDER[10:])

#: What each stage establishes, and what it does not. Data so a reader can
#: check the gate's intent against its behaviour without reading the code.
STAGE_NOTES: Mapping[GateStage, str] = {
    GateStage.STATIC_VALIDATION:
        "the challenger's modules parse, declare no forbidden import, and "
        "carry no reference to a foreign checkpoint",
    GateStage.UNIT_TESTS:
        "the challenger's own tests pass, including the adversarial ones",
    GateStage.LEAKAGE_TESTS:
        "an independent audit finds no future information in the training "
        "split; this is the stage a strong historical result most often fails",
    GateStage.HISTORICAL_EVALUATION:
        "measured over a held-out period on the same dataset, cost model and "
        "metric set as the incumbent",
    GateStage.WALK_FORWARD_EVALUATION:
        "measured over rolling refits, because a single split rewards a lucky "
        "period",
    GateStage.BASELINE_COMPARISON:
        "beats persistence and the statistical baselines after costs; a "
        "challenger that beats the champion and loses to persistence has told "
        "us about the champion, not about itself",
    GateStage.ROBUSTNESS_TESTS:
        "degrades explicitly under the thirteen adversarial conditions",
    GateStage.CALIBRATION_TESTS:
        "realised coverage is within tolerance of nominal, by regime and not "
        "only overall",
    GateStage.PAPER_TRADING:
        "traded against a broker with no real money, so execution assumptions "
        "meet an order book",
    GateStage.SHADOW_MODE:
        "run alongside the incumbent on live inputs, deciding nothing",
    GateStage.HUMAN_REVIEW:
        "a named operator has read the evidence and signed",
    GateStage.RESTRICTED_PROMOTION:
        "live on a named subset, under a size cap, with an expiry — not "
        "'in production'",
}


class GateOutcome(str, Enum):
    IN_PROGRESS = "in_progress"
    #: Terminal. There is no path back to IN_PROGRESS under the same id.
    REJECTED = "rejected"
    PROMOTED = "promoted"
    #: Passed everything Olympus may run alone and is waiting for a human.
    AWAITING_REVIEW = "awaiting_review"


def next_stage(current: GateStage | None) -> GateStage | None:
    """The one stage that may come next. `None` past the end."""
    if current is None:
        return STAGE_ORDER[0]
    index = _RANK[GateStage(current)] + 1
    return STAGE_ORDER[index] if index < len(STAGE_ORDER) else None


@dataclass(frozen=True)
class StageResult:
    """One stage, run once. No mutator, so a recorded failure stays recorded."""

    stage: GateStage
    passed: bool
    at: datetime
    #: What was measured. Required — a stage that passed with no evidence is a
    #: stage nobody ran.
    evidence: Mapping[str, Any]
    #: Who ran it. An autonomous stage says "olympus"; a human stage names a
    #: person, and `GateLedger` refuses the human stages without one.
    actor: str = "olympus"
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "stage", GateStage(self.stage))
        object.__setattr__(self, "at", ensure_utc(self.at, field_name="at"))
        object.__setattr__(self, "evidence", dict(self.evidence))
        if not self.evidence:
            raise ConfigurationError(
                "a stage result must carry the evidence it rests on; a pass "
                "with nothing behind it is a stage nobody ran",
                stage=self.stage.value)
        if not str(self.actor).strip():
            raise ConfigurationError("a stage result must name who ran it",
                                     stage=self.stage.value)

    def to_dict(self) -> dict:
        return {"stage": self.stage.value, "passed": self.passed,
                "at": jsonable(self.at), "evidence": dict(self.evidence),
                "actor": self.actor, "notes": self.notes,
                "establishes": STAGE_NOTES[self.stage]}


@dataclass(frozen=True)
class Restriction:
    """The shape of a restricted promotion. Every field a real bound."""

    instruments: tuple[str, ...]
    max_notional: float
    expires_at: datetime
    review_at: datetime
    #: What must be true for the restriction to widen. Required, because a
    #: restriction with no exit is one nobody will ever revisit.
    widening_criteria: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "instruments", tuple(self.instruments))
        object.__setattr__(self, "widening_criteria",
                           tuple(self.widening_criteria))
        for name in ("expires_at", "review_at"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if not self.instruments:
            raise ConfigurationError(
                "a restricted promotion must name its instruments; "
                "'restricted to everything' is not a restriction")
        if float(self.max_notional) <= 0:
            raise ConfigurationError("max_notional must be positive")
        if self.review_at > self.expires_at:
            raise ConfigurationError(
                "a review scheduled after expiry is a review that never "
                "happens", review_at=self.review_at.isoformat(),
                expires_at=self.expires_at.isoformat())
        if not self.widening_criteria:
            raise ConfigurationError(
                "a restriction must state what would justify widening it; "
                "without that it is a restriction nobody will revisit")

    def covers(self, instrument_key: str) -> bool:
        return instrument_key in self.instruments

    def active_at(self, when: datetime) -> bool:
        return ensure_utc(when, field_name="when") < self.expires_at

    def to_dict(self) -> dict:
        return {"instruments": list(self.instruments),
                "max_notional": self.max_notional,
                "expires_at": jsonable(self.expires_at),
                "review_at": jsonable(self.review_at),
                "widening_criteria": list(self.widening_criteria)}


@dataclass(frozen=True)
class ChallengerRun:
    """One challenger's passage through the gate. Rebuilt, never mutated."""

    challenger_id: str
    proposal_id: str
    created_at: datetime
    results: tuple[StageResult, ...] = ()
    restriction: Restriction | None = None
    #: Set when rejected. Terminal.
    rejection_reason: str = ""
    schema_version: int = PROMOTION_SCHEMA_VERSION

    def __post_init__(self):
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "results", tuple(self.results))
        if not str(self.challenger_id).strip():
            raise ConfigurationError("a challenger run must be identified")
        expected = list(STAGE_ORDER)[:len(self.results)]
        actual = [r.stage for r in self.results]
        if actual != expected:
            raise ConfigurationError(
                "recorded stages are not the prefix of the gate order; a run "
                "that skipped a stage — or lost one — is not a run through "
                "this gate", challenger_id=self.challenger_id,
                expected=[s.value for s in expected],
                actual=[s.value for s in actual])

    @property
    def passed_stages(self) -> tuple[GateStage, ...]:
        return tuple(r.stage for r in self.results if r.passed)

    @property
    def failed_at(self) -> StageResult | None:
        return next((r for r in self.results if not r.passed), None)

    @property
    def furthest(self) -> GateStage | None:
        return self.results[-1].stage if self.results else None

    @property
    def next_stage(self) -> GateStage | None:
        """`None` when finished or when a stage has already failed."""
        if self.failed_at is not None:
            return None
        return next_stage(self.furthest)

    @property
    def outcome(self) -> GateOutcome:
        """Computed from the results. There is no field that sets it."""
        if self.rejection_reason or self.failed_at is not None:
            return GateOutcome.REJECTED
        if (self.furthest is GateStage.RESTRICTED_PROMOTION
                and self.restriction is not None):
            return GateOutcome.PROMOTED
        if self.next_stage in HUMAN_STAGES:
            return GateOutcome.AWAITING_REVIEW
        return GateOutcome.IN_PROGRESS

    @property
    def terminal(self) -> bool:
        return self.outcome in (GateOutcome.REJECTED, GateOutcome.PROMOTED)

    def active_at(self, when: datetime) -> bool:
        """Promoted, restricted and not expired. All three, computed."""
        return (self.outcome is GateOutcome.PROMOTED
                and self.restriction is not None
                and self.restriction.active_at(when))

    def evidence_for(self, stage: GateStage | str) -> Mapping[str, Any]:
        wanted = GateStage(stage)
        for result in self.results:
            if result.stage is wanted:
                return dict(result.evidence)
        return {}

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "challenger_id": self.challenger_id,
                "proposal_id": self.proposal_id,
                "created_at": jsonable(self.created_at),
                "results": [r.to_dict() for r in self.results],
                "passed": [s.value for s in self.passed_stages],
                "failed_at": (self.failed_at.stage.value
                              if self.failed_at else None),
                "furthest": self.furthest.value if self.furthest else None,
                "next_stage": (self.next_stage.value
                               if self.next_stage else None),
                "outcome": self.outcome.value,
                "terminal": self.terminal,
                "rejection_reason": self.rejection_reason,
                "restriction": (self.restriction.to_dict()
                                if self.restriction else None)}


class PromotionRefused(ConfigurationError):
    """Raised when the gate declines to advance or promote."""


class GateLedger:
    """Every challenger's passage, appended. Nothing here removes a result.

    In-memory and explicit for the same reason `EvidenceJournal` is: an
    experiment must be able to hold one without holding a shared store.
    `to_dict` is how it is persisted by a caller who has somewhere to put it.
    """

    __slots__ = ("_runs", "_ledger", "_audit")

    def __init__(self, *, ledger: Any = None, audit: Any = None):
        self._runs: dict[str, ChallengerRun] = {}
        #: Optional `evolution.EvolutionLedger`, so a gate decision joins the
        #: same record as everything else Olympus does.
        self._ledger = ledger
        self._audit = audit

    # -- reading -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._runs)

    def __iter__(self) -> Iterator[ChallengerRun]:
        return iter(self._runs.values())

    def __contains__(self, challenger_id: object) -> bool:
        return challenger_id in self._runs

    def get(self, challenger_id: str) -> ChallengerRun | None:
        return self._runs.get(challenger_id)

    def __getitem__(self, challenger_id: str) -> ChallengerRun:
        run = self._runs.get(challenger_id)
        if run is None:
            raise KeyError(challenger_id)
        return run

    @property
    def promoted(self) -> tuple[str, ...]:
        return tuple(sorted(r.challenger_id for r in self
                            if r.outcome is GateOutcome.PROMOTED))

    @property
    def rejected(self) -> tuple[str, ...]:
        return tuple(sorted(r.challenger_id for r in self
                            if r.outcome is GateOutcome.REJECTED))

    @property
    def awaiting_review(self) -> tuple[str, ...]:
        return tuple(sorted(r.challenger_id for r in self
                            if r.outcome is GateOutcome.AWAITING_REVIEW))

    # -- writing -----------------------------------------------------------

    def enter(self, *, challenger_id: str, proposal_id: str,
              created_at: datetime) -> ChallengerRun:
        if challenger_id in self._runs:
            raise PromotionRefused(
                "this challenger is already in the gate; re-entering would "
                "give it a second first attempt",
                challenger_id=challenger_id)
        run = ChallengerRun(challenger_id=challenger_id,
                            proposal_id=proposal_id, created_at=created_at)
        self._runs[challenger_id] = run
        return run

    def advance(self, challenger_id: str, result: StageResult, *,
                actor: Any = None) -> ChallengerRun:
        """Record one stage. Only the next one, and only by a permitted actor.

        A human stage requires a real operator: `governance.authorise` is
        consulted for the corresponding action and raises for anything else.
        The check is on the actor's kind, and an autonomous engine has no route
        to becoming an operator.
        """
        run = self[challenger_id]
        if run.terminal:
            raise PromotionRefused(
                "this challenger is finished; a terminal outcome does not "
                "reopen", challenger_id=challenger_id,
                outcome=run.outcome.value)
        expected = run.next_stage
        if expected is None:
            raise PromotionRefused(
                "there is no next stage for this challenger",
                challenger_id=challenger_id, furthest=run.furthest.value)
        if result.stage is not expected:
            raise PromotionRefused(
                "stages are passed in order; skipping one is how a leakage "
                "test gets dropped under time pressure",
                challenger_id=challenger_id, expected=expected.value,
                got=result.stage.value)
        if result.stage in HUMAN_STAGES:
            self._require_operator(result.stage, actor)

        advanced = ChallengerRun(
            challenger_id=run.challenger_id, proposal_id=run.proposal_id,
            created_at=run.created_at, results=run.results + (result,),
            restriction=run.restriction)
        self._runs[challenger_id] = advanced
        self._log("stage", advanced, result)
        return advanced

    def reject(self, challenger_id: str, *, reason: str,
               at: datetime) -> ChallengerRun:
        """Autonomous, and terminal. Rejecting is always Olympus's to do."""
        run = self[challenger_id]
        if not str(reason).strip():
            raise ConfigurationError("a rejection must state its reason",
                                     challenger_id=challenger_id)
        if run.outcome is GateOutcome.PROMOTED:
            raise PromotionRefused(
                "a promoted challenger is withdrawn by rollback, not by "
                "rejection; rejecting it would leave a live deployment with a "
                "rejected record", challenger_id=challenger_id)
        rejected = ChallengerRun(
            challenger_id=run.challenger_id, proposal_id=run.proposal_id,
            created_at=run.created_at, results=run.results,
            restriction=run.restriction, rejection_reason=reason)
        self._runs[challenger_id] = rejected
        self._log("reject", rejected, None, extra={"reason": reason})
        return rejected

    def promote(self, challenger_id: str, *, actor: Any,
                restriction: Restriction, at: datetime,
                evidence: Mapping[str, Any]) -> ChallengerRun:
        """Human-only. The first act is `governance.authorise`.

        There is no `force` and no autonomous path. Everything up to stage 10
        is Olympus's; this is not.
        """
        run = self[challenger_id]
        if run.next_stage is not GateStage.RESTRICTED_PROMOTION:
            raise PromotionRefused(
                "a challenger is promoted only from the stage before it, and "
                "only after human review",
                challenger_id=challenger_id,
                next_stage=run.next_stage.value if run.next_stage else None)
        self._require_operator(GateStage.RESTRICTED_PROMOTION, actor)
        result = StageResult(
            stage=GateStage.RESTRICTED_PROMOTION, passed=True, at=at,
            evidence={**dict(evidence),
                      "restriction": restriction.to_dict()},
            actor=getattr(actor, "name", str(actor)))
        promoted = ChallengerRun(
            challenger_id=run.challenger_id, proposal_id=run.proposal_id,
            created_at=run.created_at, results=run.results + (result,),
            restriction=restriction)
        self._runs[challenger_id] = promoted
        self._log("promote", promoted, result)
        return promoted

    # -- governance --------------------------------------------------------

    @staticmethod
    def _require_operator(stage: GateStage, actor: Any) -> Any:
        from ..governance import Action, authorise
        action = (Action.PROMOTE_TO_LIVE
                  if stage is GateStage.RESTRICTED_PROMOTION
                  else Action.PROMOTE_CAPABILITY)
        if actor is None:
            raise PromotionRefused(
                "this stage requires a named operator carrying a token",
                stage=stage.value)
        authorise(action, actor, subject=f"gate stage {stage.value}")
        return actor

    def _log(self, kind: str, run: ChallengerRun, result: StageResult | None,
             extra: Mapping[str, Any] | None = None) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                event=f"native.promotion.{kind}",
                subject=run.challenger_id,
                detail={**(dict(extra) if extra else {}),
                        "outcome": run.outcome.value,
                        "stage": result.stage.value if result else None,
                        "passed": result.passed if result else None})
        except Exception:                                # noqa: BLE001
            pass          # an audit sink that fails must not stop the gate

    # -- reporting ---------------------------------------------------------

    def concealment_check(self) -> tuple[str, ...]:
        """Any run whose stage record is not a prefix of the gate order.

        What a removed failure looks like. `ChallengerRun` already refuses to
        construct that way, so this is a second, independent reading of the
        stored objects — a check that only ran at construction would not catch
        a ledger reconstructed from tampered data.
        """
        offenders = []
        for run in self:
            stages = [r.stage for r in run.results]
            if stages != list(STAGE_ORDER)[:len(stages)]:
                offenders.append(
                    f"{run.challenger_id}: stages {[s.value for s in stages]} "
                    f"are not a prefix of the gate order")
            failures = [r for r in run.results if not r.passed]
            if len(failures) > 1:
                offenders.append(
                    f"{run.challenger_id}: {len(failures)} failed stages "
                    f"recorded, but a failure is terminal")
            if failures and run.outcome is not GateOutcome.REJECTED:
                offenders.append(
                    f"{run.challenger_id}: failed at "
                    f"{failures[0].stage.value} and is not rejected")
        return tuple(offenders)

    def report(self) -> dict:
        return {"schema_version": PROMOTION_SCHEMA_VERSION,
                "stages": [s.value for s in STAGE_ORDER],
                "autonomous_stages": sorted(s.value for s in AUTONOMOUS_STAGES),
                "human_stages": sorted(s.value for s in HUMAN_STAGES),
                "runs": {r.challenger_id: r.to_dict() for r in self},
                "promoted": list(self.promoted),
                "rejected": list(self.rejected),
                "awaiting_review": list(self.awaiting_review),
                "concealment_findings": list(self.concealment_check())}

    def table(self) -> str:                              # pragma: no cover
        header = f"{'challenger':<24}{'furthest':>26}{'outcome':>18}"
        lines = [header, "-" * len(header)]
        for run in sorted(self, key=lambda r: r.challenger_id):
            lines.append(
                f"{run.challenger_id:<24}"
                f"{(run.furthest.value if run.furthest else '-'):>26}"
                f"{run.outcome.value:>18}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# running the autonomous stages
# ---------------------------------------------------------------------------

def stage_id(challenger_id: str, stage: GateStage) -> str:
    return hashlib.sha256(
        f"{challenger_id}|{stage.value}".encode("utf-8")).hexdigest()[:12]


def run_autonomous_stages(ledger: GateLedger, challenger_id: str, *,
                          checks: Mapping[GateStage, Any],
                          at: datetime,
                          stop_on_failure: bool = True) -> ChallengerRun:
    """Run stages 1–10 in order, stopping at the first failure.

    Each entry in `checks` is a callable returning `(passed, evidence)`. A
    stage with no check **fails**, with the reason recorded — the alternative
    is that a missing check reads as a pass, which is how a gate becomes a
    formality.
    """
    run = ledger[challenger_id]
    for stage in STAGE_ORDER:
        if stage in HUMAN_STAGES:
            break
        if run.terminal or run.next_stage is not stage:
            continue
        check = checks.get(stage)
        if check is None:
            passed, evidence = False, {
                "reason": f"no check was supplied for {stage.value}; a stage "
                          f"nobody ran is not a stage that passed"}
        else:
            passed, evidence = check()
        run = ledger.advance(challenger_id, StageResult(
            stage=stage, passed=bool(passed), at=at, evidence=evidence))
        if not passed:
            if stop_on_failure:
                run = ledger.reject(
                    challenger_id,
                    reason=f"failed {stage.value}: "
                           f"{evidence.get('reason', 'see evidence')}",
                    at=at)
            break
    return run


def default_restriction(*, instruments: Sequence[str], max_notional: float,
                        at: datetime, days: int = 30) -> Restriction:
    """A conservative default: one month, one review at the halfway point."""
    moment = ensure_utc(at, field_name="at")
    return Restriction(
        instruments=tuple(instruments), max_notional=float(max_notional),
        expires_at=moment + timedelta(days=days),
        review_at=moment + timedelta(days=max(1, days // 2)),
        widening_criteria=(
            "the restricted period completes with no rollback trigger firing",
            "realised coverage stays within tolerance of nominal in every "
            "regime with a usable sample",
            "net return after costs is positive in the restricted set",
        ))


def describe_gate() -> str:                              # pragma: no cover
    lines = ["The twelve-stage gate", "=" * 21, ""]
    for index, stage in enumerate(STAGE_ORDER, start=1):
        who = "human" if stage in HUMAN_STAGES else "olympus"
        lines.append(f"{index:>2}. [{who:<7}] {stage.value}")
        lines.append(f"      {STAGE_NOTES[stage]}")
    return "\n".join(lines)


__all__ = ["PROMOTION_SCHEMA_VERSION", "GateStage", "STAGE_ORDER",
           "AUTONOMOUS_STAGES", "HUMAN_STAGES", "STAGE_NOTES", "GateOutcome",
           "next_stage", "StageResult", "Restriction", "ChallengerRun",
           "PromotionRefused", "GateLedger", "stage_id",
           "run_autonomous_stages", "default_restriction", "describe_gate"]
