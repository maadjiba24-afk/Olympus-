"""Model cards: what a checkpoint is, what it is not, and who may say so.

A model card here is generated from the artefacts rather than written beside
them. `build_card` reads the model config, the run record, the checkpoint and
the evaluation report, and assembles the card from what those objects actually
say. Then it runs `originality.audit` on the result and **refuses to emit a
card that would state something false** — so the disclosure and the check
cannot drift apart, which is the usual failure mode of a hand-written card.

Three sections are not optional
-------------------------------
**Limitations.** A card with an empty limitations list is refused. "None known"
is a statement and must be written as one, and writing it is enough friction
that somebody thinks about it first.

**External methods.** Every published technique the architecture uses, named.
`originality.REQUIRED_METHOD_DISCLOSURES` is the list the audit checks against,
so a card that omits "mixture of experts" for a model that has one does not
render.

**Evidence class.** Every performance claim carries the class of evidence
behind it — synthetic, real, or none. `EvidenceClass.REAL_DATA` is the one no
card in this repository may currently use, because blocker B1 means no such
evidence exists; `assert_no_real_data_claim` enforces it rather than trusting
whoever fills the template.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from ..contracts import jsonable
from ..errors import ConfigurationError
from . import originality
from .model import ModelConfig, architecture_report
from .tasks import TASKS, TaskClass

#: Bumped when the card's *sections* change, so an older card is detectably
#: older rather than partially parsed.
CARD_VERSION = 1


class EvidenceClass(str, Enum):
    """What a performance claim rests on. Ordered weakest to strongest."""

    NONE = "none"
    DESIGNED_ONLY = "designed_only"
    UNIT_TESTED = "unit_tested"
    SYNTHETIC_DATA = "synthetic_data"
    REAL_DATA = "real_data"


@dataclass(frozen=True)
class PerformanceClaim:
    """One number, and the class of evidence behind it. Never one without the
    other."""

    metric: str
    value: Any
    evidence: EvidenceClass
    dataset: str = ""
    n: int = 0
    note: str = ""

    def __post_init__(self):
        object.__setattr__(self, "evidence", EvidenceClass(self.evidence))
        if self.evidence in (EvidenceClass.SYNTHETIC_DATA,
                             EvidenceClass.REAL_DATA) and not self.dataset:
            raise ConfigurationError(
                "a data-backed claim must name its dataset",
                metric=self.metric, evidence=self.evidence.value)

    def to_dict(self) -> dict:
        return {"metric": self.metric, "value": jsonable(self.value),
                "evidence": self.evidence.value, "dataset": self.dataset,
                "n": self.n, "note": self.note}


@dataclass(frozen=True)
class ModelCard:
    """The card. Rendered to markdown, audited before it renders."""

    name: str
    version: str
    created_at: datetime
    summary: str
    intended_use: str
    out_of_scope: tuple[str, ...]
    architecture: Mapping[str, Any]
    tasks: Mapping[str, Any]
    training_data: Mapping[str, Any]
    training_procedure: Mapping[str, Any]
    evaluation: Mapping[str, Any]
    performance: tuple[PerformanceClaim, ...]
    limitations: tuple[str, ...]
    external_methods: tuple[str, ...]
    dependencies: Mapping[str, str]
    ownership: Mapping[str, Any]
    abstention: Mapping[str, Any]
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    reproducibility: Mapping[str, Any] = field(default_factory=dict)
    card_version: int = CARD_VERSION

    def __post_init__(self):
        for name in ("out_of_scope", "limitations", "external_methods",
                     "performance"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for name in ("architecture", "tasks", "training_data",
                     "training_procedure", "evaluation", "dependencies",
                     "ownership", "abstention", "checkpoint",
                     "reproducibility"):
            object.__setattr__(self, name, dict(getattr(self, name)))
        if not self.limitations:
            raise ConfigurationError(
                "a model card must state at least one limitation; 'none known' "
                "is a statement and should be written as one", card=self.name)
        if not self.external_methods:
            raise ConfigurationError(
                "a model card must name the external methods it uses",
                card=self.name)
        if not self.out_of_scope:
            raise ConfigurationError(
                "a model card must say what the model is not for", card=self.name)

    @property
    def strongest_evidence(self) -> EvidenceClass:
        order = list(EvidenceClass)
        if not self.performance:
            return EvidenceClass.NONE
        return max((c.evidence for c in self.performance), key=order.index)

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "created_at": jsonable(self.created_at),
                "summary": self.summary, "intended_use": self.intended_use,
                "out_of_scope": list(self.out_of_scope),
                "architecture": jsonable(dict(self.architecture)),
                "tasks": jsonable(dict(self.tasks)),
                "training_data": jsonable(dict(self.training_data)),
                "training_procedure": jsonable(dict(self.training_procedure)),
                "evaluation": jsonable(dict(self.evaluation)),
                "performance": [c.to_dict() for c in self.performance],
                "strongest_evidence": self.strongest_evidence.value,
                "limitations": list(self.limitations),
                "external_methods": list(self.external_methods),
                "dependencies": dict(self.dependencies),
                "ownership": jsonable(dict(self.ownership)),
                "abstention": jsonable(dict(self.abstention)),
                "checkpoint": jsonable(dict(self.checkpoint)),
                "reproducibility": jsonable(dict(self.reproducibility)),
                "card_version": self.card_version}

    def to_markdown(self) -> str:
        lines = [
            f"# Model card — {self.name} `{self.version}`", "",
            f"- **Created:** {self.created_at.isoformat()}",
            f"- **Strongest evidence:** `{self.strongest_evidence.value}`",
            f"- **Checkpoint:** `{self.checkpoint.get('content_hash', '(none)')[:16]}`",
            f"- **Reproducible:** {self.reproducibility.get('reproducible', False)}",
            "", "## Summary", "", self.summary, "",
            "## Intended use", "", self.intended_use, "",
            "## Out of scope", "",
        ]
        lines += [f"- {item}" for item in self.out_of_scope]

        lines += ["", "## Architecture", ""]
        blocks = self.architecture.get("blocks") or {}
        for name, block in blocks.items():
            enabled = block.get("enabled")
            state = "" if enabled is None else (" — enabled" if enabled
                                                else " — not enabled")
            lines.append(f"- **{name}**{state}: {block.get('why', '')}")

        lines += ["", "## Tasks", "",
                  "| task | class | enabled | note |", "|---|---|---|---|"]
        enabled = set(self.tasks.get("enabled") or ())
        for key, spec in (self.tasks.get("tasks") or {}).items():
            note = spec.get("blocked_on") or spec.get("summary", "")
            lines.append(f"| `{key}` | {spec.get('task_class')} | "
                         f"{'yes' if key in enabled else 'no'} | {note} |")

        lines += ["", "## Training data", ""]
        for key, value in sorted(self.training_data.items()):
            lines.append(f"- **{key}**: {value}")

        lines += ["", "## Training procedure", ""]
        for key, value in sorted(self.training_procedure.items()):
            lines.append(f"- **{key}**: {value}")

        lines += ["", "## Evaluation", ""]
        for key, value in sorted(self.evaluation.items()):
            lines.append(f"- **{key}**: {value}")

        lines += ["", "## Performance", "",
                  "| metric | value | evidence | dataset | n |",
                  "|---|---|---|---|---|"]
        for claim in self.performance:
            lines.append(f"| {claim.metric} | {claim.value} | "
                         f"`{claim.evidence.value}` | {claim.dataset} | "
                         f"{claim.n} |")
        lines += ["",
                  "> No claim above rests on real market data. See the "
                  "limitations."]

        lines += ["", "## Limitations", ""]
        lines += [f"- {item}" for item in self.limitations]

        lines += ["", "## External methods and inspirations", "",
                  "Published, general techniques. **No novelty is claimed in "
                  "any of them.**", ""]
        lines += [f"- {item}" for item in self.external_methods]

        lines += ["", "## Dependencies disclosed", ""]
        for name, why in sorted(self.dependencies.items()):
            lines.append(f"- **{name}**: {why}")

        lines += ["", "## Ownership", ""]
        for key, value in sorted(self.ownership.items()):
            lines.append(f"- **{key}**: {value}")

        lines += ["", "## Abstention", "",
                  "The model may decline. It does so for these reasons, each a "
                  "measured check:", ""]
        for reason, meaning in sorted(
                (self.abstention.get("reasons") or {}).items()):
            lines.append(f"- `{reason}` — {meaning}")

        lines += ["", "## Reproducibility", ""]
        for key, value in sorted(self.reproducibility.items()):
            lines.append(f"- **{key}**: {value}")
        return "\n".join(lines)


def build_card(*, name: str, version: str, created_at: datetime,
               model_config: ModelConfig,
               run_record: Any = None,
               checkpoint: Any = None,
               evaluation_report: Any = None,
               dataset_manifest: Any = None,
               abstention_policy: Any = None,
               performance: Sequence[PerformanceClaim] = (),
               extra_limitations: Sequence[str] = (),
               summary: str = "", intended_use: str = "") -> ModelCard:
    """Assemble a card from the artefacts, then audit it.

    Raises if the audit finds anything. A card that cannot pass its own
    originality checks is a card that would state something false, and emitting
    it with a warning would mean the warning is what gets ignored.
    """
    architecture = architecture_report(model_config)
    from .tasks import task_report
    tasks = task_report(model_config.tasks)

    blocked = {k: spec.blocked_on for k, spec in TASKS.items()
               if spec.task_class is TaskClass.BLOCKED}
    limitations = [
        "**No weights in this repository have been trained on real market "
        "data.** Every number on this card comes from series generated by the "
        "repository itself (blocker B1: no market-data provider is reachable).",
        f"{len(blocked)} of the fifteen tasks cannot be enabled here: "
        + "; ".join(f"`{k.value}` ({why.split('.')[0]})"
                    for k, why in blocked.items()),
        "The mixture of experts routes on the regime head's logits, so experts "
        "can only specialise along an axis the regime classifier already sees. "
        "A free router might discover a better partition; "
        "`ModelConfig.regime_routing=False` exists so that comparison can be "
        "run, and it has not been.",
        "Mixed precision and distributed training are implemented behind "
        "capability detection and have never executed here — there is no CUDA "
        "and one process (blocker B5).",
        "No matched evaluation against any third-party model has been run, and "
        "none can be: no official external checkpoint is reachable (blocker "
        "B2) and its weight licence is unverified (blocker B4).",
    ]
    limitations.extend(extra_limitations)

    ownership = {
        "code": "independently produced in this repository, written against "
                "Olympus's own contracts and channel schema",
        "weights": "trained from an Olympus dataset by an Olympus pipeline; "
                   "no foreign weights, and `checkpoint.assert_olympus_origin` "
                   "refuses a foreign origin at construction",
        "architecture": "an independently produced *combination*; the "
                        "individual techniques are published and are listed "
                        "under external methods, where no novelty is claimed",
        "scope_of_claim": "Olympus ownership applies to independently produced "
                          "code and weights only. It does not extend to the "
                          "published methods the architecture uses.",
        "weights_trained_on_real_data": False,
    }

    training_data: dict[str, Any] = {"source": "synthetic (see limitations)"}
    if dataset_manifest is not None:
        training_data.update({
            "dataset_id": getattr(dataset_manifest, "dataset_id", ""),
            "content_hash": getattr(dataset_manifest, "content_hash", "")[:16],
            "schema": getattr(dataset_manifest, "schema_name", ""),
            "manifest_gaps": list(getattr(dataset_manifest, "gaps", ())) or "none",
        })

    procedure: dict[str, Any] = {}
    reproducibility: dict[str, Any] = {"reproducible": False,
                                       "gaps": ["no run record supplied"]}
    if run_record is not None:
        procedure = dict(run_record.training_config)
        procedure["parameters"] = run_record.parameters
        procedure["selection_split"] = run_record.selection_split
        procedure["epochs_run"] = len(run_record.epochs)
        procedure["untested_paths"] = list(run_record.untested_paths)
        reproducibility = {
            "reproducible": run_record.reproducible,
            "gaps": list(run_record.gaps) or "none",
            "dataset_hash": run_record.dataset_hash[:16],
            "code_version": run_record.code_version,
            "config_fingerprint": run_record.config_fingerprint,
            "seed": run_record.seed,
            "environment": dict(run_record.environment),
            "signed": run_record.signed,
        }

    checkpoint_info: dict[str, Any] = {}
    if checkpoint is not None:
        checkpoint_info = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "model_kind": checkpoint.model_kind,
            "content_hash": checkpoint.content_hash,
            "model_version": checkpoint.model_version,
            "initialised_from": checkpoint.initialised_from or "(fresh)",
            "manifest_gaps": list(checkpoint.manifest.gaps) or "none",
        }

    evaluation: dict[str, Any] = {"status": "not evaluated"}
    if evaluation_report is not None:
        evaluation = {
            "verdict": evaluation_report.verdict,
            "n": evaluation_report.overall.get("n"),
            "strata": len(evaluation_report.strata),
            "comparisons": [c.opponent for c in evaluation_report.comparisons],
            "missing_arms": [a.get("name") for a in evaluation_report.missing_arms],
            "weak_strata": [f"{s.dimension}={s.value}"
                            for s in evaluation_report.weak_strata],
        }

    abstention = (abstention_policy.describe() if abstention_policy is not None
                  else {"reasons": {}})

    card = ModelCard(
        name=name, version=version, created_at=created_at,
        summary=summary or _default_summary(model_config),
        intended_use=intended_use or _default_intended_use(),
        out_of_scope=(
            "submitting orders, changing risk limits, accessing broker "
            "credentials, enabling live trading, or promoting itself — it "
            "produces forecasts and evidence and nothing else",
            "any use on real market data: nothing here has been validated "
            "against any",
            "any claim of superiority to a third-party model",
        ),
        architecture=architecture, tasks=tasks, training_data=training_data,
        training_procedure=procedure, evaluation=evaluation,
        performance=tuple(performance), limitations=tuple(limitations),
        external_methods=tuple(architecture["external_methods"]),
        dependencies=dict(originality.DISCLOSED_DEPENDENCIES),
        ownership=ownership, abstention=abstention,
        checkpoint=checkpoint_info, reproducibility=reproducibility)

    findings = originality.audit(
        card.to_dict(),
        checkpoints=[checkpoint] if checkpoint is not None else (),
        configurations=[model_config.to_dict()])
    if findings:
        raise ConfigurationError(
            "the model card failed its own originality audit and was not "
            "emitted",
            findings=[f.to_dict() for f in findings])
    assert_no_real_data_claim(card)
    return card


def assert_no_real_data_claim(card: ModelCard) -> None:
    """No card in this repository may claim real-data evidence.

    Enforced rather than trusted, because a template's strongest safeguard is
    the one that does not depend on whoever fills it in. When B1 lifts, this
    function is what has to be deliberately changed — which is the right amount
    of friction for a claim that big.
    """
    offenders = [c.metric for c in card.performance
                 if c.evidence is EvidenceClass.REAL_DATA]
    if offenders:
        raise ConfigurationError(
            "a performance claim asserts real-data evidence, and no such "
            "evidence exists in this repository (blocker B1)",
            metrics=offenders)
    if card.ownership.get("weights_trained_on_real_data"):
        raise ConfigurationError(
            "the card claims weights trained on real data (blocker B1)")


def _default_summary(config: ModelConfig) -> str:
    return (
        f"An Olympus-native multi-task market model: a replaceable "
        f"representation layer feeding a dilated causal-convolution core, a "
        f"mixture of {config.n_experts} experts routed by a separately "
        f"supervised regime head, and one modular head per enabled task "
        f"({len(config.tasks.heads)} of fifteen). Emits quantiles of future "
        f"log return, a direction probability, a volatility forecast, a "
        f"regime distribution, a drawdown probability and an abstention "
        f"signal, over a horizon of {config.horizon} bars from a "
        f"{config.lookback}-bar window.")


def _default_intended_use() -> str:
    return (
        "Producing forecasts, evidence and structured trade intents inside "
        "Olympus, as one interchangeable implementation of "
        "`forecast.Forecaster`. It is a peer of the statistical baselines, "
        "judged by the same evaluator and gated by the same capability ladder. "
        "It may not submit orders, change risk limits, reach broker "
        "credentials, enable live trading or promote itself into production.")


def card_template() -> dict:
    """The empty template, for a card written by hand rather than generated.

    Every key `build_card` fills, with a note on what belongs there. A hand-
    written card must still pass `originality.audit`, so this is a starting
    point rather than an escape hatch.
    """
    return {
        "name": "<model name>",
        "version": "<version>",
        "created_at": "<ISO-8601 UTC>",
        "summary": "<what it is, in three sentences>",
        "intended_use": "<what it is for, and inside what boundary>",
        "out_of_scope": ["<what it must not be used for>"],
        "architecture": {"blocks": {}, "external_methods": [],
                         "claimed_original": [], "not_claimed": []},
        "tasks": {"enabled": [], "blocked": {}},
        "training_data": {"source": "", "content_hash": "", "manifest_gaps": []},
        "training_procedure": {"seed": 0, "epochs": 0, "selection_split": ""},
        "evaluation": {"verdict": "", "strata": 0, "missing_arms": []},
        "performance": [{"metric": "", "value": None,
                         "evidence": "synthetic_data|unit_tested|none",
                         "dataset": "", "n": 0}],
        "limitations": ["<at least one; 'none known' must be written as one>"],
        "external_methods": ["<every published technique used, by name>"],
        "dependencies": dict(originality.DISCLOSED_DEPENDENCIES),
        "ownership": {"code": "", "weights": "", "architecture": "",
                      "scope_of_claim": "",
                      "weights_trained_on_real_data": False},
        "abstention": {"reasons": {}},
        "checkpoint": {"content_hash": "", "initialised_from": ""},
        "reproducibility": {"reproducible": False, "gaps": []},
        "card_version": CARD_VERSION,
    }


def write_card(card: ModelCard, path: Any) -> None:                # pragma: no cover
    """Write the markdown card and its JSON twin beside each other."""
    from pathlib import Path
    target = Path(path)
    target.write_text(card.to_markdown(), encoding="utf-8")
    target.with_suffix(".json").write_text(
        json.dumps(card.to_dict(), indent=2, default=str), encoding="utf-8")


__all__ = ["CARD_VERSION", "EvidenceClass", "PerformanceClaim", "ModelCard",
           "build_card", "assert_no_real_data_claim", "card_template",
           "write_card"]
