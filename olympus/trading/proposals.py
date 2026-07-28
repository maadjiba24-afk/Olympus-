"""What Olympus wants built, written down so a human can decide.

Two kinds of proposal live here, and they share one property: neither can be
applied by anything in this package.

**`FeatureProposal`** covers both meanings of "feature" — a market feature (a
new indicator, a liquidity measure, a calendar effect) and a system capability
(a broker integration, better reconciliation, a new report). Ten fields are
required and none may be empty, including `security_implications` and
`rollback_plan`. A proposal that cannot say how to undo itself is not ready to
be considered, and the constructor says so rather than leaving it to a reviewer
to notice.

**`CodeChangeProposal`** carries an actual patch, actual tests, and an expected
impact. Every file it touches passes through `kernel.assert_not_kernel`, so a
proposal cannot name `risk.py`, `killswitch.py`, `modes.py` or the vault at
all — it raises at construction, before review, before CI, before anyone can be
persuaded that this particular limit change is fine.

The absence that matters
------------------------
There is no `apply()`, `merge()`, `deploy()` or `commit()` in this module, and
`test_trading_proposals.py` asserts it by parsing the AST rather than by
grepping — so a future refactor that adds one fails the build. The furthest a
proposal goes autonomously is `mark_ci_result`: Olympus may run its own change
in isolation and record what happened. Turning that into a merge is a human
act, performed with human tools, outside this system.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, SafetyKernelViolation
from .governance import Action, Actor, authorise
from .kernel import assert_not_kernel, path_to_module
from .storekeys import KeyedStore

_NS = "trading.proposals"


class FeatureKind(str, Enum):
    """The two meanings of "feature", kept apart because they review differently."""

    # -- market features -----------------------------------------------------
    INDICATOR = "indicator"
    VOLATILITY_MEASURE = "volatility_measure"
    LIQUIDITY_MEASURE = "liquidity_measure"
    REGIME_INDICATOR = "regime_indicator"
    CROSS_ASSET_SIGNAL = "cross_asset_signal"
    SENTIMENT_VARIABLE = "sentiment_variable"
    FUNDAMENTAL_VARIABLE = "fundamental_variable"
    CALENDAR_EFFECT = "calendar_effect"
    EXECUTION_QUALITY_FEATURE = "execution_quality_feature"
    # -- product and system capabilities -------------------------------------
    BROKER_INTEGRATION = "broker_integration"
    DATA_PROVIDER = "data_provider"
    MONITORING = "monitoring"
    RECONCILIATION = "reconciliation"
    VISUALISATION = "visualisation"
    INCIDENT_DETECTION = "incident_detection"
    RESEARCH_TOOL = "research_tool"
    EVALUATION_REPORT = "evaluation_report"
    OPERATOR_CONTROL = "operator_control"
    NEW_MARKET = "new_market"


MARKET_FEATURE_KINDS: frozenset[FeatureKind] = frozenset({
    FeatureKind.INDICATOR, FeatureKind.VOLATILITY_MEASURE,
    FeatureKind.LIQUIDITY_MEASURE, FeatureKind.REGIME_INDICATOR,
    FeatureKind.CROSS_ASSET_SIGNAL, FeatureKind.SENTIMENT_VARIABLE,
    FeatureKind.FUNDAMENTAL_VARIABLE, FeatureKind.CALENDAR_EFFECT,
    FeatureKind.EXECUTION_QUALITY_FEATURE,
})


class ReviewState(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class SecurityImpact(str, Enum):
    NONE = "none"
    #: Reads external content. Everything in this class inherits the untrusted
    #: treatment from `knowledge.py` and `sentiment.py`.
    CONSUMES_EXTERNAL_CONTENT = "consumes_external_content"
    NEW_NETWORK_EGRESS = "new_network_egress"
    NEW_DEPENDENCY = "new_dependency"
    HANDLES_CREDENTIALS = "handles_credentials"
    #: Touches the safety kernel. Recordable as a recommendation; never
    #: implementable through this system.
    TOUCHES_SAFETY_KERNEL = "touches_safety_kernel"


def _require_text(value: Any, field_name: str, subject: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ConfigurationError(
            f"{field_name} is required; a proposal missing it cannot be "
            "reviewed on its merits", proposal=subject, field=field_name)
    return text


@dataclass(frozen=True)
class FeatureProposal:
    """A proposed capability, with everything a reviewer needs and nothing less."""

    proposal_id: str
    title: str
    kind: FeatureKind
    created_at: datetime
    problem: str
    expected_value: str
    implementation_plan: str
    test_plan: str
    rollback_plan: str
    acceptance_criteria: tuple[str, ...]
    supporting_evidence: tuple[str, ...] = ()
    required_dependencies: tuple[str, ...] = ()
    security_implications: tuple[SecurityImpact, ...] = (SecurityImpact.NONE,)
    security_notes: str = ""
    risk_implications: str = "none identified"
    state: ReviewState = ReviewState.DRAFT
    proposed_by: str = "olympus"
    reviewer: str = ""
    review_notes: str = ""
    estimated_effort: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", FeatureKind(self.kind))
        object.__setattr__(self, "state", ReviewState(self.state))
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        for name in ("acceptance_criteria", "supporting_evidence",
                     "required_dependencies"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "security_implications",
                           tuple(SecurityImpact(s) for s in self.security_implications))

        subject = self.proposal_id
        for name in ("proposal_id", "title", "problem", "expected_value",
                     "implementation_plan", "test_plan", "rollback_plan",
                     "risk_implications"):
            object.__setattr__(self, name,
                               _require_text(getattr(self, name), name, subject))
        if not self.acceptance_criteria:
            raise ConfigurationError(
                "acceptance_criteria is required; without it there is no way to "
                "tell afterwards whether the feature did what it promised",
                proposal=subject)
        if not self.security_implications:
            raise ConfigurationError(
                "security_implications must be stated, even if NONE; the "
                "absence of an answer is not the same as 'no impact'",
                proposal=subject)
        if (SecurityImpact.TOUCHES_SAFETY_KERNEL in self.security_implications
                and not self.security_notes.strip()):
            raise ConfigurationError(
                "a proposal that touches the safety kernel must explain what "
                "and why; it can only ever be a recommendation for a human",
                proposal=subject)

    @property
    def is_market_feature(self) -> bool:
        return self.kind in MARKET_FEATURE_KINDS

    @property
    def needs_security_review(self) -> bool:
        return any(s is not SecurityImpact.NONE for s in self.security_implications)

    def to_dict(self) -> dict:
        return {"proposal_id": self.proposal_id, "title": self.title,
                "kind": self.kind.value, "created_at": jsonable(self.created_at),
                "problem": self.problem, "expected_value": self.expected_value,
                "implementation_plan": self.implementation_plan,
                "test_plan": self.test_plan, "rollback_plan": self.rollback_plan,
                "acceptance_criteria": list(self.acceptance_criteria),
                "supporting_evidence": list(self.supporting_evidence),
                "required_dependencies": list(self.required_dependencies),
                "security_implications": [s.value for s in self.security_implications],
                "security_notes": self.security_notes,
                "risk_implications": self.risk_implications,
                "state": self.state.value, "proposed_by": self.proposed_by,
                "reviewer": self.reviewer, "review_notes": self.review_notes,
                "estimated_effort": self.estimated_effort,
                "is_market_feature": self.is_market_feature,
                "needs_security_review": self.needs_security_review}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FeatureProposal":
        return cls(
            proposal_id=str(data["proposal_id"]), title=str(data["title"]),
            kind=FeatureKind(data["kind"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            problem=str(data.get("problem", "")),
            expected_value=str(data.get("expected_value", "")),
            implementation_plan=str(data.get("implementation_plan", "")),
            test_plan=str(data.get("test_plan", "")),
            rollback_plan=str(data.get("rollback_plan", "")),
            acceptance_criteria=tuple(data.get("acceptance_criteria") or ()),
            supporting_evidence=tuple(data.get("supporting_evidence") or ()),
            required_dependencies=tuple(data.get("required_dependencies") or ()),
            security_implications=tuple(SecurityImpact(s) for s in
                                        data.get("security_implications") or ()),
            security_notes=str(data.get("security_notes", "")),
            risk_implications=str(data.get("risk_implications", "none identified")),
            state=ReviewState(data.get("state", ReviewState.DRAFT.value)),
            proposed_by=str(data.get("proposed_by", "olympus")),
            reviewer=str(data.get("reviewer", "")),
            review_notes=str(data.get("review_notes", "")),
            estimated_effort=str(data.get("estimated_effort", "")))

    def evolve(self, **changes) -> "FeatureProposal":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return FeatureProposal(**data)


# ---------------------------------------------------------------------------
# code changes
# ---------------------------------------------------------------------------


class DefectSeverity(str, Enum):
    COSMETIC = "cosmetic"
    MINOR = "minor"
    MAJOR = "major"
    #: Produces a wrong number silently. The Kronos teardown's whole §12.
    CORRECTNESS = "correctness"
    SECURITY = "security"


@dataclass(frozen=True)
class DefectReport:
    """A defect Olympus found in its own code. Detection is autonomous."""

    defect_id: str
    module: str
    severity: DefectSeverity
    summary: str
    detected_at: datetime
    evidence: tuple[str, ...] = ()
    reproduction: str = ""
    line: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "severity", DefectSeverity(self.severity))
        object.__setattr__(self, "detected_at",
                           ensure_utc(self.detected_at, field_name="detected_at"))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        _require_text(self.summary, "summary", self.defect_id)

    @property
    def in_safety_kernel(self) -> bool:
        """A defect *report* against the kernel is fine and important.

        Only a *change* is refused. Suppressing the report as well would mean
        Olympus could find a bug in the risk engine and be unable to say so,
        which is strictly worse than the risk of it saying so.
        """
        from .kernel import is_kernel_module
        return is_kernel_module(path_to_module(self.module))

    def to_dict(self) -> dict:
        return {"defect_id": self.defect_id, "module": self.module,
                "severity": self.severity.value, "summary": self.summary,
                "detected_at": jsonable(self.detected_at),
                "evidence": list(self.evidence),
                "reproduction": self.reproduction, "line": self.line,
                "in_safety_kernel": self.in_safety_kernel}


class CIStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"


#: Patch text that would touch things a code proposal may never touch, checked
#: on the diff body as well as the file list. A change to a non-kernel file that
#: reaches into the kernel through an import is still a kernel change.
_PATCH_RED_FLAGS = (
    (re.compile(r"(?m)^\+.*\bLimitsStore\b"), "constructs the risk LimitsStore"),
    (re.compile(r"(?m)^\+.*\.save\s*\(\s*.*limits"), "writes risk limits"),
    (re.compile(r"(?m)^\+.*\bdisengage\s*\("), "clears a kill switch"),
    (re.compile(r"(?m)^\+.*\benable_live\s*\("), "enables live trading"),
    (re.compile(r"(?m)^\+.*Mode\.LIVE"), "references live mode"),
    (re.compile(r"(?m)^\+.*\bfrom\s+olympus\s+import\s+vault"), "imports the vault"),
    (re.compile(r"(?m)^\+.*\bledger\.(truncate|rewrite)\b"), "rewrites the ledger"),
)


@dataclass(frozen=True)
class CodeChangeProposal:
    """A patch, its tests, and its expected impact. Never applied from here."""

    proposal_id: str
    title: str
    created_at: datetime
    #: Repo-relative paths. Each is checked against the safety kernel.
    target_files: tuple[str, ...]
    diff: str
    rationale: str
    expected_impact: str
    rollback_plan: str
    tests_added: tuple[str, ...] = ()
    fixes_defect: str = ""
    branch: str = ""
    state: ReviewState = ReviewState.DRAFT
    ci_status: CIStatus = CIStatus.NOT_RUN
    ci_detail: str = ""
    proposed_by: str = "olympus"
    reviewer: str = ""

    def __post_init__(self):
        object.__setattr__(self, "state", ReviewState(self.state))
        object.__setattr__(self, "ci_status", CIStatus(self.ci_status))
        object.__setattr__(self, "created_at",
                           ensure_utc(self.created_at, field_name="created_at"))
        object.__setattr__(self, "target_files", tuple(self.target_files))
        object.__setattr__(self, "tests_added", tuple(self.tests_added))

        subject = self.proposal_id
        for name in ("proposal_id", "title", "diff", "rationale",
                     "expected_impact", "rollback_plan"):
            object.__setattr__(self, name,
                               _require_text(getattr(self, name), name, subject))
        if not self.target_files:
            raise ConfigurationError("a code change must name the files it edits",
                                     proposal=subject)
        if not self.tests_added:
            raise ConfigurationError(
                "a code change must add or name a test; an untested repair is "
                "how a defect comes back", proposal=subject)

        # The refusal. Checked at construction so a kernel-targeting patch
        # cannot exist as an object, let alone reach a reviewer's queue.
        for path in self.target_files:
            assert_not_kernel(path, what="code-change target")
        for pattern, description in _PATCH_RED_FLAGS:
            if pattern.search(self.diff):
                raise SafetyKernelViolation(
                    "the patch body reaches into the safety kernel even though "
                    "its target files do not: " + description,
                    proposal=subject, detail=description)

    @property
    def touched_modules(self) -> tuple[str, ...]:
        return tuple(sorted({path_to_module(p) for p in self.target_files}))

    @property
    def ready_for_review(self) -> bool:
        """CI passed and a test exists. Not "may be merged" — that is not here."""
        return self.ci_status is CIStatus.PASSED and bool(self.tests_added)

    def fingerprint(self) -> str:
        return hashlib.sha256(
            (self.title + "\n" + self.diff).encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {"proposal_id": self.proposal_id, "title": self.title,
                "created_at": jsonable(self.created_at),
                "target_files": list(self.target_files),
                "touched_modules": list(self.touched_modules),
                "diff": self.diff, "rationale": self.rationale,
                "expected_impact": self.expected_impact,
                "rollback_plan": self.rollback_plan,
                "tests_added": list(self.tests_added),
                "fixes_defect": self.fixes_defect, "branch": self.branch,
                "state": self.state.value, "ci_status": self.ci_status.value,
                "ci_detail": self.ci_detail, "proposed_by": self.proposed_by,
                "reviewer": self.reviewer,
                "ready_for_review": self.ready_for_review,
                "applied": False,
                "note": "proposal only; nothing in olympus.trading applies, "
                        "merges or deploys a code change"}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodeChangeProposal":
        return cls(
            proposal_id=str(data["proposal_id"]), title=str(data["title"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            target_files=tuple(data.get("target_files") or ()),
            diff=str(data.get("diff", "")),
            rationale=str(data.get("rationale", "")),
            expected_impact=str(data.get("expected_impact", "")),
            rollback_plan=str(data.get("rollback_plan", "")),
            tests_added=tuple(data.get("tests_added") or ()),
            fixes_defect=str(data.get("fixes_defect", "")),
            branch=str(data.get("branch", "")),
            state=ReviewState(data.get("state", ReviewState.DRAFT.value)),
            ci_status=CIStatus(data.get("ci_status", CIStatus.NOT_RUN.value)),
            ci_detail=str(data.get("ci_detail", "")),
            proposed_by=str(data.get("proposed_by", "olympus")),
            reviewer=str(data.get("reviewer", "")))

    def evolve(self, **changes) -> "CodeChangeProposal":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return CodeChangeProposal(**data)


class ProposalRegister:
    """Stores proposals of both kinds. Has no method that enacts one.

    `record_review` writes a human's decision into the record. It does not act
    on it: an ACCEPTED code proposal is a note that a person agreed, and the
    merge happens elsewhere, with that person's credentials, leaving their trace
    rather than Olympus's.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _put(self, kind: str, proposal_id: str, payload: Mapping[str, Any]) -> None:
        self._store().put((kind, proposal_id), jsonable(dict(payload)))
        if self.audit is not None:
            try:
                from .audit import EventType
                self.audit.record(EventType.PROPOSAL_SUBMITTED,
                                  {**dict(payload), "proposal_kind": kind},
                                  actor=str(payload.get("proposed_by", "olympus")),
                                  subject=proposal_id)
            except Exception:                            # noqa: BLE001
                pass

    def _get(self, kind: str, proposal_id: str) -> dict | None:
        return self._store().get((kind, proposal_id))

    def _ids(self, kind: str) -> list[str]:
        return sorted(parts[1] for parts in self._store().keys((kind,)))

    # -- features ----------------------------------------------------------

    def submit_feature(self, proposal: FeatureProposal, *,
                       actor: Actor | None = None) -> FeatureProposal:
        actor = actor or Actor.autonomous("proposal-register")
        authorise(Action.PROPOSE_IMPROVEMENT, actor,
                  subject=proposal.proposal_id, clock=self.clock, audit=self.audit)
        submitted = proposal.evolve(state=ReviewState.SUBMITTED)
        self._put("feature", submitted.proposal_id, submitted.to_dict())
        return submitted

    def feature(self, proposal_id: str) -> FeatureProposal | None:
        data = self._get("feature", proposal_id)
        return FeatureProposal.from_dict(data) if data else None

    def features(self, *, state: ReviewState | str | None = None
                 ) -> list[FeatureProposal]:
        out = []
        for proposal_id in self._ids("feature"):
            proposal = self.feature(proposal_id)
            if proposal is None:
                continue
            if state is not None and proposal.state is not ReviewState(state):
                continue
            out.append(proposal)
        return out

    # -- code changes ------------------------------------------------------

    def submit_code_change(self, proposal: CodeChangeProposal, *,
                           actor: Actor | None = None) -> CodeChangeProposal:
        """Record a patch for human review. Autonomous, and only that far."""
        actor = actor or Actor.autonomous("proposal-register")
        authorise(Action.PROPOSE_IMPROVEMENT, actor,
                  subject=proposal.proposal_id, clock=self.clock, audit=self.audit)
        submitted = proposal.evolve(state=ReviewState.SUBMITTED)
        self._put("code", submitted.proposal_id, submitted.to_dict())
        return submitted

    def code_change(self, proposal_id: str) -> CodeChangeProposal | None:
        data = self._get("code", proposal_id)
        return CodeChangeProposal.from_dict(data) if data else None

    def code_changes(self, *, state: ReviewState | str | None = None
                     ) -> list[CodeChangeProposal]:
        out = []
        for proposal_id in self._ids("code"):
            proposal = self.code_change(proposal_id)
            if proposal is None:
                continue
            if state is not None and proposal.state is not ReviewState(state):
                continue
            out.append(proposal)
        return out

    def mark_ci_result(self, proposal_id: str, *, status: CIStatus | str,
                       detail: str = "") -> CodeChangeProposal:
        """Record the outcome of running the change in isolation. Autonomous.

        Recording a *failure* is as important as recording a pass, and there is
        no path that removes a failed result — a proposal's CI history is part
        of what a reviewer is deciding about.
        """
        proposal = self.code_change(proposal_id)
        if proposal is None:
            raise ConfigurationError("unknown code-change proposal",
                                     proposal_id=proposal_id)
        updated = proposal.evolve(ci_status=CIStatus(status), ci_detail=detail)
        self._put("code", proposal_id, updated.to_dict())
        return updated

    # -- review ------------------------------------------------------------

    def record_review(self, proposal_id: str, *, kind: str, actor: Actor,
                      accepted: bool, notes: str = "") -> dict:
        """Write a human's decision into the record. Does not enact it.

        Requires an operator: an autonomous actor recording its own acceptance
        would make the review step ceremonial, which is exactly what a
        self-evolving system would do if permitted.
        """
        from .governance import require_operator
        require_operator(actor, what="reviewing a proposal")
        authorise(Action.DEPLOY_CODE if kind == "code" else Action.PROMOTE_CAPABILITY,
                  actor, subject=proposal_id, clock=self.clock, audit=self.audit)
        data = self._get(kind, proposal_id)
        if data is None:
            raise ConfigurationError("unknown proposal", proposal_id=proposal_id,
                                     kind=kind)
        state = ReviewState.ACCEPTED if accepted else ReviewState.REJECTED
        data = {**data, "state": state.value, "reviewer": actor.name,
                "review_notes": notes,
                "note": "reviewed; enacting an accepted change happens outside "
                        "olympus.trading, with the reviewer's own credentials"}
        self._put(kind, proposal_id, data)
        return data

    def report(self) -> dict:
        features = self.features()
        codes = self.code_changes()
        return {"feature_proposals": len(features),
                "code_change_proposals": len(codes),
                "awaiting_review": len([p for p in features + codes
                                        if p.state is ReviewState.SUBMITTED]),
                "ci_passed": len([c for c in codes if c.ci_status is CIStatus.PASSED]),
                "ci_failed": len([c for c in codes if c.ci_status is CIStatus.FAILED]),
                "applied": 0,
                "note": "`applied` is structurally zero: this package contains "
                        "no code path that applies a proposal"}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"ProposalRegister(namespace={self.namespace!r})"


__all__ = ["FeatureKind", "MARKET_FEATURE_KINDS", "ReviewState",
           "SecurityImpact", "FeatureProposal", "DefectSeverity",
           "DefectReport", "CIStatus", "CodeChangeProposal", "ProposalRegister"]
