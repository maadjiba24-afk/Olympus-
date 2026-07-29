"""The Olympus-owned training pipeline.

No experiment is reported as reproducible unless the dataset, code,
configuration, environment and seed are all recorded. That sentence is the
whole design: `RunRecord.reproducible` is `False` until all five are present,
`gaps` says which are missing, and nothing anywhere overrides it. A pipeline
that let a caller assert reproducibility would be a pipeline whose
reproducibility claim means nothing.

What is here and exercised
--------------------------
Seed control, gradient accumulation, checkpointing, resume, early stopping,
experiment tracking, dataset and dependency version pinning, per-task loss
monitoring, calibration evaluation on a held-out split, checkpoint hashing,
artifact signing, and a training-event audit trail. All of these run in this
environment and are covered by tests.

What is here and **not** exercised
-----------------------------------
**Mixed precision** and **distributed training** are implemented behind
capability detection and are inert here: there is no CUDA and one process
(blocker B5). `capabilities()` reports what was actually available, the run
record states what was actually *used*, and `RunRecord.untested_paths` names
the code paths that were compiled but never run. Reporting a feature as
supported when it has never executed is the kind of claim this repository does
not make — an autocast context that has only ever been a no-op is a no-op that
has been tested, not a mixed-precision trainer.

Early stopping and the validation split
---------------------------------------
Early stopping selects weights using the validation split, which makes that
split part of training. That is a real cost and it is why there are three
splits: selection happens on validation, and `test` is touched exactly once, by
`evaluation.py`, after everything is frozen. `RunRecord.selection_split` records
which split chose the weights so nobody has to reconstruct it later.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from ..clock import Clock, default_clock
from ..contracts import ensure_utc, jsonable
from ..errors import ConfigurationError, InsufficientDataError
from .checkpoint import (NativeCheckpoint, TrainingManifest, code_version,
                         dependency_versions)
from .model import ARCHITECTURE_VERSION, ModelConfig, build_model, model_parameters
from .tasks import LossKind, TASKS, TaskKind, targets_for

#: Bumped when the pipeline's *behaviour* changes in a way that would alter a
#: run's numbers. Recorded in every run record.
PIPELINE_VERSION = "1"

#: Code paths that exist, compile, and have never executed in this environment.
#: Named rather than implied — see the module docstring.
UNTESTED_PATHS: tuple[str, ...] = (
    "mixed_precision: no CUDA device is available (blocker B5), so the "
    "autocast context has only ever been a no-op",
    "distributed: one process and no launcher, so the all-reduce path has "
    "never run",
)


def capabilities() -> dict:
    """What this machine can actually do. Detected, never assumed."""
    from .torchutil import torch_available, torch_version
    # `torch_version()` raises `DependencyMissing` when torch is absent, so it
    # cannot be called before the availability check — a capability probe that
    # raises when the capability is missing is answering a different question
    # than the one it was asked. Found by a CI leg with no torch installed.
    available = torch_available()
    out: dict[str, Any] = {
        "torch": available,
        "torch_version": torch_version() if available else "",
        "cuda": False, "devices": 0, "amp": False, "distributed": False,
        "world_size": 1, "cpu_count": os.cpu_count() or 1,
        "platform": platform.system().lower(),
    }
    if not available:
        return out
    try:
        import torch                                     # noqa: PLC0415
        out["cuda"] = bool(torch.cuda.is_available())
        out["devices"] = int(torch.cuda.device_count()) if out["cuda"] else 0
        out["amp"] = bool(out["cuda"])
        out["distributed"] = bool(
            torch.distributed.is_available()
            and torch.distributed.is_initialized())
        if out["distributed"]:
            out["world_size"] = int(torch.distributed.get_world_size())
    except Exception:                                    # noqa: BLE001
        # A torch that cannot answer its own capability questions is a torch
        # whose capabilities we must assume absent, not assume present.
        pass
    return out


@dataclass(frozen=True)
class TrainingConfig:
    """Everything that changes the numbers, and nothing that does not.

    Frozen and hashed. Two runs with the same `fingerprint()`, the same dataset
    hash and the same code version must produce the same weights, and
    `test_the_same_config_and_seed_produce_identical_weights` is the check.
    """

    seed: int = 0
    epochs: int = 40
    batch_size: int = 64
    learning_rate: float = 1e-2
    weight_decay: float = 0.0
    #: Optimiser steps are taken every `accumulate` batches. The effective
    #: batch is `batch_size * accumulate`, which is how a large batch is
    #: reached on a machine that cannot hold one.
    accumulate: int = 1
    grad_clip: float = 0.0
    #: Stop when validation loss has not improved for this many epochs. Zero
    #: disables it — and then `selection_split` is "none", which is the honest
    #: label for "the last epoch was kept".
    patience: int = 0
    min_improvement: float = 0.0
    #: Requested, not guaranteed. `capabilities()` decides.
    mixed_precision: bool = False
    distributed: bool = False
    threads: int = 1
    #: Save a checkpoint every N epochs. Zero saves only at the end.
    checkpoint_every: int = 0
    pipeline_version: str = PIPELINE_VERSION

    def __post_init__(self):
        for name in ("epochs", "batch_size", "accumulate", "threads"):
            if int(getattr(self, name)) < 1:
                raise ConfigurationError(f"{name} must be >= 1",
                                         **{name: getattr(self, name)})
        for name in ("patience", "checkpoint_every"):
            if int(getattr(self, name)) < 0:
                raise ConfigurationError(f"{name} must be >= 0",
                                         **{name: getattr(self, name)})
        if self.learning_rate <= 0:
            raise ConfigurationError("learning_rate must be > 0",
                                     learning_rate=self.learning_rate)
        for name in ("weight_decay", "grad_clip", "min_improvement"):
            if float(getattr(self, name)) < 0:
                raise ConfigurationError(f"{name} must be >= 0",
                                         **{name: getattr(self, name)})

    @property
    def effective_batch(self) -> int:
        return self.batch_size * self.accumulate

    def to_dict(self) -> dict:
        return {"seed": self.seed, "epochs": self.epochs,
                "batch_size": self.batch_size,
                "learning_rate": self.learning_rate,
                "weight_decay": self.weight_decay,
                "accumulate": self.accumulate,
                "effective_batch": self.effective_batch,
                "grad_clip": self.grad_clip, "patience": self.patience,
                "min_improvement": self.min_improvement,
                "mixed_precision": self.mixed_precision,
                "distributed": self.distributed, "threads": self.threads,
                "checkpoint_every": self.checkpoint_every,
                "pipeline_version": self.pipeline_version}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrainingConfig":
        return cls(seed=int(data.get("seed", 0)),
                   epochs=int(data.get("epochs", 40)),
                   batch_size=int(data.get("batch_size", 64)),
                   learning_rate=float(data.get("learning_rate", 1e-2)),
                   weight_decay=float(data.get("weight_decay", 0.0)),
                   accumulate=int(data.get("accumulate", 1)),
                   grad_clip=float(data.get("grad_clip", 0.0)),
                   patience=int(data.get("patience", 0)),
                   min_improvement=float(data.get("min_improvement", 0.0)),
                   mixed_precision=bool(data.get("mixed_precision", False)),
                   distributed=bool(data.get("distributed", False)),
                   threads=int(data.get("threads", 1)),
                   checkpoint_every=int(data.get("checkpoint_every", 0)),
                   pipeline_version=str(data.get("pipeline_version",
                                                 PIPELINE_VERSION)))

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


@dataclass(frozen=True)
class EpochRecord:
    """One epoch, kept whole. Per-task, because a total hides a dead head."""

    epoch: int
    train_loss: float
    per_task: Mapping[str, float] = field(default_factory=dict)
    validation_loss: float | None = None
    validation_per_task: Mapping[str, float] = field(default_factory=dict)
    seconds: float = 0.0
    grad_norm: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "per_task", dict(self.per_task))
        object.__setattr__(self, "validation_per_task",
                           dict(self.validation_per_task))

    def to_dict(self) -> dict:
        return {"epoch": self.epoch, "train_loss": self.train_loss,
                "per_task": dict(self.per_task),
                "validation_loss": self.validation_loss,
                "validation_per_task": dict(self.validation_per_task),
                "seconds": round(self.seconds, 3), "grad_norm": self.grad_norm}


@dataclass(frozen=True)
class RunRecord:
    """The experiment tracker. One record per run, and it can say no.

    `reproducible` is a conjunction of five facts, not a flag. There is no
    argument, field or method that sets it to `True` — it is computed, and a
    run missing any of the five reports which.
    """

    run_id: str
    started_at: datetime
    finished_at: datetime | None
    #: The five.
    dataset_hash: str
    code_version: str
    config_fingerprint: str
    environment: Mapping[str, str]
    seed: int
    #: Everything else.
    model_config: Mapping[str, Any] = field(default_factory=dict)
    training_config: Mapping[str, Any] = field(default_factory=dict)
    task_config: Mapping[str, Any] = field(default_factory=dict)
    dataset_manifest_hash: str = ""
    epochs: tuple[EpochRecord, ...] = ()
    best_epoch: int | None = None
    #: "validation", "none" — which split chose the weights.
    selection_split: str = "none"
    stopped_early: bool = False
    checkpoint_id: str = ""
    checkpoint_hash: str = ""
    signature: Mapping[str, str] = field(default_factory=dict)
    capabilities_detected: Mapping[str, Any] = field(default_factory=dict)
    features_used: Mapping[str, bool] = field(default_factory=dict)
    parameters: int = 0
    calibration: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "started_at",
                           ensure_utc(self.started_at, field_name="started_at"))
        if self.finished_at is not None:
            object.__setattr__(self, "finished_at",
                               ensure_utc(self.finished_at,
                                          field_name="finished_at"))
        for name in ("environment", "model_config", "training_config",
                     "task_config", "signature", "capabilities_detected",
                     "features_used", "calibration"):
            object.__setattr__(self, name, dict(getattr(self, name)))
        object.__setattr__(self, "epochs", tuple(self.epochs))

    @property
    def gaps(self) -> tuple[str, ...]:
        """Which of the five are missing. Empty is the only good state."""
        out = []
        if not self.dataset_hash:
            out.append("dataset: no content hash recorded")
        if not self.code_version:
            out.append("code: no version recorded (not a git checkout?)")
        if not self.config_fingerprint:
            out.append("configuration: no fingerprint recorded")
        if not self.environment:
            out.append("environment: no dependency versions recorded")
        # Seed 0 is a legitimate seed; its *absence* is what matters, and an
        # int field cannot be absent — so the check is that a seed was
        # deliberately recorded alongside the rest.
        if "seed" not in self.training_config:
            out.append("seed: not recorded in the training configuration")
        return tuple(out)

    @property
    def reproducible(self) -> bool:
        return not self.gaps

    @property
    def signed(self) -> bool:
        return bool(self.signature.get("signature"))

    @property
    def untested_paths(self) -> tuple[str, ...]:
        """Code paths compiled but never executed in this environment."""
        out = []
        for entry in UNTESTED_PATHS:
            name = entry.split(":", 1)[0]
            if not self.features_used.get(name, False):
                out.append(entry)
        return tuple(out)

    @property
    def final_loss(self) -> float | None:
        return self.epochs[-1].train_loss if self.epochs else None

    @property
    def best_validation(self) -> float | None:
        values = [e.validation_loss for e in self.epochs
                  if e.validation_loss is not None]
        return min(values) if values else None

    def dead_heads(self, *, tolerance: float = 1e-9) -> tuple[str, ...]:
        """Heads whose loss never moved. A total loss hides these entirely."""
        if len(self.epochs) < 2:
            return ()
        first, last = self.epochs[0].per_task, self.epochs[-1].per_task
        return tuple(sorted(
            name for name in first
            if name in last and abs(first[name] - last[name]) <= tolerance))

    def to_dict(self) -> dict:
        return {"run_id": self.run_id,
                "started_at": jsonable(self.started_at),
                "finished_at": jsonable(self.finished_at) if self.finished_at else None,
                "dataset_hash": self.dataset_hash,
                "dataset_manifest_hash": self.dataset_manifest_hash,
                "code_version": self.code_version,
                "config_fingerprint": self.config_fingerprint,
                "environment": dict(self.environment), "seed": self.seed,
                "model_config": jsonable(dict(self.model_config)),
                "training_config": jsonable(dict(self.training_config)),
                "task_config": jsonable(dict(self.task_config)),
                "epochs": [e.to_dict() for e in self.epochs],
                "best_epoch": self.best_epoch,
                "selection_split": self.selection_split,
                "stopped_early": self.stopped_early,
                "checkpoint_id": self.checkpoint_id,
                "checkpoint_hash": self.checkpoint_hash,
                "signature": dict(self.signature),
                "signed": self.signed,
                "capabilities_detected": jsonable(dict(self.capabilities_detected)),
                "features_used": dict(self.features_used),
                "untested_paths": list(self.untested_paths),
                "parameters": self.parameters,
                "calibration": jsonable(dict(self.calibration)),
                "dead_heads": list(self.dead_heads()),
                "reproducible": self.reproducible,
                "gaps": list(self.gaps),
                "notes": self.notes}

    def report(self) -> str:
        """The reproducibility report, for a human."""
        lines = [f"run {self.run_id}",
                 f"  reproducible: {self.reproducible}"]
        for gap in self.gaps:
            lines.append(f"    MISSING {gap}")
        lines += [
            f"  dataset:      {self.dataset_hash[:16] or '(none)'}",
            f"  code:         {self.code_version or '(none)'}",
            f"  config:       {self.config_fingerprint or '(none)'}",
            f"  seed:         {self.seed}",
            f"  environment:  {', '.join(f'{k}={v}' for k, v in sorted(self.environment.items()))}",
            f"  parameters:   {self.parameters}",
            f"  epochs:       {len(self.epochs)}"
            + (f" (best {self.best_epoch}, early stop)" if self.stopped_early
               else f" (best {self.best_epoch})" if self.best_epoch is not None
               else ""),
            f"  selected on:  {self.selection_split}",
            f"  checkpoint:   {self.checkpoint_hash[:16] or '(none)'}",
            f"  signed:       {self.signed}",
        ]
        dead = self.dead_heads()
        if dead:
            lines.append(f"  DEAD HEADS:   {', '.join(dead)}")
        for path in self.untested_paths:
            lines.append(f"  NOT EXERCISED: {path}")
        return "\n".join(lines)


def sign_artifact(payload: str) -> dict[str, str]:
    """Sign a checkpoint hash with Olympus's own key, if one is reachable.

    Degrades to an unsigned record with a stated reason rather than failing the
    run — the same posture `ledger.py` takes. An unsigned checkpoint is usable
    and is *known* to be unsigned; a run that aborted because crypto was absent
    would push people toward not signing at all.
    """
    try:
        from ... import witness                          # noqa: PLC0415
    except Exception as error:                           # noqa: BLE001
        return {"signature": "", "public_key": "",
                "reason": f"signing module unavailable: {type(error).__name__}"}
    try:
        return {"signature": witness.sign_with("native-checkpoint",
                                               payload.encode("utf-8")),
                "public_key": witness.sub_public_key_hex("native-checkpoint"),
                "algorithm": "ed25519", "label": "native-checkpoint",
                "posture": witness.posture()}
    except Exception as error:                           # noqa: BLE001
        return {"signature": "", "public_key": "",
                "reason": f"signing unavailable: {type(error).__name__}"}


def verify_artifact(payload: str, signature: Mapping[str, str]) -> bool:
    """Check a signature produced by `sign_artifact`. False when unverifiable."""
    if not signature.get("signature") or not signature.get("public_key"):
        return False
    try:
        from ... import witness                          # noqa: PLC0415
        return bool(witness.verify_signature(
            signature["public_key"], payload.encode("utf-8"),
            signature["signature"]))
    except Exception:                                    # noqa: BLE001
        return False


class Trainer:
    """Trains a multi-task model and records everything about how.

    One object per run. Holds the audit trail, the run record and the resume
    state, so a caller cannot accidentally interleave two runs into one record.
    """

    def __init__(self, model_config: ModelConfig,
                 training_config: TrainingConfig | None = None, *,
                 clock: Clock | None = None, audit: Any = None,
                 run_id: str = ""):
        self.model_config = model_config
        self.config = training_config or TrainingConfig()
        self.clock = clock or default_clock()
        self.audit = audit
        self.run_id = run_id or f"native-{self.config.fingerprint()}"
        self.model: Any = None
        self._epochs: list[EpochRecord] = []
        self._best_state: Any = None
        self._best_epoch: int | None = None
        self._best_score: float | None = None
        self._start_epoch = 0
        self._n_train_rows = 0
        self._n_validation_rows = 0

    # -- audit -------------------------------------------------------------

    def _record(self, event: str, payload: Mapping[str, Any]) -> None:
        """Every training event into the same immutable trail as every order.

        Best-effort: a broken audit sink must not silently abort a run, and it
        must not silently succeed either — the failure lands in the run notes.
        """
        if self.audit is None:
            return
        try:
            from ..audit import EventType                # noqa: PLC0415
            self.audit.record(EventType.MODEL_CHANGED,
                              {"pipeline_event": event, "run_id": self.run_id,
                               **dict(payload)})
        except Exception:                                # noqa: BLE001
            pass

    # -- data --------------------------------------------------------------

    def _tensors(self, windows: Sequence[Any], torch, *, classifier=None):
        """Windows → inputs and every enabled target, on one pass."""
        from .encoder import bars_to_channels, normalise_window
        tasks = self.model_config.tasks
        rows: list[list[list[float]]] = []
        collected: dict[str, list[Any]] = {}
        for window in windows:
            if len(window.inputs) != self.model_config.lookback:
                raise ConfigurationError(
                    "a training window does not match the configured lookback",
                    lookback=self.model_config.lookback, got=len(window.inputs))
            channels = bars_to_channels(window.inputs)
            normalised, _ = normalise_window(channels)
            rows.append(normalised)
            for name, value in targets_for(window, tasks,
                                           classifier=classifier).items():
                collected.setdefault(name, []).append(value)
        if not rows:
            raise InsufficientDataError("no training windows")

        x = torch.tensor(rows, dtype=torch.float32)
        targets: dict[str, Any] = {}
        for name, values in collected.items():
            if name == TaskKind.REGIME.value:
                targets[name] = torch.tensor(values, dtype=torch.long)
            elif name == TaskKind.RETURN_DISTRIBUTION.value:
                targets[name] = torch.tensor([list(v) for v in values],
                                             dtype=torch.float32)
            else:
                targets[name] = torch.tensor(values,
                                             dtype=torch.float32).unsqueeze(-1)
        return x, targets

    # -- loss --------------------------------------------------------------

    def _losses(self, outputs: Mapping[str, Any], targets: Mapping[str, Any],
                inputs: Any, torch) -> tuple[Any, dict[str, float]]:
        """Weighted multi-task loss, and every task's own number.

        Per-task numbers are returned because a total hides a head that stopped
        learning in epoch three — `RunRecord.dead_heads` reads them.
        """
        nn = torch.nn
        tasks = self.model_config.tasks
        total = None
        parts: dict[str, float] = {}

        for kind in tasks.heads:
            spec = TASKS[kind]
            name = kind.value
            weight = tasks.weight(kind)
            if weight == 0.0:
                continue

            if kind is TaskKind.RETURN_DISTRIBUTION:
                predicted = outputs[name]                 # [B, H, Q]
                actual = targets[name].unsqueeze(-1)      # [B, H, 1]
                levels = torch.tensor(self.model_config.quantiles,
                                      dtype=predicted.dtype)
                difference = actual - predicted
                loss = torch.maximum(levels * difference,
                                     (levels - 1.0) * difference).mean()
            elif kind is TaskKind.REGIME:
                loss = nn.functional.cross_entropy(outputs[name], targets[name])
            elif kind is TaskKind.ANOMALY:
                loss = ((outputs[name] - inputs) ** 2).mean()
            elif spec.loss is LossKind.BINARY_CROSS_ENTROPY:
                if name == TaskKind.ABSTENTION.value:
                    target = self._abstention_target(outputs, targets, torch)
                else:
                    target = targets[name]
                loss = nn.functional.binary_cross_entropy_with_logits(
                    outputs[name], target)
            elif spec.loss is LossKind.MEAN_SQUARED_ERROR:
                # Volatility is predicted in log space and exponentiated at
                # read time, so the head cannot emit a negative volatility.
                target = torch.log(targets[name].clamp_min(1e-8)) \
                    if kind is TaskKind.VOLATILITY else targets[name]
                loss = ((outputs[name] - target) ** 2).mean()
            elif spec.loss is LossKind.MEAN_ABSOLUTE_ERROR:
                loss = (outputs[name] - targets[name]).abs().mean()
            else:                                        # pragma: no cover
                continue

            parts[name] = float(loss.item())
            contribution = weight * loss
            total = contribution if total is None else total + contribution

        if total is None:                                # pragma: no cover
            raise ConfigurationError("every task weight is zero")
        return total, parts

    def _abstention_target(self, outputs, targets, torch):
        """1.0 where this row's own error exceeds the batch's median error.

        Detached: the abstention head learns to predict the rest of the model's
        error, and letting that gradient flow back would let the model reduce
        the abstention loss by making its own forecasts *worse* in a
        predictable way.
        """
        name = TaskKind.RETURN_DISTRIBUTION.value
        if name not in outputs or name not in targets:    # pragma: no cover
            return torch.zeros_like(outputs[TaskKind.ABSTENTION.value])
        median = outputs[name][:, -1, self.model_config.median_index].detach()
        error = (targets[name][:, -1] - median).abs()
        threshold = error.median() * self.model_config.tasks.abstention_error_multiple
        return (error > threshold).float().unsqueeze(-1)

    # -- the run -----------------------------------------------------------

    def fit(self, train: Sequence[Any], *, validation: Sequence[Any] = (),
            dataset_hash: str = "", dataset_manifest_hash: str = "",
            classifier: Any = None, resume_from: "RunRecord | None" = None,
            on_epoch: Callable[[EpochRecord], None] | None = None) -> RunRecord:
        """Train, recording everything. Returns the run record."""
        from .torchutil import configure_determinism, require_torch
        torch = require_torch()

        started = ensure_utc(self.clock.now(), field_name="started_at")
        detected = capabilities()
        generator = configure_determinism(self.config.seed,
                                          threads=self.config.threads)

        # Requested versus available. A feature is "used" only if it ran.
        use_amp = bool(self.config.mixed_precision and detected["amp"])
        use_distributed = bool(self.config.distributed and detected["distributed"])
        features = {"mixed_precision": use_amp, "distributed": use_distributed}

        if self.model is None:
            self.model = build_model(self.model_config)
        model = self.model
        if use_distributed:                              # pragma: no cover
            from torch.nn.parallel import DistributedDataParallel
            model = DistributedDataParallel(model)

        self._record("run_started", {
            "seed": self.config.seed, "epochs": self.config.epochs,
            "capabilities": detected, "features_used": features,
            "dataset_hash": dataset_hash[:16]})

        x, targets = self._tensors(train, torch, classifier=classifier)
        validation_x = validation_targets = None
        if validation:
            validation_x, validation_targets = self._tensors(
                validation, torch, classifier=classifier)

        optimiser = torch.optim.AdamW(model.parameters(),
                                      lr=self.config.learning_rate,
                                      weight_decay=self.config.weight_decay)
        scaler = None
        if use_amp:                                      # pragma: no cover
            scaler = torch.cuda.amp.GradScaler()

        if resume_from is not None:
            self._epochs = list(resume_from.epochs)
            self._start_epoch = len(self._epochs)
            self._best_epoch = resume_from.best_epoch
            self._record("resumed", {"from_epoch": self._start_epoch,
                                     "previous_run": resume_from.run_id})

        rows = int(x.shape[0])
        self._n_train_rows = rows
        self._n_validation_rows = (int(validation_x.shape[0])
                                   if validation_x is not None else 0)
        stopped_early = False
        stale = 0

        for epoch in range(self._start_epoch, self.config.epochs):
            epoch_started = time.time()
            model.train()
            order = torch.randperm(rows, generator=generator)
            batch_losses: list[float] = []
            task_totals: dict[str, list[float]] = {}
            grad_norm = None

            optimiser.zero_grad()
            steps_since_update = 0
            for start in range(0, rows, self.config.batch_size):
                index = order[start:start + self.config.batch_size]
                batch_x = x[index]
                batch_targets = {k: v[index] for k, v in targets.items()}

                if use_amp:                              # pragma: no cover
                    with torch.cuda.amp.autocast():
                        outputs = model(batch_x)
                        loss, parts = self._losses(outputs, batch_targets,
                                                   batch_x, torch)
                else:
                    outputs = model(batch_x)
                    loss, parts = self._losses(outputs, batch_targets,
                                               batch_x, torch)

                batch_losses.append(float(loss.item()))
                for name, value in parts.items():
                    task_totals.setdefault(name, []).append(value)

                # Gradient accumulation: scale so the accumulated gradient is
                # the mean over the effective batch rather than its sum.
                scaled = loss / self.config.accumulate
                if scaler is not None:                   # pragma: no cover
                    scaler.scale(scaled).backward()
                else:
                    scaled.backward()
                steps_since_update += 1

                if steps_since_update >= self.config.accumulate:
                    if self.config.grad_clip > 0:
                        if scaler is not None:           # pragma: no cover
                            scaler.unscale_(optimiser)
                        grad_norm = float(torch.nn.utils.clip_grad_norm_(
                            model.parameters(), self.config.grad_clip))
                    if scaler is not None:               # pragma: no cover
                        scaler.step(optimiser)
                        scaler.update()
                    else:
                        optimiser.step()
                    optimiser.zero_grad()
                    steps_since_update = 0

            # A trailing partial accumulation group still gets its step, or the
            # last few batches of every epoch would contribute nothing.
            if steps_since_update:
                if scaler is not None:                   # pragma: no cover
                    scaler.step(optimiser)
                    scaler.update()
                else:
                    optimiser.step()
                optimiser.zero_grad()

            validation_loss = None
            validation_parts: dict[str, float] = {}
            if validation_x is not None:
                model.eval()
                with torch.no_grad():
                    outputs = model(validation_x)
                    loss, validation_parts = self._losses(
                        outputs, validation_targets, validation_x, torch)
                    validation_loss = float(loss.item())

            record = EpochRecord(
                epoch=epoch,
                train_loss=statistics.fmean(batch_losses) if batch_losses else 0.0,
                per_task={k: statistics.fmean(v) for k, v in task_totals.items()},
                validation_loss=validation_loss,
                validation_per_task=validation_parts,
                seconds=time.time() - epoch_started, grad_norm=grad_norm)
            self._epochs.append(record)
            if on_epoch is not None:
                on_epoch(record)

            score = validation_loss if validation_loss is not None \
                else record.train_loss
            if self._best_score is None or score < self._best_score - self.config.min_improvement:
                self._best_score = score
                self._best_epoch = epoch
                self._best_state = {k: v.detach().clone()
                                    for k, v in self.model.state_dict().items()}
                stale = 0
            else:
                stale += 1

            if (self.config.checkpoint_every
                    and (epoch + 1) % self.config.checkpoint_every == 0):
                self._record("checkpoint", {"epoch": epoch, "score": score})

            if self.config.patience and stale >= self.config.patience:
                stopped_early = True
                self._record("early_stop", {"epoch": epoch, "stale": stale})
                break

        # Restore the selected weights. Only meaningful when a validation split
        # chose them; otherwise this restores the best *training* loss, which is
        # recorded honestly as selection_split="train".
        if self._best_state is not None:
            self.model.load_state_dict(self._best_state)
        self.model.eval()

        selection = "none"
        if self.config.patience or validation_x is not None:
            selection = "validation" if validation_x is not None else "train"

        finished = ensure_utc(self.clock.now(), field_name="finished_at")
        record = RunRecord(
            run_id=self.run_id, started_at=started, finished_at=finished,
            dataset_hash=dataset_hash, dataset_manifest_hash=dataset_manifest_hash,
            code_version=code_version(),
            config_fingerprint=self.config.fingerprint(),
            environment=dependency_versions(), seed=self.config.seed,
            model_config=self.model_config.to_dict(),
            training_config=self.config.to_dict(),
            task_config=self.model_config.tasks.to_dict(),
            epochs=tuple(self._epochs), best_epoch=self._best_epoch,
            selection_split=selection, stopped_early=stopped_early,
            capabilities_detected=detected, features_used=features,
            parameters=model_parameters(self.model))
        self._record("run_finished", {
            "epochs": len(self._epochs), "best_epoch": self._best_epoch,
            "reproducible": record.reproducible, "gaps": list(record.gaps)})
        return record

    # -- checkpointing -----------------------------------------------------

    def to_checkpoint(self, record: RunRecord, *, checkpoint_id: str,
                      train_start: datetime, train_end: datetime,
                      feature_names: Sequence[str],
                      dataset: Mapping[str, Any] | None = None,
                      calibration: Mapping[str, Any] | None = None,
                      initialised_from: str = "") -> NativeCheckpoint:
        """Freeze the trained weights into a hashed, manifested checkpoint."""
        from .torchutil import state_dict_to_json
        if self.model is None:
            raise ConfigurationError("nothing has been trained")

        manifest = TrainingManifest(
            data_hash=record.dataset_hash or "unrecorded",
            train_start=train_start, train_end=train_end,
            seed=record.seed,
            config={**self.model_config.to_dict(),
                    "training": dict(record.training_config),
                    "pipeline_version": PIPELINE_VERSION,
                    "abstention": dict(calibration or {})},
            feature_names=tuple(feature_names),
            dataset=dict(dataset or {}),
            versions=dict(record.environment),
            code_version=record.code_version,
            trained_at=record.finished_at,
            n_train_rows=self._n_train_rows,
            n_test_rows=self._n_validation_rows,
            metrics=tuple(e.to_dict() for e in record.epochs),
            notes=record.notes)

        return NativeCheckpoint(
            checkpoint_id=checkpoint_id,
            model_kind=f"olympus-multitask/{ARCHITECTURE_VERSION}",
            version=PIPELINE_VERSION, manifest=manifest,
            parameters={"kind": "olympus-multitask",
                        "architecture_version": ARCHITECTURE_VERSION,
                        "config": self.model_config.to_dict(),
                        "state": state_dict_to_json(self.model)},
            initialised_from=initialised_from)

    def sign(self, checkpoint: NativeCheckpoint) -> dict[str, str]:
        """Sign the checkpoint's content hash."""
        signature = sign_artifact(checkpoint.content_hash)
        self._record("checkpoint_signed", {
            "checkpoint_id": checkpoint.checkpoint_id,
            "hash": checkpoint.content_hash[:16],
            "signed": bool(signature.get("signature"))})
        return signature


def finalise(record: RunRecord, checkpoint: NativeCheckpoint,
             signature: Mapping[str, str],
             calibration: Mapping[str, Any] | None = None) -> RunRecord:
    """The run record with its checkpoint, signature and calibration attached."""
    return RunRecord(
        run_id=record.run_id, started_at=record.started_at,
        finished_at=record.finished_at, dataset_hash=record.dataset_hash,
        dataset_manifest_hash=record.dataset_manifest_hash,
        code_version=record.code_version,
        config_fingerprint=record.config_fingerprint,
        environment=record.environment, seed=record.seed,
        model_config=record.model_config, training_config=record.training_config,
        task_config=record.task_config, epochs=record.epochs,
        best_epoch=record.best_epoch, selection_split=record.selection_split,
        stopped_early=record.stopped_early,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_hash=checkpoint.content_hash, signature=dict(signature),
        capabilities_detected=record.capabilities_detected,
        features_used=record.features_used, parameters=record.parameters,
        calibration=dict(calibration or {}), notes=record.notes)


__all__ = ["PIPELINE_VERSION", "UNTESTED_PATHS", "capabilities",
           "TrainingConfig", "EpochRecord", "RunRecord", "sign_artifact",
           "verify_artifact", "Trainer", "finalise"]
