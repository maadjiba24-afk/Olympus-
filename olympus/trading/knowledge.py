"""Versioned knowledge: what Olympus believes, why, and how sure it is.

A trading system that learns needs somewhere to put what it learned that is not
a log line and not a model weight. This is that place. Every belief is a
`KnowledgeRecord` carrying its own provenance, confidence, evidence for *and
against*, and an expiry — so that "Olympus thinks BTC gaps at the CME open" can
be traced to what produced it, checked against what contradicts it, and retired
when it stops being true.

Four rules do most of the work here, and each exists because the obvious
implementation is wrong:

**Repetition is not evidence.** Ten articles repeating one rumour is one
rumour. `corroborate()` raises the confidence of an unvalidated external claim
only up to `MAX_UNVALIDATED_CONFIDENCE`, no matter how many sources agree, and
sources that share an origin are counted once. A knowledge base that let
frequency stand in for truth would be a machine for laundering consensus into
fact — and market consensus is often exactly the thing worth betting against.

**External content is untrusted until validated.** A record whose source type
is external is created `UNVALIDATED` and classified `UNVERIFIED_CLAIM`,
whatever it asserts about itself. Validation is a separate, attributed act.

**Correction never deletes.** `correct()` writes a *new version* and marks the
old one superseded. `history()` still returns the superseded record. The
immutable answer to "what did Olympus believe on 14 March, and was it right"
survives every later correction — which is the only way to audit a system that
changes its mind.

**Knowledge is not authority.** `assert_safe_for()` refuses to let any record
reach risk configuration, credentials, permissions or execution settings unless
it is an operator-authored rule. The strongest possible evidence for widening a
limit is still not permission to widen it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import ensure_utc, jsonable
from .errors import ConfigurationError, KnowledgeError
from .storekeys import KeyedStore

_NS = "trading.knowledge"

#: An unvalidated external claim can never be more than this confident, however
#: many sources repeat it. The number is low on purpose: it must not be enough
#: to clear any decision threshold on its own.
MAX_UNVALIDATED_CONFIDENCE = 0.5

#: An inference drawn from other records is capped by its weakest input. Chains
#: of plausible reasoning are how a system talks itself into certainty.
INFERENCE_CONFIDENCE_DISCOUNT = 0.9


class SourceType(str, Enum):
    """Where a record came from. Determines default trust, not content."""

    # -- internal: produced by Olympus observing itself or the market --------
    HISTORICAL_MARKET_DATA = "historical_market_data"
    LIVE_OBSERVATION = "live_observation"
    FORECAST_OUTCOME = "forecast_outcome"
    TRADE_OUTCOME = "trade_outcome"
    STRATEGY_REPORT = "strategy_report"
    REGIME_TRANSITION = "regime_transition"
    INTERNAL_INCIDENT = "internal_incident"
    SYSTEM_FAILURE = "system_failure"
    EXPERIMENT = "experiment"
    # -- human ---------------------------------------------------------------
    HUMAN_FEEDBACK = "human_feedback"
    ANALYST_CONCLUSION = "analyst_conclusion"
    OPERATOR_RULE = "operator_rule"
    # -- external: untrusted until validated ---------------------------------
    ECONOMIC_RELEASE = "economic_release"
    COMPANY_ANNOUNCEMENT = "company_announcement"
    RESEARCH_PAPER = "research_paper"
    EXCHANGE_DOCUMENTATION = "exchange_documentation"
    BROKER_DOCUMENTATION = "broker_documentation"
    REGULATORY = "regulatory"
    NEWS = "news"
    WEB = "web"
    MODEL_OUTPUT = "model_output"


#: Sources Olympus produced itself by measuring something. Trusted for
#: *provenance* — the measurement really happened — never for interpretation.
INTERNAL_SOURCES: frozenset[SourceType] = frozenset({
    SourceType.HISTORICAL_MARKET_DATA, SourceType.LIVE_OBSERVATION,
    SourceType.FORECAST_OUTCOME, SourceType.TRADE_OUTCOME,
    SourceType.STRATEGY_REPORT, SourceType.REGIME_TRANSITION,
    SourceType.INTERNAL_INCIDENT, SourceType.SYSTEM_FAILURE,
    SourceType.EXPERIMENT,
})

#: Sources carrying a human's name and judgement.
HUMAN_SOURCES: frozenset[SourceType] = frozenset({
    SourceType.HUMAN_FEEDBACK, SourceType.ANALYST_CONCLUSION,
    SourceType.OPERATOR_RULE,
})


def is_external(source_type: SourceType) -> bool:
    """Everything not measured internally and not authored by a human.

    Note `MODEL_OUTPUT` is external. A language model summarising a document is
    not a witness to it; treating its output as internal because it ran on our
    hardware is the mistake this classification exists to prevent.
    """
    return source_type not in INTERNAL_SOURCES and source_type not in HUMAN_SOURCES


class Epistemic(str, Enum):
    """What *kind* of thing this record is. Not how confident — what sort."""

    CONFIRMED_FACT = "confirmed_fact"
    UNVERIFIED_CLAIM = "unverified_claim"
    HYPOTHESIS = "hypothesis"
    INFERENCE = "inference"
    EXPERIMENTAL_FINDING = "experimental_finding"
    DEPRECATED = "deprecated"
    #: A rule a human decided to operate by. Carries authority the others do
    #: not, and is the only class permitted anywhere near configuration.
    OPERATOR_RULE = "operator_rule"


class Validation(str, Enum):
    UNVALIDATED = "unvalidated"
    VALIDATING = "validating"
    VALIDATED = "validated"
    REFUTED = "refuted"
    EXPIRED = "expired"


class MemoryClass(str, Enum):
    """Which memory this record lives in. Governs retention and access."""

    #: Never rewritten, never expired. Backed by the audit ledger.
    AUDIT = "audit"
    #: Survives restarts and versions; the compounding part.
    LONG_TERM = "long_term"
    #: Findings from the research lab. Isolated from operational retrieval by
    #: default so an unreplicated experiment cannot influence live decisions.
    RESEARCH = "research"
    #: This session's context. Expires quickly by design.
    OPERATIONAL = "operational"
    INSTRUMENT = "instrument"
    STRATEGY = "strategy"
    DEPRECATED = "deprecated"
    HUMAN_FEEDBACK = "human_feedback"


class Purpose(str, Enum):
    """Why a caller wants a record. The access-control dimension."""

    ANALYSIS = "analysis"
    FORECASTING = "forecasting"
    STRATEGY = "strategy"
    RESEARCH = "research"
    REPORTING = "reporting"
    #: The four that no amount of evidence unlocks for a non-operator record.
    RISK_CONFIGURATION = "risk_configuration"
    CREDENTIALS = "credentials"
    PERMISSIONS = "permissions"
    EXECUTION_SETTINGS = "execution_settings"


#: Purposes that can change what the system is permitted to do. Reaching one of
#: these requires an operator-authored rule, full stop.
PRIVILEGED_PURPOSES: frozenset[Purpose] = frozenset({
    Purpose.RISK_CONFIGURATION, Purpose.CREDENTIALS, Purpose.PERMISSIONS,
    Purpose.EXECUTION_SETTINGS,
})


class Freshness(str, Enum):
    FRESH = "fresh"
    AGEING = "ageing"
    STALE = "stale"
    EXPIRED = "expired"
    #: No event timestamp, so age is unknowable. Treated as stale by callers
    #: that care, and never silently as fresh.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    """One piece of support or contradiction, with its own provenance.

    `origin` is what makes repetition detectable: two articles quoting the same
    press release share an origin and count once.
    """

    ref: str
    kind: str = "observation"
    origin: str = ""
    weight: float = 1.0
    note: str = ""

    def __post_init__(self):
        if not str(self.ref).strip():
            raise ConfigurationError("evidence needs a reference")
        weight = float(self.weight)
        if not 0.0 <= weight <= 1.0:
            raise ConfigurationError("evidence weight must be in [0, 1]",
                                     ref=self.ref, weight=weight)
        object.__setattr__(self, "weight", weight)
        if not str(self.origin).strip():
            object.__setattr__(self, "origin", str(self.ref))

    def to_dict(self) -> dict:
        return {"ref": self.ref, "kind": self.kind, "origin": self.origin,
                "weight": self.weight, "note": self.note}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Evidence":
        return cls(ref=str(data.get("ref", "")), kind=str(data.get("kind", "observation")),
                   origin=str(data.get("origin", "")),
                   weight=float(data.get("weight", 1.0)),
                   note=str(data.get("note", "")))


# --- untrusted-content neutralisation ---------------------------------------
# External text reaches this module as a claim body. It is stored, not obeyed.
# These patterns strip the shapes that try to read as instructions to whatever
# later summarises the knowledge base, matching `sentiment.py`'s treatment of
# news. Storage is not execution, but a knowledge base whose contents are later
# concatenated into a prompt is one indirection away from being one.
_INSTRUCTION_PATTERNS = (
    re.compile(r"(?im)^\s*(?:system|assistant|user)\s*:", ),
    re.compile(r"(?is)<\|.*?\|>"),
    re.compile(r"(?im)^\s*```+\s*(?:system|instructions?)\b.*?$"),
    re.compile(r"(?i)\b(?:ignore|disregard|override)\s+(?:all\s+)?"
               r"(?:previous|prior|above|earlier)\s+instructions?\b"),
    re.compile(r"(?i)\b(?:set|raise|increase|widen|disable|remove)\s+"
               r"(?:the\s+)?(?:risk\s+)?limits?\b"),
)

NEUTRALISED = "[neutralised]"


def neutralise(text: Any) -> Any:
    """Strip instruction-shaped constructs from external text.

    Conservative and additive, like `audit.redact`: a false positive costs a
    marker in a stored claim, a false negative costs an instruction reaching
    something that acts on it.
    """
    if not isinstance(text, str):
        return text
    out = text
    for pattern in _INSTRUCTION_PATTERNS:
        out = pattern.sub(NEUTRALISED, out)
    return out


@dataclass(frozen=True)
class KnowledgeRecord:
    """One versioned belief with everything needed to judge it later."""

    knowledge_id: str
    statement: str
    source: str
    source_type: SourceType
    collected_at: datetime
    #: When the thing being described happened. `None` for timeless claims;
    #: freshness is then UNKNOWN rather than assumed current.
    event_at: datetime | None = None
    epistemic: Epistemic = Epistemic.UNVERIFIED_CLAIM
    validation: Validation = Validation.UNVALIDATED
    memory_class: MemoryClass = MemoryClass.OPERATIONAL
    confidence: float = 0.0
    instruments: tuple[str, ...] = ()
    market: str = ""
    supporting: tuple[Evidence, ...] = ()
    contradicting: tuple[Evidence, ...] = ()
    version: int = 1
    supersedes: str = ""
    superseded_by: str = ""
    review_by: datetime | None = None
    expires_at: datetime | None = None
    tags: tuple[str, ...] = ()
    author: str = "system"
    #: Set when this record was derived from others; used to cap confidence.
    derived_from: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self):
        if not str(self.knowledge_id).strip():
            raise ConfigurationError("knowledge_id must be non-empty")
        if not str(self.statement).strip():
            raise ConfigurationError("a knowledge record must state something",
                                     knowledge_id=self.knowledge_id)
        if not str(self.source).strip():
            raise ConfigurationError(
                "a knowledge record must name its source; an unattributed "
                "belief cannot be re-examined", knowledge_id=self.knowledge_id)
        object.__setattr__(self, "source_type", SourceType(self.source_type))
        object.__setattr__(self, "epistemic", Epistemic(self.epistemic))
        object.__setattr__(self, "validation", Validation(self.validation))
        object.__setattr__(self, "memory_class", MemoryClass(self.memory_class))
        for name in ("collected_at", "event_at", "review_by", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value, field_name=name))
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ConfigurationError("confidence must be in [0, 1]",
                                     knowledge_id=self.knowledge_id,
                                     confidence=confidence)
        if int(self.version) < 1:
            raise ConfigurationError("version starts at 1",
                                     knowledge_id=self.knowledge_id)
        object.__setattr__(self, "version", int(self.version))
        for name in ("instruments", "tags", "derived_from"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "supporting", tuple(self.supporting))
        object.__setattr__(self, "contradicting", tuple(self.contradicting))

        # -- the invariants that make the rest of the module trustworthy ----
        if is_external(self.source_type):
            object.__setattr__(self, "statement", neutralise(self.statement))
            if self.epistemic is Epistemic.OPERATOR_RULE:
                raise KnowledgeError(
                    "external content can never become an operator rule; that "
                    "class carries configuration authority and must originate "
                    "with a named operator, not with a document",
                    knowledge_id=self.knowledge_id,
                    source_type=self.source_type.value)
            if (self.epistemic is Epistemic.CONFIRMED_FACT
                    and self.validation is not Validation.VALIDATED):
                # External content stays an unverified claim *until validated*.
                # Validation is the attributed act that promotes it; asserting
                # the promotion without it is the whole thing being prevented.
                raise KnowledgeError(
                    "external content cannot be recorded as a confirmed fact "
                    "before it is validated; it enters as an unverified claim "
                    "and is promoted only by an attributed validation",
                    knowledge_id=self.knowledge_id,
                    source_type=self.source_type.value,
                    validation=self.validation.value)
            if (self.validation is not Validation.VALIDATED
                    and confidence > MAX_UNVALIDATED_CONFIDENCE):
                raise KnowledgeError(
                    "an unvalidated external claim is capped at "
                    f"{MAX_UNVALIDATED_CONFIDENCE}; corroboration by repetition "
                    "does not raise it further",
                    knowledge_id=self.knowledge_id, confidence=confidence)
        if (self.epistemic is Epistemic.OPERATOR_RULE
                and self.source_type is not SourceType.OPERATOR_RULE):
            raise KnowledgeError(
                "an operator rule must carry the operator_rule source type; "
                "this is the class that unlocks configuration purposes",
                knowledge_id=self.knowledge_id,
                source_type=self.source_type.value)
        if self.validation is Validation.REFUTED and confidence > 0.0:
            raise KnowledgeError("a refuted record has zero confidence",
                                 knowledge_id=self.knowledge_id,
                                 confidence=confidence)

    # -- derived properties ------------------------------------------------

    @property
    def key(self) -> str:
        return f"{self.knowledge_id}@{self.version}"

    @property
    def is_external(self) -> bool:
        return is_external(self.source_type)

    @property
    def superseded(self) -> bool:
        return bool(self.superseded_by)

    @property
    def actionable(self) -> bool:
        """Usable as a basis for a decision — not as a basis for configuration."""
        return (self.validation is Validation.VALIDATED
                and not self.superseded
                and self.epistemic not in (Epistemic.DEPRECATED,
                                           Epistemic.UNVERIFIED_CLAIM))

    @property
    def contested(self) -> bool:
        """Something on record disagrees with this."""
        return bool(self.contradicting)

    def age(self, now: datetime) -> timedelta | None:
        if self.event_at is None:
            return None
        return ensure_utc(now, field_name="now") - self.event_at

    def freshness(self, now: datetime, *,
                  fresh_within: timedelta = timedelta(days=1),
                  ageing_within: timedelta = timedelta(days=30)) -> Freshness:
        """Age band, with expiry taking precedence.

        Expiry outranks age: a record explicitly marked to expire on a date is
        expired on that date whether or not it still looks recent.
        """
        now = ensure_utc(now, field_name="now")
        if self.expires_at is not None and now >= self.expires_at:
            return Freshness.EXPIRED
        if self.validation is Validation.EXPIRED:
            return Freshness.EXPIRED
        age = self.age(now)
        if age is None:
            return Freshness.UNKNOWN
        if age <= fresh_within:
            return Freshness.FRESH
        if age <= ageing_within:
            return Freshness.AGEING
        return Freshness.STALE

    def due_for_review(self, now: datetime) -> bool:
        if self.review_by is None:
            return False
        return ensure_utc(now, field_name="now") >= self.review_by

    def evidence_balance(self) -> float:
        """Support minus contradiction, on distinct origins. In [-1, 1].

        Distinct origins, not distinct references: this is the arithmetic that
        makes repetition worthless.
        """
        def weigh(items: Sequence[Evidence]) -> float:
            best: dict[str, float] = {}
            for item in items:
                best[item.origin] = max(best.get(item.origin, 0.0), item.weight)
            return sum(best.values())
        support = weigh(self.supporting)
        against = weigh(self.contradicting)
        total = support + against
        return 0.0 if total <= 0 else (support - against) / total

    def to_dict(self) -> dict:
        return {
            "knowledge_id": self.knowledge_id, "statement": self.statement,
            "source": self.source, "source_type": self.source_type.value,
            "collected_at": jsonable(self.collected_at),
            "event_at": jsonable(self.event_at),
            "epistemic": self.epistemic.value,
            "validation": self.validation.value,
            "memory_class": self.memory_class.value,
            "confidence": self.confidence, "instruments": list(self.instruments),
            "market": self.market,
            "supporting": [e.to_dict() for e in self.supporting],
            "contradicting": [e.to_dict() for e in self.contradicting],
            "version": self.version, "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "review_by": jsonable(self.review_by),
            "expires_at": jsonable(self.expires_at), "tags": list(self.tags),
            "author": self.author, "derived_from": list(self.derived_from),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KnowledgeRecord":
        def when(name):
            value = data.get(name)
            return datetime.fromisoformat(value) if isinstance(value, str) and value else None
        return cls(
            knowledge_id=str(data.get("knowledge_id", "")),
            statement=str(data.get("statement", "")),
            source=str(data.get("source", "")),
            source_type=SourceType(data.get("source_type", SourceType.WEB.value)),
            collected_at=when("collected_at") or datetime.min,
            event_at=when("event_at"),
            epistemic=Epistemic(data.get("epistemic", Epistemic.UNVERIFIED_CLAIM.value)),
            validation=Validation(data.get("validation", Validation.UNVALIDATED.value)),
            memory_class=MemoryClass(data.get("memory_class", MemoryClass.OPERATIONAL.value)),
            confidence=float(data.get("confidence", 0.0)),
            instruments=tuple(data.get("instruments") or ()),
            market=str(data.get("market", "")),
            supporting=tuple(Evidence.from_dict(e) for e in data.get("supporting") or ()),
            contradicting=tuple(Evidence.from_dict(e) for e in data.get("contradicting") or ()),
            version=int(data.get("version", 1)),
            supersedes=str(data.get("supersedes", "")),
            superseded_by=str(data.get("superseded_by", "")),
            review_by=when("review_by"), expires_at=when("expires_at"),
            tags=tuple(data.get("tags") or ()), author=str(data.get("author", "system")),
            derived_from=tuple(data.get("derived_from") or ()),
            notes=str(data.get("notes", "")))

    def evolve(self, **changes) -> "KnowledgeRecord":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return KnowledgeRecord(**data)

    def fingerprint(self) -> str:
        """Content hash, for detecting a claim already on record."""
        payload = json.dumps({"statement": self.statement,
                              "instruments": sorted(self.instruments),
                              "market": self.market}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------


def assert_safe_for(record: KnowledgeRecord, purpose: Purpose | str) -> KnowledgeRecord:
    """Gate a record against the use the caller intends.

    The privileged purposes are unreachable for anything but an operator rule.
    This is the code-level statement of "no article, webpage, message, document
    or model output may directly alter risk limits, credentials, permissions,
    execution settings or production configuration" — and it holds for a
    validated, highly-confident, thoroughly corroborated article too.
    """
    purpose = Purpose(purpose)
    if purpose in PRIVILEGED_PURPOSES:
        if (record.epistemic is not Epistemic.OPERATOR_RULE
                or record.source_type is not SourceType.OPERATOR_RULE):
            raise KnowledgeError(
                f"knowledge may not reach {purpose.value}: only an "
                "operator-authored rule carries that authority, regardless of "
                "how well evidenced the record is",
                knowledge_id=record.knowledge_id,
                epistemic=record.epistemic.value,
                source_type=record.source_type.value)
        if record.superseded or record.validation is Validation.REFUTED:
            raise KnowledgeError(
                "operator rule is superseded or refuted and may not be applied",
                knowledge_id=record.knowledge_id)
    return record


# ---------------------------------------------------------------------------
# the base
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contradiction:
    """Two live records that cannot both be right."""

    left: str
    right: str
    reason: str

    def to_dict(self) -> dict:
        return {"left": self.left, "right": self.right, "reason": self.reason}


class KnowledgeBase:
    """Versioned, persistent, append-only-per-version knowledge storage.

    Backed by `olympus.store` so beliefs survive restarts — the "retain
    validated lessons across restarts and future versions" requirement is a
    storage property, not a prompt.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 namespace: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = namespace

    # -- persistence -------------------------------------------------------

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def _write(self, record: KnowledgeRecord) -> KnowledgeRecord:
        self._store().put((record.knowledge_id, f"{record.version:06d}"),
                          record.to_dict())
        return record

    def _read_version(self, knowledge_id: str, version: int) -> KnowledgeRecord | None:
        body = self._store().get((knowledge_id, f"{version:06d}"))
        return KnowledgeRecord.from_dict(body) if body else None

    def _versions(self, knowledge_id: str) -> list[int]:
        out = []
        for parts, _ in self._store().items((knowledge_id,)):
            try:
                out.append(int(parts[1]))
            except (IndexError, ValueError):             # pragma: no cover
                continue
        return sorted(out)

    def _emit(self, event_type: Any, record: KnowledgeRecord, action: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(event_type, {**record.to_dict(), "action": action},
                              actor=record.author, subject=record.knowledge_id)
        except Exception:                                # noqa: BLE001
            # Same reasoning as the model registry: the belief is already
            # written and the caller must see it. A missing audit event surfaces
            # in `audit.verify()`, which is the right place for it.
            pass

    # -- writing -----------------------------------------------------------

    def record(self, knowledge_id: str, *, statement: str, source: str,
               source_type: SourceType | str,
               event_at: datetime | None = None,
               epistemic: Epistemic | str | None = None,
               memory_class: MemoryClass | str | None = None,
               confidence: float = 0.0,
               instruments: Iterable[str] = (), market: str = "",
               supporting: Iterable[Evidence] = (),
               contradicting: Iterable[Evidence] = (),
               review_by: datetime | None = None,
               expires_at: datetime | None = None,
               tags: Iterable[str] = (), author: str = "system",
               derived_from: Iterable[str] = (), notes: str = ""
               ) -> KnowledgeRecord:
        """Add a belief at version 1. Refuses to overwrite an existing id.

        Overwriting would destroy the "what did we believe then" answer, which
        is the property that makes this a knowledge base rather than a cache.
        Use `correct()` to change a belief.
        """
        source_type = SourceType(source_type)
        external = is_external(source_type)
        if epistemic is None:
            epistemic = (Epistemic.UNVERIFIED_CLAIM if external
                         else Epistemic.INFERENCE)
        epistemic = Epistemic(epistemic)
        if memory_class is None:
            memory_class = (MemoryClass.HUMAN_FEEDBACK
                            if source_type in HUMAN_SOURCES
                            else MemoryClass.OPERATIONAL)
        validation = Validation.UNVALIDATED
        if external:
            confidence = min(float(confidence), MAX_UNVALIDATED_CONFIDENCE)

        from olympus import proclock
        with proclock.lock(f"knowledge-{knowledge_id}"):
            if self._versions(knowledge_id):
                raise KnowledgeError(
                    "knowledge_id already exists; correct it rather than "
                    "overwriting, so the previous belief stays auditable",
                    knowledge_id=knowledge_id)
            record = KnowledgeRecord(
                knowledge_id=knowledge_id, statement=statement, source=source,
                source_type=source_type, collected_at=self.clock.now(),
                event_at=event_at, epistemic=epistemic, validation=validation,
                memory_class=MemoryClass(memory_class), confidence=confidence,
                instruments=tuple(instruments), market=market,
                supporting=tuple(supporting), contradicting=tuple(contradicting),
                version=1, review_by=review_by, expires_at=expires_at,
                tags=tuple(tags), author=author,
                derived_from=tuple(derived_from), notes=notes)
            self._write(record)
        from .audit import EventType
        self._emit(EventType.KNOWLEDGE_RECORDED, record, "record")
        return record

    def _new_version(self, record: KnowledgeRecord, **changes) -> KnowledgeRecord:
        return record.evolve(version=record.version + 1, **changes)

    def correct(self, knowledge_id: str, *, statement: str | None = None,
                by: str, reason: str, confidence: float | None = None,
                **changes) -> KnowledgeRecord:
        """Replace a belief with a new version. The old one stays readable.

        This is how Olympus changes its mind without erasing the fact that it
        held the earlier view — which is exactly what an operator needs when
        asking why a decision made in March looked reasonable at the time.
        """
        if not str(by).strip():
            raise ConfigurationError("a correction must name its author")
        if not str(reason).strip():
            raise ConfigurationError("a correction must give a reason")
        from olympus import proclock
        with proclock.lock(f"knowledge-{knowledge_id}"):
            current = self.head(knowledge_id)
            if current is None:
                raise KnowledgeError("unknown knowledge_id",
                                     knowledge_id=knowledge_id)
            updates: dict[str, Any] = dict(changes)
            if statement is not None:
                updates["statement"] = statement
            if confidence is not None:
                updates["confidence"] = confidence
            updates.setdefault("supersedes", current.key)
            updates["author"] = by
            updates["collected_at"] = self.clock.now()
            updates["superseded_by"] = ""
            updates["notes"] = reason
            new = self._new_version(current, **updates)
            self._write(current.evolve(superseded_by=new.key))
            self._write(new)
        from .audit import EventType
        self._emit(EventType.KNOWLEDGE_RECORDED, new, "correct")
        return new

    def validate(self, knowledge_id: str, *, by: str, evidence: Iterable[Evidence] = (),
                 confidence: float | None = None) -> KnowledgeRecord:
        """Attributed promotion out of `UNVALIDATED`.

        Validation is what an external claim needs before it can be actionable,
        and it always carries a name. There is no automatic promotion on
        accumulated corroboration — see `corroborate`.
        """
        if not str(by).strip():
            raise ConfigurationError("validation must name its validator")
        current = self.head(knowledge_id)
        if current is None:
            raise KnowledgeError("unknown knowledge_id", knowledge_id=knowledge_id)
        supporting = tuple(current.supporting) + tuple(evidence)
        epistemic = current.epistemic
        if epistemic is Epistemic.UNVERIFIED_CLAIM:
            epistemic = (Epistemic.CONFIRMED_FACT if not current.is_external
                         else Epistemic.CONFIRMED_FACT)
        return self.correct(
            knowledge_id, by=by, reason=f"validated by {by}",
            validation=Validation.VALIDATED, epistemic=epistemic,
            supporting=supporting,
            confidence=current.confidence if confidence is None else confidence)

    def refute(self, knowledge_id: str, *, by: str, evidence: Iterable[Evidence],
               reason: str = "") -> KnowledgeRecord:
        """Mark a belief false. Confidence goes to zero; the record survives."""
        evidence = tuple(evidence)
        if not evidence:
            raise ConfigurationError(
                "a refutation must cite evidence; an unevidenced refutation is "
                "just a contrary opinion")
        current = self.head(knowledge_id)
        if current is None:
            raise KnowledgeError("unknown knowledge_id", knowledge_id=knowledge_id)
        return self.correct(
            knowledge_id, by=by, reason=reason or f"refuted by {by}",
            validation=Validation.REFUTED, confidence=0.0,
            contradicting=tuple(current.contradicting) + evidence)

    def corroborate(self, knowledge_id: str, evidence: Evidence, *,
                    by: str = "system") -> KnowledgeRecord:
        """Add support. Confidence rises only within the honest ceiling.

        The rule that makes this module worth having: an unvalidated external
        claim cannot exceed `MAX_UNVALIDATED_CONFIDENCE` however many
        independent-looking sources repeat it, and sources sharing an origin do
        not count twice at all.
        """
        current = self.head(knowledge_id)
        if current is None:
            raise KnowledgeError("unknown knowledge_id", knowledge_id=knowledge_id)
        known_origins = {e.origin for e in current.supporting}
        if evidence.origin in known_origins:
            # Already counted. Recording it again would let one source vote
            # twice by being quoted twice.
            return current
        supporting = tuple(current.supporting) + (evidence,)
        balance = KnowledgeRecord.evidence_balance(
            current.evolve(supporting=supporting))
        proposed = max(current.confidence, min(1.0, 0.5 * (1.0 + balance)))
        if current.is_external and current.validation is not Validation.VALIDATED:
            proposed = min(proposed, MAX_UNVALIDATED_CONFIDENCE)
        return self.correct(knowledge_id, by=by,
                            reason=f"corroborated by {evidence.ref}",
                            supporting=supporting, confidence=proposed)

    def contradict(self, knowledge_id: str, evidence: Evidence, *,
                   by: str = "system") -> KnowledgeRecord:
        """Record something that disagrees. Lowers confidence, never below 0."""
        current = self.head(knowledge_id)
        if current is None:
            raise KnowledgeError("unknown knowledge_id", knowledge_id=knowledge_id)
        contradicting = tuple(current.contradicting) + (evidence,)
        balance = KnowledgeRecord.evidence_balance(
            current.evolve(contradicting=contradicting))
        proposed = max(0.0, min(current.confidence, 0.5 * (1.0 + balance)))
        return self.correct(knowledge_id, by=by,
                            reason=f"contradicted by {evidence.ref}",
                            contradicting=contradicting, confidence=proposed)

    def deprecate(self, knowledge_id: str, *, by: str, reason: str
                  ) -> KnowledgeRecord:
        """Retire a belief that has stopped being useful or true."""
        record = self.correct(knowledge_id, by=by, reason=reason,
                              epistemic=Epistemic.DEPRECATED,
                              memory_class=MemoryClass.DEPRECATED)
        from .audit import EventType
        self._emit(EventType.KNOWLEDGE_DEPRECATED, record, "deprecate")
        return record

    # -- reading -----------------------------------------------------------

    def head(self, knowledge_id: str) -> KnowledgeRecord | None:
        """The current version, or None."""
        versions = self._versions(knowledge_id)
        if not versions:
            return None
        return self._read_version(knowledge_id, versions[-1])

    def get(self, knowledge_id: str, version: int | None = None,
            *, purpose: Purpose | str = Purpose.ANALYSIS
            ) -> KnowledgeRecord | None:
        """Fetch, access-checked against the caller's stated purpose."""
        record = (self.head(knowledge_id) if version is None
                  else self._read_version(knowledge_id, version))
        if record is None:
            return None
        return assert_safe_for(record, purpose)

    def history(self, knowledge_id: str) -> list[KnowledgeRecord]:
        """Every version, oldest first. This is what makes correction auditable."""
        out = []
        for version in self._versions(knowledge_id):
            record = self._read_version(knowledge_id, version)
            if record is not None:
                out.append(record)
        return out

    def ids(self) -> list[str]:
        return sorted({parts[0] for parts in self._store().keys()})

    def search(self, *, instrument: str | None = None,
               memory_class: MemoryClass | str | None = None,
               epistemic: Epistemic | str | None = None,
               validation: Validation | str | None = None,
               tag: str | None = None, market: str | None = None,
               actionable_only: bool = False,
               include_superseded: bool = False,
               min_confidence: float = 0.0) -> list[KnowledgeRecord]:
        """Retrieval. Deterministically ordered by id so tests can assert it."""
        out: list[KnowledgeRecord] = []
        for knowledge_id in self.ids():
            record = self.head(knowledge_id)
            if record is None:
                continue
            if record.superseded and not include_superseded:
                continue
            if instrument is not None and instrument not in record.instruments:
                continue
            if market is not None and record.market != market:
                continue
            if memory_class is not None and record.memory_class is not MemoryClass(memory_class):
                continue
            if epistemic is not None and record.epistemic is not Epistemic(epistemic):
                continue
            if validation is not None and record.validation is not Validation(validation):
                continue
            if tag is not None and tag not in record.tags:
                continue
            if record.confidence < min_confidence:
                continue
            if actionable_only and not record.actionable:
                continue
            out.append(record)
        return sorted(out, key=lambda r: r.knowledge_id)

    def for_decision(self, *, instrument: str | None = None,
                     min_confidence: float = 0.6) -> list[KnowledgeRecord]:
        """What may inform a trading decision right now.

        Deliberately narrow: validated, unsuperseded, non-research, non-expired,
        above a confidence floor. Research memory is excluded by default because
        an unreplicated experimental finding influencing a live decision is the
        exact leak this separation exists to prevent.
        """
        now = self.clock.now()
        out = []
        for record in self.search(instrument=instrument, actionable_only=True,
                                  min_confidence=min_confidence):
            if record.memory_class is MemoryClass.RESEARCH:
                continue
            if record.freshness(now) is Freshness.EXPIRED:
                continue
            out.append(record)
        return out

    # -- maintenance -------------------------------------------------------

    def expire_stale(self, *, by: str = "system") -> list[KnowledgeRecord]:
        """Mark past-expiry records EXPIRED. Returns what changed.

        Expiry is a state change, not a deletion: an expired belief is still
        the belief that was acted on last month.
        """
        now = self.clock.now()
        changed = []
        for record in self.search():
            if record.validation is Validation.EXPIRED:
                continue
            if record.expires_at is not None and now >= record.expires_at:
                changed.append(self.correct(
                    record.knowledge_id, by=by,
                    reason="expired: past its stated expiry date",
                    validation=Validation.EXPIRED,
                    epistemic=Epistemic.DEPRECATED,
                    memory_class=MemoryClass.DEPRECATED))
        return changed

    def due_for_review(self) -> list[KnowledgeRecord]:
        now = self.clock.now()
        return [r for r in self.search() if r.due_for_review(now)]

    def contradictions(self) -> list[Contradiction]:
        """Live records that cannot both hold.

        Two detectors, both conservative: an explicit contradiction reference
        naming another record, and two live records making the same claim with
        opposite validation. Reported, never auto-resolved — deciding which of
        two contradictory beliefs to keep is a judgement, and a knowledge base
        that silently picked one would hide the disagreement that matters.
        """
        live = self.search()
        by_id = {r.knowledge_id: r for r in live}
        found: list[Contradiction] = []
        seen: set[tuple[str, str]] = set()

        for record in live:
            for evidence in record.contradicting:
                other = by_id.get(evidence.ref)
                if other is None:
                    continue
                pair = tuple(sorted((record.knowledge_id, other.knowledge_id)))
                if pair in seen:
                    continue
                seen.add(pair)
                found.append(Contradiction(
                    left=pair[0], right=pair[1],
                    reason=f"{record.knowledge_id} cites {other.knowledge_id} "
                           "as contradicting evidence"))

        by_fingerprint: dict[str, list[KnowledgeRecord]] = {}
        for record in live:
            by_fingerprint.setdefault(record.fingerprint(), []).append(record)
        for group in by_fingerprint.values():
            for i, left in enumerate(group):
                for right in group[i + 1:]:
                    if left.validation is right.validation:
                        continue
                    if {left.validation, right.validation} != {Validation.VALIDATED,
                                                               Validation.REFUTED}:
                        continue
                    pair = tuple(sorted((left.knowledge_id, right.knowledge_id)))
                    if pair in seen:
                        continue
                    seen.add(pair)
                    found.append(Contradiction(
                        left=pair[0], right=pair[1],
                        reason="identical claim recorded as both validated and "
                               "refuted"))
        return sorted(found, key=lambda c: (c.left, c.right))

    def stats(self) -> dict:
        live = self.search()
        by_class: dict[str, int] = {}
        by_epistemic: dict[str, int] = {}
        for record in live:
            by_class[record.memory_class.value] = by_class.get(record.memory_class.value, 0) + 1
            by_epistemic[record.epistemic.value] = by_epistemic.get(record.epistemic.value, 0) + 1
        return {"live_records": len(live), "ids": len(self.ids()),
                "by_memory_class": by_class, "by_epistemic": by_epistemic,
                "contradictions": len(self.contradictions()),
                "due_for_review": len(self.due_for_review())}

    def __repr__(self) -> str:                          # pragma: no cover
        return f"KnowledgeBase(namespace={self.namespace!r})"


__all__ = [
    "SourceType", "INTERNAL_SOURCES", "HUMAN_SOURCES", "is_external",
    "Epistemic", "Validation", "MemoryClass", "Purpose", "PRIVILEGED_PURPOSES",
    "Freshness", "Evidence", "KnowledgeRecord", "KnowledgeBase",
    "Contradiction", "assert_safe_for", "neutralise", "NEUTRALISED",
    "MAX_UNVALIDATED_CONFIDENCE", "INFERENCE_CONFIDENCE_DISCOUNT",
]
