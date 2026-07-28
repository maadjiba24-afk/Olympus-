"""Model registry: which models exist, and which are allowed near real money.

A forecast is only reproducible if the checkpoint that produced it is
identifiable. The upstream Kronos project pins its Hugging Face revisions, but
nothing downstream *enforces* that, and a model loaded from a moving pointer
(`main`, `latest`) silently changes underneath a running system — at which point
the `model_version` recorded in the audit trail is a lie, and "why did we take
that trade in March" has no answer.

This registry closes that in three ways:

1. **A model with an unpinned revision can never be approved.** Not a warning, a
   refusal. `main`, `master`, `head`, `latest` and an empty revision are all
   unpinned, as is a `pin` mapping that contradicts the record it pins.
2. **Approval requires a named operator and writes an audit event.** Approval is
   a human act with consequences, so it carries a human's name into the
   immutable trail. There is no "auto-approve on good metrics" path: metrics are
   evidence for an operator, not an authority.
3. **Only APPROVED models may be used in live modes.** `assert_usable` raises
   `ModeNotPermitted` otherwise, which is what `modes.ModelApprovedGate` reads
   when deciding whether live trading may be enabled at all.

Non-live modes deliberately permit unapproved models. Backtest, paper and shadow
are exactly where a candidate earns its approval; a registry that refused to run
an unapproved model anywhere would make approval unobtainable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .clock import Clock, default_clock
from .contracts import Mode, ensure_utc, jsonable
from .errors import ConfigurationError, ModeNotPermitted

#: Store namespace (docs/TRADING_ARCHITECTURE.md §10).
_NS = "trading.registry"

#: Lifecycle states. "candidate" is the only state a model can be registered
#: into; everything else is an operator decision.
CANDIDATE = "candidate"
APPROVED = "approved"
RETIRED = "retired"
STATUSES = (CANDIDATE, APPROVED, RETIRED)

#: Revision strings that name a *moving target* rather than an immutable
#: commit. Loading one of these makes a run unreproducible by construction.
MOVING_REVISIONS = frozenset({"main", "master", "head", "latest", "dev",
                              "develop", "trunk", "default"})


@dataclass(frozen=True)
class ModelRecord:
    """One model's identity, provenance, and lifecycle state.

    `sha256` is the content hash where one is available; `revision` is the VCS
    or Hub revision. Both are kept because they answer different questions: the
    revision says which release this claims to be, the hash says what was
    actually loaded, and a mismatch between them is a supply-chain event.
    """
    model_id: str
    kind: str                              # "forecaster" | "tokenizer" | ...
    version: str = ""
    repo_id: str = ""
    revision: str = ""
    sha256: str = ""
    registered_at: datetime | None = None
    registered_by: str = "system"
    status: str = CANDIDATE
    metrics: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""
    #: The full pin: repo, revision, hashes, anything else needed to reload
    #: exactly this artefact.
    pin: Mapping[str, Any] = field(default_factory=dict)
    approved_by: str = ""
    approved_at: datetime | None = None
    retired_at: datetime | None = None

    def __post_init__(self):
        if not str(self.model_id).strip():
            raise ConfigurationError("model_id must be non-empty")
        if not str(self.kind).strip():
            raise ConfigurationError("model kind must be non-empty",
                                     model_id=self.model_id)
        if self.status not in STATUSES:
            raise ConfigurationError("unknown model status",
                                     model_id=self.model_id, got=self.status,
                                     expected=list(STATUSES))
        for name in ("registered_at", "approved_at", "retired_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, ensure_utc(value, field_name=name))
        object.__setattr__(self, "metrics", dict(self.metrics))
        object.__setattr__(self, "pin", dict(self.pin))

    @property
    def is_pinned(self) -> bool:
        """True only when this record identifies an immutable artefact.

        A missing revision, a moving pointer, or a `pin` mapping whose revision
        disagrees with the record's all count as unpinned. The last case matters
        because a stale pin looks like diligence while loading something else.
        """
        revision = str(self.revision or "").strip()
        if not revision or revision.lower() in MOVING_REVISIONS:
            return False
        pinned_revision = str(self.pin.get("revision", "") or "").strip()
        if pinned_revision and pinned_revision != revision:
            return False
        pinned_sha = str(self.pin.get("sha256", "") or "").strip()
        if pinned_sha and self.sha256 and pinned_sha != self.sha256:
            return False
        return True

    @property
    def approved(self) -> bool:
        return self.status == APPROVED

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "kind": self.kind,
                "version": self.version, "repo_id": self.repo_id,
                "revision": self.revision, "sha256": self.sha256,
                "registered_at": jsonable(self.registered_at),
                "registered_by": self.registered_by, "status": self.status,
                "metrics": jsonable(dict(self.metrics)), "notes": self.notes,
                "pin": jsonable(dict(self.pin)),
                "approved_by": self.approved_by,
                "approved_at": jsonable(self.approved_at),
                "retired_at": jsonable(self.retired_at),
                "is_pinned": self.is_pinned}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ModelRecord":
        def when(name):
            value = data.get(name)
            return datetime.fromisoformat(value) if isinstance(value, str) and value else None
        return cls(
            model_id=str(data.get("model_id", "")),
            kind=str(data.get("kind", "")),
            version=str(data.get("version", "")),
            repo_id=str(data.get("repo_id", "")),
            revision=str(data.get("revision", "")),
            sha256=str(data.get("sha256", "")),
            registered_at=when("registered_at"),
            registered_by=str(data.get("registered_by", "system")),
            status=str(data.get("status", CANDIDATE)),
            metrics=data.get("metrics") or {}, notes=str(data.get("notes", "")),
            pin=data.get("pin") or {},
            approved_by=str(data.get("approved_by", "")),
            approved_at=when("approved_at"), retired_at=when("retired_at"))

    def evolve(self, **changes) -> "ModelRecord":
        data = {f: getattr(self, f) for f in self.__dataclass_fields__}
        data.update(changes)
        return ModelRecord(**data)


class ModelRegistry:
    """The record of which models exist and which may touch real money.

    Backed by `olympus.store` so the answer survives a restart, and guarded by
    `proclock` on every read-modify-write so two operators approving at once
    cannot lose one of the decisions.
    """

    def __init__(self, *, clock: Clock | None = None, audit: Any = None,
                 store_ns: str = _NS):
        self.clock = clock or default_clock()
        self.audit = audit
        self.namespace = store_ns

    # -- persistence -------------------------------------------------------

    def _store(self):
        from olympus import store
        return store.backend()

    def _read(self, model_id: str) -> ModelRecord | None:
        raw = self._store().get(self.namespace, model_id)
        if not raw:
            return None
        return ModelRecord.from_dict(json.loads(raw.decode("utf-8")))

    def _write(self, record: ModelRecord) -> ModelRecord:
        payload = json.dumps(record.to_dict(), sort_keys=True).encode("utf-8")
        self._store().put(self.namespace, record.model_id, payload)
        return record

    def _record_event(self, record: ModelRecord, action: str, actor: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record("MODEL_CHANGED",
                              {**record.to_dict(), "action": action},
                              actor=actor or "system", subject=record.model_id)
        except Exception:                                # noqa: BLE001
            # The state change already happened and the caller must see it; a
            # missing audit event shows up in `audit.verify()` rather than
            # rolling back an approval that an operator believes they made.
            pass

    # -- lifecycle ---------------------------------------------------------

    def register(self, model_id: str, *, kind: str, version: str = "",
                 repo_id: str = "", revision: str = "", sha256: str = "",
                 registered_by: str = "system",
                 metrics: Mapping[str, Any] | None = None, notes: str = "",
                 pin: Mapping[str, Any] | None = None) -> ModelRecord:
        """Register a model as a CANDIDATE.

        Registration never confers permission — a newly registered model is a
        candidate, always, whatever its metrics say. Re-registering an existing
        model is refused rather than silently overwriting: changing the identity
        under an approval would launder an unreviewed checkpoint into live use.
        """
        from olympus import proclock
        with proclock.lock(f"registry-{model_id}"):
            existing = self._read(model_id)
            if existing is not None:
                raise ConfigurationError(
                    "model_id is already registered; retire it or use a new id "
                    "rather than redefining what an approval refers to",
                    model_id=model_id, status=existing.status)
            record = ModelRecord(
                model_id=model_id, kind=kind, version=version, repo_id=repo_id,
                revision=revision, sha256=sha256,
                registered_at=self.clock.now(), registered_by=registered_by,
                status=CANDIDATE, metrics=dict(metrics or {}), notes=notes,
                pin=dict(pin or {}))
            self._write(record)
        self._record_event(record, "register", registered_by)
        return record

    def approve(self, model_id: str, *, operator: str, notes: str = "",
                metrics: Mapping[str, Any] | None = None) -> ModelRecord:
        """Approve a model for live use. Requires a named operator.

        Two refusals worth stating plainly:

        * **No operator, no approval.** An empty or whitespace operator string
          raises. An approval nobody's name is on cannot be reviewed later.
        * **No pin, no approval.** An unpinned revision means the artefact can
          change without the registry noticing, so the approval would apply to
          something nobody evaluated.
        """
        operator = str(operator or "").strip()
        if not operator:
            raise ConfigurationError(
                "approval requires a named operator; an unattributed approval "
                "cannot be reviewed", model_id=model_id)
        from olympus import proclock
        with proclock.lock(f"registry-{model_id}"):
            record = self._read(model_id)
            if record is None:
                raise ConfigurationError("unknown model", model_id=model_id)
            if record.status == RETIRED:
                raise ConfigurationError(
                    "a retired model cannot be approved; register the "
                    "replacement under a new id", model_id=model_id)
            if not record.is_pinned:
                raise ConfigurationError(
                    "cannot approve a model whose revision is not pinned: a "
                    "moving revision makes every forecast unreproducible and "
                    "the recorded model_version untrue",
                    model_id=model_id, revision=record.revision or "(empty)")
            record = record.evolve(
                status=APPROVED, approved_by=operator,
                approved_at=self.clock.now(),
                notes=notes or record.notes,
                metrics={**record.metrics, **dict(metrics or {})})
            self._write(record)
        self._record_event(record, "approve", operator)
        return record

    def retire(self, model_id: str, *, operator: str = "system",
               reason: str = "") -> ModelRecord:
        """Retire a model. Immediately unusable in live modes."""
        from olympus import proclock
        with proclock.lock(f"registry-{model_id}"):
            record = self._read(model_id)
            if record is None:
                raise ConfigurationError("unknown model", model_id=model_id)
            record = record.evolve(status=RETIRED, retired_at=self.clock.now(),
                                   notes=reason or record.notes)
            self._write(record)
        self._record_event(record, "retire", operator)
        return record

    # -- queries -----------------------------------------------------------

    def get(self, model_id: str) -> ModelRecord | None:
        return self._read(model_id)

    def list(self, status: str | None = None,
             kind: str | None = None) -> list[ModelRecord]:
        """Every registered model, optionally filtered. Deterministically ordered."""
        if status is not None and status not in STATUSES:
            raise ConfigurationError("unknown status filter", got=status,
                                     expected=list(STATUSES))
        out = []
        for key in sorted(self._store().keys(self.namespace)):
            record = self._read(key)
            if record is None:
                continue
            if status is not None and record.status != status:
                continue
            if kind is not None and record.kind != kind:
                continue
            out.append(record)
        return out

    def approved(self, kind: str) -> ModelRecord | None:
        """The approved model of this kind, or None.

        Returns the most recently approved one when several qualify, tie-broken
        by `model_id` so the answer is deterministic — two processes asking the
        same question must load the same checkpoint.
        """
        candidates = [r for r in self.list(status=APPROVED, kind=kind)]
        if not candidates:
            return None
        epoch = datetime.min.replace(tzinfo=self.clock.now().tzinfo)
        candidates.sort(key=lambda r: (r.approved_at or epoch, r.model_id))
        return candidates[-1]

    # -- the gate ----------------------------------------------------------

    def assert_usable(self, model_id: str, mode: Mode) -> ModelRecord | None:
        """Permit or refuse this model in this mode.

        Live modes require an APPROVED record: unknown, candidate and retired
        models all raise `ModeNotPermitted`. Non-live modes permit anything,
        including a model that was never registered — backtest, paper and shadow
        are where a candidate earns approval, and refusing to run it there would
        make approval unreachable.
        """
        mode = Mode(mode)
        record = self._read(model_id)
        if not mode.is_live:
            return record
        if record is None:
            raise ModeNotPermitted(
                "model is not registered and cannot be used in a live mode",
                model_id=model_id, mode=mode.value)
        if record.status != APPROVED:
            raise ModeNotPermitted(
                "model is not approved for live use",
                model_id=model_id, mode=mode.value, status=record.status)
        if not record.is_pinned:
            # Belt and braces: approval already requires a pin, but a record
            # edited out of band must not be able to smuggle an unpinned model
            # into live trading.
            raise ModeNotPermitted(
                "approved model is no longer pinned to an immutable revision",
                model_id=model_id, mode=mode.value,
                revision=record.revision or "(empty)")
        return record

    def assert_all_usable(self, model_ids: Sequence[str], mode: Mode) -> None:
        """Check a whole set. Never short-circuits — an operator needs the full
        list of what is blocking live trading, not the first item."""
        blocked = []
        for model_id in model_ids:
            try:
                self.assert_usable(model_id, mode)
            except ModeNotPermitted as exc:
                blocked.append(f"{model_id}: {exc.message}")
        if blocked:
            raise ModeNotPermitted(
                "one or more models are not usable in this mode",
                mode=Mode(mode).value, blocked=blocked)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"ModelRegistry(namespace={self.namespace!r})"


__all__ = ["ModelRecord", "ModelRegistry", "CANDIDATE", "APPROVED", "RETIRED",
           "STATUSES", "MOVING_REVISIONS"]
