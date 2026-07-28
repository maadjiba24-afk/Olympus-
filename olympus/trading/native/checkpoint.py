"""Olympus-owned checkpoints, and the manifest that makes them claimable.

A checkpoint without provenance is a number nobody can defend. This module
makes provenance a construction precondition rather than a convention: a
`NativeCheckpoint` cannot exist without a `TrainingManifest`, and the manifest
cannot exist without a data hash, split boundaries, a seed, a config, a code
version, dependency versions and the feature set the model was trained against.

Two gates live here.

**G8 — every checkpoint carries a complete manifest.** Enforced in
`TrainingManifest.__post_init__`. Each required field has a reason:

* `data_hash` — "which bars" is the first question about any model, and a
  dataset that changed underneath a rerun is the commonest cause of an
  irreproducible result.
* `train_start` / `train_end` — the split boundary is what makes an
  out-of-sample claim checkable.
* `seed` — without it, "run it again" is not an instruction.
* `feature_names` — a model served against a different feature set than it was
  trained on is silently, totally wrong.
* `dependency_versions` — the same code on a different numeric library is
  different code.

**G3 — native weights are never initialised from foreign weights.**
`assert_olympus_origin` accepts exactly two things: nothing (a fresh model), or
the id of a checkpoint already in this store. A filesystem path, a URL, or a
Hugging Face repo id raises. This is deliberately a *whitelist by shape*: there
is no argument value that reaches a foreign file, so the guarantee does not
depend on anyone remembering to check.

The parameters themselves are JSON. A statistical estimator's parameters are a
few kilobytes of tables, and keeping them human-readable means a reviewer can
look at what a model actually learned. When a tensor model arrives in a later
phase this becomes a manifest beside a binary blob — the manifest, which is what
the claim rests on, does not change.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError
from ..storekeys import KeyedStore

_NS = "trading.native.checkpoints"

#: Bumped when the checkpoint envelope changes shape. A checkpoint written
#: under an older version is readable but flagged, never silently re-interpreted.
CHECKPOINT_SCHEMA_VERSION = 1

#: Shapes that mean "somewhere outside this store". Matched against the
#: `initialised_from` argument, which is the only place a foreign origin could
#: enter.
_FOREIGN_SHAPES = (
    (re.compile(r"^[a-z][a-z0-9+.-]*://", re.I), "a URL"),
    (re.compile(r"^[~/]|^[A-Za-z]:[\\/]"), "a filesystem path"),
    (re.compile(r"\.(safetensors|ckpt|pt|pth|bin|h5|onnx|gguf)$", re.I),
     "a weights file"),
    (re.compile(r"^[\w.-]+/[\w.-]+$"), "a model-hub repository id"),
)


class ForeignWeightsRefused(ConfigurationError):
    """Something tried to seed a native model from weights Olympus did not train.

    A distinct type because this is the one configuration error that is a
    *provenance* failure rather than a wiring mistake: accepting it would make
    every downstream independence claim false, quietly.
    """
    code = "FOREIGN_WEIGHTS_REFUSED"


def assert_olympus_origin(initialised_from: Any, *,
                          known: Sequence[str] = ()) -> str:
    """Permit a fresh start or an Olympus checkpoint id. Refuse anything else.

    `known` is the set of checkpoint ids that exist. Passing it turns "looks
    like an id" into "is an id we wrote", which is the difference between a
    naming convention and a guarantee.
    """
    if initialised_from is None or initialised_from == "":
        return ""
    origin = str(initialised_from).strip()
    for pattern, description in _FOREIGN_SHAPES:
        if pattern.search(origin):
            raise ForeignWeightsRefused(
                f"refusing to initialise a native model from {description}: a "
                "native checkpoint may start fresh or continue an Olympus "
                "checkpoint, and nothing else. Weights Olympus did not train "
                "are not Olympus weights, whatever the file is called.",
                initialised_from=origin, looks_like=description)
    if known and origin not in set(known):
        raise ForeignWeightsRefused(
            "initialised_from names a checkpoint this store does not contain",
            initialised_from=origin, known=sorted(known))
    return origin


def dependency_versions() -> dict[str, str]:
    """What this process is actually running. Recorded, not assumed.

    Only the interpreter and platform for a stdlib model. When a tensor library
    arrives it is added here, and a checkpoint trained under a different version
    becomes visible in a diff rather than in a mysterious numerical change.
    """
    return {"python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.system().lower()}


@dataclass(frozen=True)
class TrainingManifest:
    """Everything needed to reproduce, audit or distrust a checkpoint."""

    #: Content hash of the exact bars — `data.data_version`.
    data_hash: str
    train_start: datetime
    train_end: datetime
    seed: int
    #: The model's own hyperparameters, verbatim.
    config: Mapping[str, Any]
    #: Feature names, in the order the model consumed them.
    feature_names: tuple[str, ...]
    dataset: Mapping[str, Any] = field(default_factory=dict)
    versions: Mapping[str, str] = field(default_factory=dict)
    code_version: str = ""
    trained_at: datetime | None = None
    trained_by: str = "olympus"
    n_train_rows: int = 0
    n_test_rows: int = 0
    embargoed_rows: int = 0
    #: Metrics recorded during fitting, stage by stage. Kept whole: a final
    #: number with no trajectory hides a model that was better three stages ago.
    metrics: tuple[Mapping[str, Any], ...] = ()
    notes: str = ""

    def __post_init__(self):
        if not str(self.data_hash).strip():
            raise ConfigurationError(
                "a manifest must record the data hash: 'which bars' is the "
                "first question anyone asks about a model")
        for name in ("train_start", "train_end"):
            object.__setattr__(self, name,
                               ensure_utc(getattr(self, name), field_name=name))
        if self.train_end <= self.train_start:
            raise ConfigurationError("the training window is empty",
                                     train_start=self.train_start,
                                     train_end=self.train_end)
        if self.trained_at is not None:
            object.__setattr__(self, "trained_at",
                               ensure_utc(self.trained_at, field_name="trained_at"))
        object.__setattr__(self, "seed", int(self.seed))
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "feature_names", tuple(self.feature_names))
        object.__setattr__(self, "dataset", dict(self.dataset))
        object.__setattr__(self, "versions", dict(self.versions) or dependency_versions())
        object.__setattr__(self, "metrics", tuple(dict(m) for m in self.metrics))
        if not self.feature_names:
            raise ConfigurationError(
                "a manifest must record the feature names in order: a model "
                "served against a different feature set is silently wrong")
        if not self.config:
            raise ConfigurationError("a manifest must record the model config")

    @property
    def reproducible(self) -> bool:
        """Is there enough here to run it again and get the same answer?

        `code_version` is the one field that is *recorded but not required*: it
        is unavailable outside a git checkout, and refusing to write a
        checkpoint in that case would push people toward not writing manifests.
        Its absence is reported instead.
        """
        return bool(self.data_hash and self.feature_names and self.config
                    and self.code_version)

    @property
    def gaps(self) -> tuple[str, ...]:
        """What is missing for full reproducibility. Empty is the good state."""
        out = []
        if not self.code_version:
            out.append("code_version")
        if not self.versions:
            out.append("dependency versions")
        if not self.dataset:
            out.append("dataset spec")
        return tuple(out)

    def to_dict(self) -> dict:
        return {"data_hash": self.data_hash,
                "train_start": jsonable(self.train_start),
                "train_end": jsonable(self.train_end),
                "seed": self.seed, "config": jsonable(dict(self.config)),
                "feature_names": list(self.feature_names),
                "dataset": jsonable(dict(self.dataset)),
                "versions": dict(self.versions),
                "code_version": self.code_version,
                "trained_at": jsonable(self.trained_at),
                "trained_by": self.trained_by,
                "n_train_rows": self.n_train_rows,
                "n_test_rows": self.n_test_rows,
                "embargoed_rows": self.embargoed_rows,
                "metrics": [jsonable(dict(m)) for m in self.metrics],
                "notes": self.notes,
                "reproducible": self.reproducible,
                "gaps": list(self.gaps)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrainingManifest":
        when = data.get("trained_at")
        return cls(data_hash=str(data["data_hash"]),
                   train_start=datetime.fromisoformat(data["train_start"]),
                   train_end=datetime.fromisoformat(data["train_end"]),
                   seed=int(data.get("seed", 0)),
                   config=data.get("config") or {},
                   feature_names=tuple(data.get("feature_names") or ()),
                   dataset=data.get("dataset") or {},
                   versions=data.get("versions") or {},
                   code_version=str(data.get("code_version", "")),
                   trained_at=(datetime.fromisoformat(when)
                               if isinstance(when, str) and when else None),
                   trained_by=str(data.get("trained_by", "olympus")),
                   n_train_rows=int(data.get("n_train_rows", 0)),
                   n_test_rows=int(data.get("n_test_rows", 0)),
                   embargoed_rows=int(data.get("embargoed_rows", 0)),
                   metrics=tuple(data.get("metrics") or ()),
                   notes=str(data.get("notes", "")))


@dataclass(frozen=True)
class NativeCheckpoint:
    """Trained parameters plus the manifest that lets anyone judge them."""

    checkpoint_id: str
    model_kind: str
    version: str
    manifest: TrainingManifest
    #: The learned parameters. JSON for a statistical model; a reference to a
    #: blob when a tensor model arrives.
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: "" for a fresh model, or the id of the Olympus checkpoint continued.
    initialised_from: str = ""
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self):
        for name in ("checkpoint_id", "model_kind", "version"):
            if not str(getattr(self, name)).strip():
                raise ConfigurationError(f"{name} must be non-empty")
        if not isinstance(self.manifest, TrainingManifest):
            raise ConfigurationError(
                "a checkpoint must carry a TrainingManifest; parameters without "
                "provenance are a number nobody can defend",
                checkpoint_id=self.checkpoint_id)
        object.__setattr__(self, "parameters", dict(self.parameters))
        # The G3 guard, at construction. There is no way to build a checkpoint
        # object that records a foreign origin.
        object.__setattr__(self, "initialised_from",
                           assert_olympus_origin(self.initialised_from))
        if not self.parameters:
            raise ConfigurationError(
                "a checkpoint with no parameters has not learned anything",
                checkpoint_id=self.checkpoint_id)

    @property
    def content_hash(self) -> str:
        """Tamper-evident identity over parameters *and* provenance."""
        payload = json.dumps({"model_kind": self.model_kind,
                              "version": self.version,
                              "parameters": jsonable(dict(self.parameters)),
                              "manifest": self.manifest.to_dict(),
                              "initialised_from": self.initialised_from},
                             sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def model_version(self) -> str:
        """What goes into `ForecastResult.model_version`.

        Includes the content hash prefix, so two checkpoints that differ only
        in their learned tables are distinguishable in a forecast record.
        """
        return f"{self.model_kind}/{self.version}/{self.content_hash[:12]}"

    @property
    def fresh(self) -> bool:
        return not self.initialised_from

    def to_dict(self) -> dict:
        return {"checkpoint_id": self.checkpoint_id,
                "model_kind": self.model_kind, "version": self.version,
                "manifest": self.manifest.to_dict(),
                "parameters": jsonable(dict(self.parameters)),
                "initialised_from": self.initialised_from,
                "schema_version": self.schema_version,
                "content_hash": self.content_hash,
                "model_version": self.model_version,
                "owner": "olympus",
                "provenance": "trained by an Olympus pipeline from an Olympus "
                              "dataset; no foreign weights"}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NativeCheckpoint":
        return cls(checkpoint_id=str(data["checkpoint_id"]),
                   model_kind=str(data["model_kind"]),
                   version=str(data["version"]),
                   manifest=TrainingManifest.from_dict(data["manifest"]),
                   parameters=data.get("parameters") or {},
                   initialised_from=str(data.get("initialised_from", "")),
                   schema_version=int(data.get("schema_version",
                                               CHECKPOINT_SCHEMA_VERSION)))


class CheckpointStore:
    """Durable, append-only checkpoint storage.

    No update and no delete. A checkpoint is what a forecast's `model_version`
    points at; being able to change one after the fact would make every record
    that cites it unverifiable.
    """

    def __init__(self, *, namespace: str = _NS, clock: Clock | None = None,
                 audit: Any = None):
        self.namespace = namespace
        self.clock = clock or default_clock()
        self.audit = audit

    def _store(self) -> KeyedStore:
        return KeyedStore(self.namespace)

    def save(self, checkpoint: NativeCheckpoint) -> NativeCheckpoint:
        """Write. Refuses to overwrite an existing id."""
        from olympus import proclock
        with proclock.lock(f"native-checkpoint-{checkpoint.checkpoint_id}"):
            if self._store().get((checkpoint.checkpoint_id,)) is not None:
                raise ConfigurationError(
                    "checkpoint_id already exists; write a new version rather "
                    "than replacing what a forecast record points at",
                    checkpoint_id=checkpoint.checkpoint_id)
            self._store().put((checkpoint.checkpoint_id,), checkpoint.to_dict())
        if self.audit is not None:
            try:
                from ..audit import EventType
                self.audit.record(EventType.MODEL_CHANGED,
                                  {**checkpoint.to_dict(), "action": "saved"},
                                  actor=checkpoint.manifest.trained_by,
                                  subject=checkpoint.checkpoint_id)
            except Exception:                            # noqa: BLE001
                pass
        return checkpoint

    def load(self, checkpoint_id: str) -> NativeCheckpoint | None:
        body = self._store().get((checkpoint_id,))
        if body is None:
            return None
        checkpoint = NativeCheckpoint.from_dict(body)
        stored = body.get("content_hash")
        if stored and stored != checkpoint.content_hash:
            raise ConfigurationError(
                "checkpoint content hash does not match its contents; the "
                "stored parameters or manifest were altered after writing",
                checkpoint_id=checkpoint_id, stored=stored,
                computed=checkpoint.content_hash)
        return checkpoint

    def ids(self) -> list[str]:
        return sorted(parts[0] for parts in self._store().keys())

    def all(self) -> list[NativeCheckpoint]:
        return [c for i in self.ids() if (c := self.load(i)) is not None]

    def continue_from(self, checkpoint_id: Any) -> str:
        """Validate a continuation origin against what is actually stored."""
        return assert_olympus_origin(checkpoint_id, known=self.ids())

    def __repr__(self) -> str:                          # pragma: no cover
        return f"CheckpointStore(namespace={self.namespace!r})"


def code_version() -> str:
    """The current commit, or "" when unavailable.

    Best-effort and never raises: a checkpoint written outside a git checkout
    should still be written, with the gap recorded in `manifest.gaps`, rather
    than refused.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:                                    # noqa: BLE001
        return ""


__all__ = ["CHECKPOINT_SCHEMA_VERSION", "ForeignWeightsRefused",
           "assert_olympus_origin", "dependency_versions", "TrainingManifest",
           "NativeCheckpoint", "CheckpointStore", "code_version"]
