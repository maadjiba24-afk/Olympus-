"""The fifteen tasks, what each one needs, and which can be trained here.

A multi-task model is a claim about *supervision*: for every head, something has
to say what the right answer was. Most of the ways multi-task models go wrong
are not architectural — they are a head trained against a target nobody
checked, or a head enabled in a checkpoint that had no data for it and which
therefore learned the mean and reported it confidently.

So this module is the registry, and the thing it enforces is that a task cannot
be enabled without a target extractor and the channels its target needs. A
`TaskConfig` that enables `spread` in this environment is refused at
construction, because no bid or ask is ingested (blocker B1) and a spread head
trained on nothing is worse than no spread head.

The four kinds of task
----------------------
**Supervised heads** (`return_distribution`, `direction`, `volatility`,
`regime`, `drawdown`) have a target computable from a `data.Window`. They are
the five that can actually be trained from what Olympus can obtain.

**Blocked heads** (`liquidity`, `spread`, `execution_cost`, `cross_asset`,
`multi_timeframe`) are implemented and permanently available, and refuse to
enable until their inputs exist. Each states what it is waiting for.

**Self-supervised** (`anomaly`) needs no label: its target is its own input.

**Derived outputs** (`price_quantiles`, `ood`, `calibration`, `abstention`) are
not independent heads at all, and saying so is the point. Price quantiles are a
deterministic transform of return quantiles given the anchor close; giving them
their own head would let the two disagree, and a model whose price band
contradicts its own return band is worse than one with a single band. OOD and
calibration are computed *about* the model rather than *by* it. Abstention is
the one borderline case — it has a real head, trained on the model's own error
— and it is listed here with that stated.

Loss weighting
--------------
Explicit, per task, recorded in the manifest. There is no automatic
uncertainty-weighting scheme: those exist and work, and they also make "why did
this head stop learning" unanswerable from the config alone, which on a
four-core budget with no room for ablations is the wrong trade.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..contracts import Regime
from ..errors import ConfigurationError

#: Bumped when a task's *target definition* changes. A checkpoint trained
#: against an older definition is then detectable rather than silently
#: mis-scored.
TASK_SCHEMA_VERSION = 1

#: Regime levels the classification head predicts, in a fixed order. Fixed
#: because the head's output index means one of these and a reordering would
#: silently relabel every prediction.
REGIME_CLASSES: tuple[str, ...] = (
    Regime.UNKNOWN.value, Regime.TRENDING_UP.value, Regime.TRENDING_DOWN.value,
    Regime.RANGING.value, Regime.HIGH_VOLATILITY.value, Regime.CRISIS.value)


class TaskKind(str, Enum):
    """The fifteen. Values are stable identifiers written into checkpoints."""

    RETURN_DISTRIBUTION = "return_distribution"
    DIRECTION = "direction"
    PRICE_QUANTILES = "price_quantiles"
    VOLATILITY = "volatility"
    REGIME = "regime"
    DRAWDOWN = "drawdown"
    LIQUIDITY = "liquidity"
    SPREAD = "spread"
    EXECUTION_COST = "execution_cost"
    ANOMALY = "anomaly"
    OOD = "ood"
    CALIBRATION = "calibration"
    ABSTENTION = "abstention"
    CROSS_ASSET = "cross_asset"
    MULTI_TIMEFRAME = "multi_timeframe"


class TaskClass(str, Enum):
    """How a task gets its answer. Determines whether it can be enabled."""

    #: A head with a target computable from a training window.
    SUPERVISED = "supervised"
    #: A head whose target is its own input.
    SELF_SUPERVISED = "self_supervised"
    #: A head whose target needs data this environment cannot obtain.
    BLOCKED = "blocked"
    #: Not a head. Computed from another head's output, or about the model.
    DERIVED = "derived"


class LossKind(str, Enum):
    PINBALL = "pinball"
    BINARY_CROSS_ENTROPY = "binary_cross_entropy"
    CROSS_ENTROPY = "cross_entropy"
    MEAN_SQUARED_ERROR = "mean_squared_error"
    MEAN_ABSOLUTE_ERROR = "mean_absolute_error"
    #: No loss: the task contributes nothing to the objective.
    NONE = "none"


@dataclass(frozen=True)
class TaskSpec:
    """One task: what it emits, what supervises it, and what it needs."""

    kind: TaskKind
    task_class: TaskClass
    summary: str
    loss: LossKind
    #: How many scalars per horizon step. `0` means the head emits one vector
    #: for the whole window rather than per step.
    outputs_per_step: int = 1
    #: `True` when the head emits one vector for the window, not per step.
    window_level: bool = False
    #: Channels the *target* needs. Checked against the schema before enabling.
    requires_channels: tuple[str, ...] = ()
    #: Default weight in the multi-task objective.
    default_weight: float = 1.0
    #: For a BLOCKED task: what would have to exist.
    blocked_on: str = ""
    #: For a DERIVED task: what it is derived from.
    derived_from: tuple[TaskKind, ...] = ()
    notes: str = ""

    def __post_init__(self):
        object.__setattr__(self, "kind", TaskKind(self.kind))
        object.__setattr__(self, "task_class", TaskClass(self.task_class))
        object.__setattr__(self, "loss", LossKind(self.loss))
        object.__setattr__(self, "requires_channels", tuple(self.requires_channels))
        object.__setattr__(self, "derived_from", tuple(self.derived_from))
        if not self.summary.strip():
            raise ConfigurationError("a task must describe itself",
                                     task=self.kind.value)
        if self.task_class is TaskClass.BLOCKED and not self.blocked_on:
            raise ConfigurationError(
                "a blocked task must state what it is waiting for; 'blocked' "
                "with no reason is indistinguishable from unimplemented",
                task=self.kind.value)
        if self.task_class is TaskClass.DERIVED and not self.derived_from:
            raise ConfigurationError(
                "a derived task must name what it is derived from",
                task=self.kind.value)
        if self.task_class is TaskClass.DERIVED and self.loss is not LossKind.NONE:
            raise ConfigurationError(
                "a derived task has no head and therefore no loss",
                task=self.kind.value, loss=self.loss.value)
        if self.default_weight < 0:
            raise ConfigurationError("a task weight cannot be negative",
                                     task=self.kind.value)

    @property
    def has_head(self) -> bool:
        return self.task_class is not TaskClass.DERIVED

    @property
    def trainable_here(self) -> bool:
        """Can this be trained from what Olympus can obtain in this
        environment?"""
        return self.task_class in (TaskClass.SUPERVISED,
                                   TaskClass.SELF_SUPERVISED)

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "task_class": self.task_class.value,
                "summary": self.summary, "loss": self.loss.value,
                "outputs_per_step": self.outputs_per_step,
                "window_level": self.window_level,
                "requires_channels": list(self.requires_channels),
                "default_weight": self.default_weight,
                "blocked_on": self.blocked_on,
                "derived_from": [k.value for k in self.derived_from],
                "has_head": self.has_head,
                "trainable_here": self.trainable_here, "notes": self.notes}


TASKS: Mapping[TaskKind, TaskSpec] = {
    spec.kind: spec for spec in (
        TaskSpec(
            kind=TaskKind.RETURN_DISTRIBUTION, task_class=TaskClass.SUPERVISED,
            summary="Quantiles of cumulative log return at each horizon step.",
            loss=LossKind.PINBALL, outputs_per_step=0,
            requires_channels=("close",), default_weight=1.0,
            notes="The primary head. Monotone by construction — the ladder is "
                  "a base plus softplus increments, so a crossed quantile is "
                  "arithmetically impossible rather than penalised."),

        TaskSpec(
            kind=TaskKind.DIRECTION, task_class=TaskClass.SUPERVISED,
            summary="Probability that the terminal return is positive.",
            loss=LossKind.BINARY_CROSS_ENTROPY, outputs_per_step=1,
            requires_channels=("close",), default_weight=0.5,
            notes="A separate head rather than a threshold on the median. The "
                  "median's sign and the probability of a positive move are "
                  "different quantities whenever the distribution is skewed, "
                  "and a strategy sizing on one while reading the other is a "
                  "real defect. `direction_consistency` in `evaluation.py` "
                  "measures how often they disagree."),

        TaskSpec(
            kind=TaskKind.PRICE_QUANTILES, task_class=TaskClass.DERIVED,
            summary="Return quantiles expressed in price, from the anchor close.",
            loss=LossKind.NONE, derived_from=(TaskKind.RETURN_DISTRIBUTION,),
            notes="Deliberately not a head. anchor·exp(q) is exact, and two "
                  "heads could disagree — a price band contradicting its own "
                  "return band is worse than one band."),

        TaskSpec(
            kind=TaskKind.VOLATILITY, task_class=TaskClass.SUPERVISED,
            summary="Realised volatility of log returns over the horizon.",
            loss=LossKind.MEAN_SQUARED_ERROR, outputs_per_step=1,
            window_level=True, requires_channels=("close",), default_weight=0.3,
            notes="Predicted in log space and exponentiated, so the head cannot "
                  "emit a negative volatility. Trained on the *realised* value "
                  "over the horizon, which is a noisy target — the evaluation "
                  "reports the error against a persistence-of-volatility "
                  "baseline for that reason."),

        TaskSpec(
            kind=TaskKind.REGIME, task_class=TaskClass.SUPERVISED,
            summary="Distribution over the six Olympus regime labels.",
            loss=LossKind.CROSS_ENTROPY, outputs_per_step=len(REGIME_CLASSES),
            window_level=True, requires_channels=("close", "high", "low"),
            default_weight=0.3,
            notes="Weak supervision from `regime.RegimeClassifier`, which "
                  "Olympus owns end to end — so there is no licence question "
                  "and no external label source. This head's logits are also "
                  "the mixture-of-experts router, which is why it is enabled "
                  "by default even when regime output is not wanted."),

        TaskSpec(
            kind=TaskKind.DRAWDOWN, task_class=TaskClass.SUPERVISED,
            summary="Probability that peak-to-trough drawdown within the "
                    "horizon exceeds a stated threshold.",
            loss=LossKind.BINARY_CROSS_ENTROPY, outputs_per_step=1,
            window_level=True, requires_channels=("close", "low"),
            default_weight=0.3,
            notes="A probability against a threshold rather than a regression "
                  "on the magnitude: the magnitude's distribution is heavily "
                  "skewed and an MSE fit on it collapses to the mean, which is "
                  "useless precisely in the tail the risk engine cares about."),

        TaskSpec(
            kind=TaskKind.LIQUIDITY, task_class=TaskClass.BLOCKED,
            summary="Forecast of tradable depth over the horizon.",
            loss=LossKind.MEAN_SQUARED_ERROR, outputs_per_step=1,
            window_level=True,
            requires_channels=("book_depth_bid", "book_depth_ask"),
            default_weight=0.2,
            blocked_on="order-book depth is NOT_INGESTED (blocker B1). Volume "
                       "is available and is a poor proxy — a thin book can "
                       "trade large volume at terrible prices, which is the "
                       "case the head exists to catch.",
            notes="The head is implemented and refuses to enable. Enabling it "
                  "on volume would produce a liquidity forecast that is really "
                  "a volume forecast wearing its name."),

        TaskSpec(
            kind=TaskKind.SPREAD, task_class=TaskClass.BLOCKED,
            summary="Forecast bid-ask spread in basis points.",
            loss=LossKind.MEAN_SQUARED_ERROR, outputs_per_step=1,
            window_level=True, requires_channels=("bid_ask_spread",),
            default_weight=0.2,
            blocked_on="bid and ask are NOT_INGESTED (blocker B1)."),

        TaskSpec(
            kind=TaskKind.EXECUTION_COST, task_class=TaskClass.BLOCKED,
            summary="Expected implementation shortfall for a stated size.",
            loss=LossKind.MEAN_ABSOLUTE_ERROR, outputs_per_step=1,
            window_level=True,
            requires_channels=("bid_ask_spread", "book_depth_bid"),
            default_weight=0.2,
            blocked_on="needs book state as an input. Its *targets* do exist — "
                       "`outcomes.py` records real fills, fees and slippage "
                       "from the paper broker — so this is the one blocked "
                       "task whose supervision is closer than its features.",
            notes="Training it from outcomes rather than from windows is a "
                  "different pipeline and is not attempted here."),

        TaskSpec(
            kind=TaskKind.ANOMALY, task_class=TaskClass.SELF_SUPERVISED,
            summary="Reconstruction error of the input window under the "
                    "representation layer.",
            loss=LossKind.MEAN_SQUARED_ERROR, outputs_per_step=0,
            window_level=True, requires_channels=("close",), default_weight=0.1,
            notes="Needs no label: the target is the input. Doubles as a "
                  "cheap density proxy — a window the representation cannot "
                  "reconstruct is a window unlike the training set, which is "
                  "why `ood` reads it."),

        TaskSpec(
            kind=TaskKind.OOD, task_class=TaskClass.DERIVED,
            summary="Distance from the training manifold, in [0, 1].",
            loss=LossKind.NONE,
            derived_from=(TaskKind.ANOMALY, TaskKind.RETURN_DISTRIBUTION),
            notes="Computed about the model, not by it: reconstruction error "
                  "and window volatility against ranges recorded at fit time. "
                  "A head predicting its own OOD-ness would be trained only on "
                  "in-distribution data, which is the one regime where the "
                  "question does not arise."),

        TaskSpec(
            kind=TaskKind.CALIBRATION, task_class=TaskClass.DERIVED,
            summary="Empirical coverage of the predicted intervals, and the "
                    "correction that would fix it.",
            loss=LossKind.NONE, derived_from=(TaskKind.RETURN_DISTRIBUTION,),
            notes="Fitted on a held-out split *after* training, never on the "
                  "training rows. A calibration fitted on the training set "
                  "reports the training set's coverage, which is always good."),

        TaskSpec(
            kind=TaskKind.ABSTENTION, task_class=TaskClass.SUPERVISED,
            summary="Probability that this forecast's terminal error will "
                    "exceed the model's own typical error.",
            loss=LossKind.BINARY_CROSS_ENTROPY, outputs_per_step=1,
            window_level=True, requires_channels=("close",), default_weight=0.2,
            notes="The borderline case: a real head, but its target depends on "
                  "the model's own predictions, so it is trained on a detached "
                  "signal computed from the current forward pass. It informs "
                  "the abstention policy; it does not override the nine "
                  "structural reasons in `abstain.py`, which are checks rather "
                  "than predictions."),

        TaskSpec(
            kind=TaskKind.CROSS_ASSET, task_class=TaskClass.BLOCKED,
            summary="Return forecast conditioned on reference instruments.",
            loss=LossKind.PINBALL, outputs_per_step=0,
            requires_channels=("cross_asset_return",),
            default_weight=0.5,
            blocked_on="needs a multi-instrument corpus. The causal alignment "
                       "exists (`dataset.align_cross_asset`) and there is one "
                       "instrument's worth of reachable data, which is "
                       "synthetic.",
            notes="The cross-asset attention block is implemented and is "
                  "exercised in tests with constructed reference latents; what "
                  "is missing is a corpus, not code."),

        TaskSpec(
            kind=TaskKind.MULTI_TIMEFRAME, task_class=TaskClass.BLOCKED,
            summary="Return forecast at a coarser timeframe than the base.",
            loss=LossKind.PINBALL, outputs_per_step=0,
            requires_channels=("close",), default_weight=0.5,
            blocked_on="needs a multi-scale corpus. `dataset.align_timeframes` "
                       "produces the pairing and nothing has been ingested to "
                       "pair.",
            notes="The fusion block implements `interfaces.TimeframeFusion` "
                  "and fixes its timeframe order at construction."),
    )
}

#: The tasks enabled unless a caller says otherwise. Regime is included even
#: though regime output may not be wanted, because its logits route the
#: mixture of experts — see `model.py`.
DEFAULT_TASKS: tuple[TaskKind, ...] = (
    TaskKind.RETURN_DISTRIBUTION, TaskKind.DIRECTION, TaskKind.VOLATILITY,
    TaskKind.REGIME, TaskKind.DRAWDOWN, TaskKind.ANOMALY, TaskKind.ABSTENTION)


@dataclass(frozen=True)
class TaskConfig:
    """Which heads a checkpoint carries, and how much each counts.

    Refuses to enable a blocked task. That refusal is the module's main job: a
    checkpoint advertising a spread head that was trained on nothing would pass
    every shape test and report a confident number for a quantity it has never
    seen.
    """

    enabled: tuple[TaskKind, ...] = DEFAULT_TASKS
    weights: Mapping[str, float] = field(default_factory=dict)
    #: Drawdown threshold, as a positive fraction. 0.02 = "will it fall 2%".
    drawdown_threshold: float = 0.02
    #: Multiplier on the model's median absolute error that defines a "bad"
    #: forecast for the abstention head's target.
    abstention_error_multiple: float = 2.0
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self):
        kinds = tuple(TaskKind(k) for k in self.enabled)
        if not kinds:
            raise ConfigurationError("a model must have at least one task")
        if len(set(kinds)) != len(kinds):
            raise ConfigurationError("a task is enabled twice",
                                     enabled=[k.value for k in kinds])
        for kind in kinds:
            spec = TASKS[kind]
            if spec.task_class is TaskClass.BLOCKED:
                raise ConfigurationError(
                    "this task cannot be enabled here",
                    task=kind.value, blocked_on=spec.blocked_on)
            if spec.task_class is TaskClass.DERIVED:
                raise ConfigurationError(
                    "a derived task has no head to enable; it is computed from "
                    "its parents whenever they are present",
                    task=kind.value,
                    derived_from=[k.value for k in spec.derived_from])
        if TaskKind.RETURN_DISTRIBUTION not in kinds:
            raise ConfigurationError(
                "the return-distribution head is required: every derived "
                "output and the whole forecast contract depend on it",
                enabled=[k.value for k in kinds])
        # Order by the registry rather than by the caller, so two configs
        # enabling the same set produce the same fingerprint and the same head
        # ordering inside the model.
        order = list(TASKS)
        object.__setattr__(self, "enabled",
                           tuple(sorted(kinds, key=order.index)))

        weights = {str(k): float(v) for k, v in self.weights.items()}
        unknown = [k for k in weights if k not in {t.value for t in TaskKind}]
        if unknown:
            raise ConfigurationError("weights name unknown tasks",
                                     unknown=sorted(unknown))
        if any(v < 0 for v in weights.values()):
            raise ConfigurationError("a task weight cannot be negative",
                                     weights=weights)
        object.__setattr__(self, "weights", weights)

        if not 0.0 < self.drawdown_threshold < 1.0:
            raise ConfigurationError("drawdown_threshold must be in (0, 1)",
                                     drawdown_threshold=self.drawdown_threshold)
        if self.abstention_error_multiple <= 0:
            raise ConfigurationError("abstention_error_multiple must be > 0")

    def weight(self, kind: TaskKind) -> float:
        return self.weights.get(TaskKind(kind).value,
                                TASKS[TaskKind(kind)].default_weight)

    def is_enabled(self, kind: TaskKind) -> bool:
        return TaskKind(kind) in self.enabled

    @property
    def heads(self) -> tuple[TaskKind, ...]:
        return tuple(k for k in self.enabled if TASKS[k].has_head)

    @property
    def available_derived(self) -> tuple[TaskKind, ...]:
        """Derived outputs whose parents are all enabled."""
        return tuple(k for k, spec in TASKS.items()
                     if spec.task_class is TaskClass.DERIVED
                     and all(p in self.enabled for p in spec.derived_from))

    def to_dict(self) -> dict:
        return {"enabled": [k.value for k in self.enabled],
                "weights": {k.value: self.weight(k) for k in self.enabled},
                "drawdown_threshold": self.drawdown_threshold,
                "abstention_error_multiple": self.abstention_error_multiple,
                "derived": [k.value for k in self.available_derived],
                "schema_version": self.schema_version}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskConfig":
        return cls(enabled=tuple(TaskKind(k) for k in data.get("enabled") or ()),
                   weights=dict(data.get("weights") or {}),
                   drawdown_threshold=float(data.get("drawdown_threshold", 0.02)),
                   abstention_error_multiple=float(
                       data.get("abstention_error_multiple", 2.0)),
                   schema_version=int(data.get("schema_version",
                                               TASK_SCHEMA_VERSION)))


# ===========================================================================
# targets
# ===========================================================================
#
# Every supervised head's target, computed from a `data.Window` and nothing
# else. Written as plain functions over floats so they can be read, checked by
# hand and tested without a tensor library — the target definition is the part
# of a multi-task model most worth being able to read.

def _window_log_returns(window: Any) -> list[float]:
    closes = [bar.close for bar in window.inputs]
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def target_return_quantiles(window: Any) -> tuple[float, ...]:
    """Cumulative log return at each horizon step. The primary target."""
    return tuple(window.target_log_returns())


def target_direction(window: Any) -> float:
    """1.0 if the terminal cumulative return is positive, else 0.0."""
    return 1.0 if window.target_log_returns()[-1] > 0 else 0.0


def target_volatility(window: Any) -> float:
    """Realised per-bar sigma of log returns *over the horizon*.

    Over the horizon, not over the input: a volatility head trained on the
    input window's volatility would be learning to copy a number it can already
    see, and would score beautifully while forecasting nothing.
    """
    anchor = window.inputs[-1].close
    closes = [anchor] + [bar.close for bar in window.targets]
    steps = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(steps) < 2:
        # One step gives no dispersion. |r| is the honest single-sample
        # estimate of scale and is what the head is asked to predict.
        return abs(steps[0]) if steps else 0.0
    return statistics.pstdev(steps)


def target_regime(window: Any, classifier: Any = None) -> int:
    """Index into `REGIME_CLASSES` of the regime at the window's decision point.

    Classified on the *inputs*, so the label is knowable at `as_of`. Labelling
    on the targets would make the head a look-ahead detector.
    """
    if classifier is None:
        from ..regime import RegimeClassifier
        classifier = RegimeClassifier()
    try:
        state = classifier.classify(list(window.inputs))
        value = getattr(state, "regime", Regime.UNKNOWN)
        label = value.value if isinstance(value, Regime) else str(value)
    except Exception:                                    # noqa: BLE001
        label = Regime.UNKNOWN.value
    return REGIME_CLASSES.index(label) if label in REGIME_CLASSES else 0


def target_drawdown(window: Any, threshold: float) -> float:
    """1.0 if peak-to-trough drawdown within the horizon exceeds `threshold`.

    Measured on the target bars' lows against the running peak of their highs,
    starting from the anchor close — which is the worst case an open position
    would actually have experienced, not the close-to-close approximation.
    """
    peak = window.inputs[-1].close
    worst = 0.0
    for bar in window.targets:
        peak = max(peak, bar.high)
        if peak > 0:
            worst = max(worst, (peak - bar.low) / peak)
    return 1.0 if worst > threshold else 0.0


def target_drawdown_magnitude(window: Any) -> float:
    """The drawdown itself, for evaluation. Not a training target — see the
    `drawdown` task's note on why the head predicts a probability."""
    peak = window.inputs[-1].close
    worst = 0.0
    for bar in window.targets:
        peak = max(peak, bar.high)
        if peak > 0:
            worst = max(worst, (peak - bar.low) / peak)
    return worst


def targets_for(window: Any, config: TaskConfig, *, classifier: Any = None
                ) -> dict[str, Any]:
    """Every enabled supervised target for one window, keyed by task value.

    `anomaly` is absent: its target is the input, which the model already has,
    and materialising a copy here would double the memory for no information.
    """
    out: dict[str, Any] = {}
    for kind in config.enabled:
        if kind is TaskKind.RETURN_DISTRIBUTION:
            out[kind.value] = target_return_quantiles(window)
        elif kind is TaskKind.DIRECTION:
            out[kind.value] = target_direction(window)
        elif kind is TaskKind.VOLATILITY:
            out[kind.value] = target_volatility(window)
        elif kind is TaskKind.REGIME:
            out[kind.value] = target_regime(window, classifier)
        elif kind is TaskKind.DRAWDOWN:
            out[kind.value] = target_drawdown(window, config.drawdown_threshold)
    return out


def task_report(config: TaskConfig | None = None) -> dict:
    """The registry as data: what exists, what is enabled, what is blocked."""
    active = config or TaskConfig()
    return {
        "schema_version": TASK_SCHEMA_VERSION,
        "tasks": {k.value: spec.to_dict() for k, spec in TASKS.items()},
        "enabled": [k.value for k in active.enabled],
        "derived_available": [k.value for k in active.available_derived],
        "blocked": {k.value: spec.blocked_on for k, spec in TASKS.items()
                    if spec.task_class is TaskClass.BLOCKED},
        "trainable_here": sorted(k.value for k, spec in TASKS.items()
                                 if spec.trainable_here),
    }


__all__ = ["TASK_SCHEMA_VERSION", "REGIME_CLASSES", "TaskKind", "TaskClass",
           "LossKind", "TaskSpec", "TASKS", "DEFAULT_TASKS", "TaskConfig",
           "target_return_quantiles", "target_direction", "target_volatility",
           "target_regime", "target_drawdown", "target_drawdown_magnitude",
           "targets_for", "task_report"]
