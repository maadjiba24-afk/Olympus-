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

Positive decisions fail closed; negative ones fail safe
-------------------------------------------------------
The pre-merge audit found this module logging its audit events through a bare
`except Exception: pass`, with the comment *"an audit sink that fails must not
stop the gate"*. That reasoning is right in one direction and inverted in the
other, so the two directions are now separated:

* **Human review, restricted promotion** — any decision that *expands*
  permission — commits through `durable.py` in a fixed order: verify the
  operator and token, validate every piece of evidence and the restriction,
  write and verify the immutable audit event, write and verify the durable
  state, and only then update the in-memory ledger. A failure at any step
  raises and the challenger stays exactly where it was. **There is no
  permission expansion without a durable record of who granted it.**
* **Rejection, restriction, demotion, rollback, emergency shutdown** — anything
  that *reduces* permission — still proceeds when the sink is unavailable,
  because refusing to stop a deteriorating model because the audit disk is full
  is the wrong way to fail. The missing record is reported rather than
  swallowed: `unrecorded_safety_actions` lists them.

The two durable writes cannot be made atomic together, so the order is chosen
so the surviving half is the safe one — audit first, state second. A crash
between them leaves evidence that a promotion was attempted and no state saying
it happened, and `reconstruct()` reads the state. See `durable.py`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterator, Mapping, Sequence

from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError
from .durable import AuditLog, DurabilityError, StateStore, TamperedRecord

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


#: Fields a promotion's evidence must carry before an operator can grant it.
#: Not a formality: each one is a question a reviewer must have answered, and a
#: promotion whose evidence cannot say which build was reviewed is a promotion
#: of something nobody can identify afterwards.
REQUIRED_PROMOTION_EVIDENCE: tuple[str, ...] = (
    "model_version", "evaluation_report", "reviewed_by", "review_notes",
    "rollback_plan",
)


def _validate_promotion_evidence(evidence: Mapping[str, Any],
                                 *, challenger_id: str) -> dict:
    """Step 2 of the commit. Raises rather than filling in a default.

    A default here would be a reviewer's answer invented by the code, which is
    the one kind of missing evidence that reads as present.
    """
    payload = dict(evidence or {})
    missing = [name for name in REQUIRED_PROMOTION_EVIDENCE
               if not str(payload.get(name, "")).strip()]
    if missing:
        raise PromotionRefused(
            "a restricted promotion must carry every piece of review "
            "evidence; a promotion that cannot say what was reviewed is not "
            "reviewable afterwards",
            challenger_id=challenger_id, missing=missing,
            required=list(REQUIRED_PROMOTION_EVIDENCE))
    return payload


class GateLedger:
    """Every challenger's passage, appended. Nothing here removes a result.

    In-memory and explicit for the same reason `EvidenceJournal` is: an
    experiment must be able to hold one without holding a shared store.
    `to_dict` is how it is persisted by a caller who has somewhere to put it.
    """

    __slots__ = ("_runs", "_ledger", "_audit", "_log_store", "_state",
                 "_unrecorded", "allow_volatile_promotion")

    def __init__(self, *, ledger: Any = None, audit: Any = None,
                 audit_log: AuditLog | None = None,
                 state: StateStore | None = None,
                 allow_volatile_promotion: bool = False):
        self._runs: dict[str, ChallengerRun] = {}
        #: Optional `evolution.EvolutionLedger`, so a gate decision joins the
        #: same record as everything else Olympus does.
        self._ledger = ledger
        #: The legacy best-effort sink, used for *negative* actions only.
        self._audit = audit
        #: The durable, hash-chained record every positive decision goes
        #: through. In-memory when no path was given, which is what
        #: `allow_volatile_promotion` exists to make an explicit choice.
        self._log_store = audit_log if audit_log is not None else AuditLog()
        self._state = state if state is not None else StateStore()
        #: Safety actions whose record could not be written. Reported, never
        #: swallowed — the action still happened, and a reader is entitled to
        #: know the audit is incomplete.
        self._unrecorded: list[dict] = []
        #: Promoting through an in-memory audit log is a test and demo
        #: affordance. Off by default, so a production ledger constructed
        #: without a path refuses to promote rather than promoting into RAM.
        self.allow_volatile_promotion = bool(allow_volatile_promotion)

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
        advanced = ChallengerRun(
            challenger_id=run.challenger_id, proposal_id=run.proposal_id,
            created_at=run.created_at, results=run.results + (result,),
            restriction=run.restriction)

        if result.stage in HUMAN_STAGES:
            # Positive: a human stage expands what the challenger may do next,
            # so it commits durably or it does not happen. Note the ordering —
            # the operator check runs inside `_commit_positive` as its first
            # step, before anything is written anywhere.
            return self._commit_positive(
                kind="stage", stage=result.stage, actor=actor, run=advanced,
                result=result, at=result.at,
                detail={"stage": result.stage.value, "passed": result.passed,
                        "evidence": jsonable(dict(result.evidence))})

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
        """Human-only, and fail-closed. Five steps, in this order:

        1. verify the named operator and the token they are carrying;
        2. validate every piece of promotion evidence and the restriction;
        3. write **and verify** the immutable audit event;
        4. write **and verify** the durable promotion state;
        5. return only once both durable records have succeeded.

        A failure at any step raises, and the challenger stays unpromoted. It
        is not "promoted but unrecorded", which is the state the pre-merge
        audit found reachable and which nothing afterwards can distinguish from
        a promotion nobody authorised.

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
        payload = _validate_promotion_evidence(evidence,
                                               challenger_id=challenger_id)
        if not isinstance(restriction, Restriction):
            raise PromotionRefused(
                "a restricted promotion requires a Restriction; without one "
                "there is no instrument list, no size cap and no expiry",
                challenger_id=challenger_id, got=type(restriction).__name__)
        if not restriction.active_at(ensure_utc(at, field_name="at")):
            raise PromotionRefused(
                "the restriction has already expired at the moment of "
                "promotion, so this would promote nothing that could trade",
                challenger_id=challenger_id, at=jsonable(at),
                expires_at=jsonable(restriction.expires_at))
        result = StageResult(
            stage=GateStage.RESTRICTED_PROMOTION, passed=True, at=at,
            evidence={**payload, "restriction": restriction.to_dict()},
            actor=getattr(actor, "name", str(actor)))
        promoted = ChallengerRun(
            challenger_id=run.challenger_id, proposal_id=run.proposal_id,
            created_at=run.created_at, results=run.results + (result,),
            restriction=restriction)
        return self._commit_positive(
            kind="promote", stage=GateStage.RESTRICTED_PROMOTION, actor=actor,
            run=promoted, result=result, at=at,
            detail={"restriction": restriction.to_dict(),
                    "evidence": jsonable(payload)})

    # -- the fail-closed commit -------------------------------------------

    def _commit_positive(self, *, kind: str, stage: GateStage, actor: Any,
                         run: ChallengerRun, result: StageResult,
                         at: datetime, detail: Mapping[str, Any]
                         ) -> ChallengerRun:
        """The only way a permission expansion reaches the ledger.

        Every step raises on failure and nothing is written to `self._runs`
        until both durable records exist. The ordering is the guarantee, so it
        is written out rather than implied by the call sites.
        """
        # 1. the operator and their token.
        operator = self._require_operator(stage, actor)
        name = getattr(operator, "name", str(operator))

        # 2. the durable channel itself. An in-memory audit log records
        #    nothing that survives the process, so promoting through one is a
        #    decision a caller makes explicitly or not at all.
        if not (self._log_store.durable and self._state.durable) \
                and not self.allow_volatile_promotion:
            raise DurabilityError(
                "this gate has no durable audit log or state store, so a "
                "promotion through it would leave no record after the process "
                "exits; construct GateLedger with audit_log= and state= paths, "
                "or pass allow_volatile_promotion=True to say the volatility "
                "is intended",
                challenger_id=run.challenger_id, stage=stage.value,
                audit_durable=self._log_store.durable,
                state_durable=self._state.durable)

        # 3. the immutable audit event, written and read back. First, because
        #    a crash after this and before step 4 must leave evidence of the
        #    attempt and no permission granted.
        event = self._log_store.commit(
            event=f"native.promotion.{kind}", subject=run.challenger_id,
            at=at, actor=name,
            detail={**dict(detail), "stage": stage.value,
                    "outcome": run.outcome.value,
                    "proposal_id": run.proposal_id})

        # 4. the durable state, written and read back.
        snapshot = self._state.load()
        snapshot[run.challenger_id] = {**run.to_dict(),
                                       "audit_digest": event.digest,
                                       "committed_at": jsonable(at)}
        self._state.save(snapshot)

        # 5. only now does the in-memory ledger agree.
        self._runs[run.challenger_id] = run
        return run

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

    @property
    def unrecorded_safety_actions(self) -> tuple[dict, ...]:
        """Safety actions whose audit record could not be written.

        Empty in normal operation. Non-empty means the gate stopped something
        and the audit does not know — which is the right way round to fail, and
        still a thing an operator needs told.
        """
        return tuple(self._unrecorded)

    def _log(self, kind: str, run: ChallengerRun, result: StageResult | None,
             extra: Mapping[str, Any] | None = None) -> None:
        """Best-effort recording for actions that *reduce* permission.

        Still swallows the failure, and that is deliberate: refusing to reject a
        deteriorating challenger because the audit disk is full would leave it
        running. Unlike the version this replaced, the swallowed failure is
        recorded in `unrecorded_safety_actions` instead of vanishing — and
        nothing that expands permission comes through here.
        """
        detail = {**(dict(extra) if extra else {}),
                  "outcome": run.outcome.value,
                  "stage": result.stage.value if result else None,
                  "passed": result.passed if result else None}
        failures: list[str] = []
        try:
            self._log_store.commit(
                event=f"native.promotion.{kind}", subject=run.challenger_id,
                at=result.at if result else run.created_at, actor="olympus",
                detail=detail)
        except (DurabilityError, TamperedRecord, OSError) as error:
            failures.append(f"durable log: {type(error).__name__}: {error}")
        if self._audit is not None:
            try:
                self._audit.record(event=f"native.promotion.{kind}",
                                   subject=run.challenger_id, detail=detail)
            except Exception as error:                   # noqa: BLE001
                failures.append(f"audit sink: {type(error).__name__}: {error}")
        if failures:
            self._unrecorded.append({"kind": kind,
                                     "challenger_id": run.challenger_id,
                                     "failures": failures, "detail": detail})

    # -- restart -----------------------------------------------------------

    def reconstruct(self) -> tuple[str, ...]:
        """Rebuild promoted state after a restart. The state file is the truth.

        The audit log says what was *attempted*; the state file says what was
        *granted*. A challenger with an audit event and no state entry — the
        crash-between-writes window — comes back unpromoted, and is reported
        here so the gap is visible rather than merely safe.
        """
        self._log_store.require_intact()
        state = self._state.load()
        findings: list[str] = []
        for challenger_id, stored in sorted(state.items()):
            events = self._log_store.for_subject(challenger_id)
            digest = str(stored.get("audit_digest", ""))
            if digest and not any(e.digest == digest for e in events):
                findings.append(
                    f"{challenger_id}: the promotion state names audit event "
                    f"{digest[:12]}, which is not in the log")
        # The other direction: an audit event saying a promotion was granted,
        # with no state entry pointing back at it. That is the crash window —
        # the promotion did not take effect, and saying so is more useful than
        # merely being safe about it.
        latest: dict[str, Any] = {}
        for event in self._log_store.events:
            if event.event == "native.promotion.promote":
                latest[event.subject] = event
        for subject, event in sorted(latest.items()):
            stored = state.get(subject)
            if stored is None or str(stored.get("audit_digest", "")) != event.digest:
                findings.append(
                    f"{subject}: audit event {event.digest[:12]} records a "
                    f"promotion attempt with no durable state naming it, so "
                    f"it is not promoted")
        return tuple(findings)

    @property
    def durable_state(self) -> dict:
        return self._state.load()

    @property
    def audit(self) -> AuditLog:
        return self._log_store

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
           "PromotionRefused", "REQUIRED_PROMOTION_EVIDENCE", "GateLedger",
           "AuditLog", "StateStore", "DurabilityError", "TamperedRecord",
           "stage_id", "run_autonomous_stages", "default_restriction",
           "describe_gate"]
