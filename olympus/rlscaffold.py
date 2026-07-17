"""Offline RL preference-data + reward-model SCAFFOLD — gated, default-off,
human-in-the-loop, and emphatically **NOT a live training loop**.

"RL fine-tuning" done the Olympus way: not a self-modifying training loop wired
into a language model, but an *offline, transparent, advisory* substrate that
turns the outcomes Olympus already logs into a reward signal a human can inspect.
Every dangerous property of RLHF is designed OUT:

  * **No live training.** Nothing here updates a language model's weights, calls
    a model or a training API, or runs continuously. `fit()` is deterministic
    plain-Python gradient descent over a small *linear* reward model — no ML
    dependency (no torch / numpy), no network, no randomness.
  * **No autonomous behavior change.** This module has NO path that writes to
    routing, config, prompts, or any decision. Its scores are EVIDENCE a human
    reads; a `test` pins that no decision module imports it. Improvement is
    surfaced, never imposed — the same contract as `outcomes.py`.
  * **Real data only, gated.** Preference pairs are built from *human-labeled*
    outcomes already on disk (`routing_outcomes`, `outcomes`) — synthetic / test
    rows are excluded, exactly as the learned-router gate does, so replays and
    test traffic can never manufacture a training signal. Below the data gate
    the model stays an inert scaffold.
  * **Off by default.** Fitting and export require `OLYMPUS_RL_SCAFFOLD` — an
    operator's deliberate opt-in, never the agent's. Read-only inspection of the
    dataset is always allowed so an operator can see what exists before opting in.
  * **Signed, content-safe audit.** Every export is a witness-signed `deltas`
    record (externally anchored for free) carrying only categorical tags, counts
    and learned weights — never any raw user text.

Preference sources (each yields genuine, feature-differentiated pairs):

  * **routing** — for a matched context (task-type + input-length bucket), a
    decision whose run got a POSITIVE outcome is preferred over one that got a
    NEGATIVE outcome. The decision *features* (specialist / model / role) differ,
    so the pair carries real gradient. This is precisely the signal the
    learned-router SPEC-04 gate describes.
  * **actions** — a prepared action the user APPROVED is preferred over the
    neutral baseline; one they REJECTED / UNDID loses to it (pointwise → paired
    against a zero-feature reference). An APPROVED-after-edit is a half-win.

The supervision scoreboard (CLEAN/DIRTY graded cycles) is reported alongside as
signed *dataset health* provenance — a global cycle signal, not per-decision
features, so it contextualizes the dataset rather than being shoehorned into it.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from . import deltas, outcomes, routing_outcomes

# Signed audit target for dataset/model exports.
RL_DATASET = "rl:dataset"

# --- data gate (mirrors routing_outcomes' real-adoption gate) ----------------
# Enough genuine pairs, spread across enough distinct contexts, before the fitted
# reward model is considered meaningful rather than a scaffold fit to noise.
GATE_MIN_PAIRS = 50
GATE_MIN_CONTEXTS = 3

# A weight for an edit-then-approve half-win (matches learned_routing's 0.5).
_EDIT_WEIGHT = 0.5

# Fit hyperparameters — fixed, deterministic, documented (no tuning loop).
_EPOCHS = 200
_LR = 0.2
_L2 = 1e-3


class RLScaffoldError(RuntimeError):
    """A gated RL-scaffold operation was refused (disabled or below the gate)."""


def enabled() -> bool:
    """Operator-only opt-in for FIT/EXPORT (default OFF). The agent never sets
    it. Read-only dataset inspection does not require it."""
    return os.environ.get("OLYMPUS_RL_SCAFFOLD", "").strip().lower() in (
        "1", "on", "true", "yes")


# --- features ----------------------------------------------------------------

def _route_features(row: dict) -> dict:
    """The learnable DECISION features of a routing row (specialist / model /
    role). Context (task-type, length) is held constant within a pair, so it is
    the pairing key, not a feature."""
    feats = {}
    for attr in ("specialist", "model", "role"):
        val = str(row.get(attr, "")).strip()
        if val:
            feats[f"{attr}={val}"] = 1.0
    return feats


def _route_context(row: dict) -> tuple:
    return (str(row.get("task_type", "?")), str(row.get("length_bucket", "?")))


def _action_features(ref: str, kind: str) -> dict:
    return {f"action={kind}:{ref}": 1.0}


# --- preference pairs --------------------------------------------------------

@dataclass(frozen=True)
class PreferencePair:
    """A single learned comparison: `chosen` should score above `rejected`.

    Both sides are sparse categorical feature maps; an empty map is the neutral
    BASELINE reference (score 0) used to turn a pointwise good/bad label into a
    pair. `context` records the matched setting the comparison was drawn from."""
    chosen: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    source: str = "unknown"
    context: str = ""
    weight: float = 1.0

    def to_dict(self) -> dict:
        return {"chosen": self.chosen, "rejected": self.rejected,
                "source": self.source, "context": self.context,
                "weight": self.weight}


def collect_routing_pairs(rows: list | None = None, *,
                          max_per_context: int = 200) -> list[PreferencePair]:
    """Matched-context (positive ≻ negative) pairs from routing telemetry. Only
    REAL (non-synthetic) rows with a decided signal count — synthetic/test rows
    are excluded, so replays never manufacture a signal. Crossing is capped per
    context to bound the pair count."""
    rows = routing_outcomes._all_rows() if rows is None else rows
    by_ctx: dict[tuple, dict] = {}
    for r in rows:
        if r.get("synthetic"):
            continue
        sig = r.get("outcome_signal")
        if sig == routing_outcomes.POSITIVE:
            bucket = "pos"
        elif sig == routing_outcomes.NEGATIVE:
            bucket = "neg"
        elif sig == routing_outcomes.APPROVED_AFTER_EDIT:
            bucket = "half"
        else:                                   # pending / unknown → not a label
            continue
        feats = _route_features(r)
        if not feats:
            continue
        by_ctx.setdefault(_route_context(r), {"pos": [], "neg": [], "half": []})
        by_ctx[_route_context(r)][bucket].append(feats)

    pairs: list[PreferencePair] = []
    for ctx, arms in sorted(by_ctx.items()):
        winners = arms["pos"] + arms["half"]      # half-wins still beat a loss
        losers = arms["neg"]
        if not winners or not losers:
            continue
        n = 0
        for wi, w in enumerate(winners):
            for li, l in enumerate(losers):
                if n >= max_per_context:
                    break
                # A half-win contributes at half weight.
                weight = _EDIT_WEIGHT if wi >= len(arms["pos"]) else 1.0
                pairs.append(PreferencePair(
                    chosen=dict(w), rejected=dict(l), source="routing",
                    context=f"{ctx[0]}/{ctx[1]}", weight=weight))
                n += 1
            if n >= max_per_context:
                break
    return pairs


def collect_action_pairs(users: list | None = None) -> list[PreferencePair]:
    """Pointwise action outcomes as pairs-vs-baseline: APPROVED ≻ baseline,
    baseline ≻ REJECTED/UNDONE, APPROVED_AFTER_EDIT a half-win."""
    users = _known_users() if users is None else users
    pairs: list[PreferencePair] = []
    for user in sorted(set(users)):
        for e in outcomes.events(user):
            ref, kind = str(e.get("ref", "")), str(e.get("kind", "action"))
            if not ref:
                continue
            feats = _action_features(ref, kind)
            outcome = e.get("outcome")
            if outcome == outcomes.APPROVED:
                pairs.append(PreferencePair(chosen=feats, rejected={},
                                            source="action", context=kind))
            elif outcome == outcomes.APPROVED_AFTER_EDIT:
                pairs.append(PreferencePair(chosen=feats, rejected={},
                                            source="action", context=kind,
                                            weight=_EDIT_WEIGHT))
            elif outcome in (outcomes.REJECTED, outcomes.UNDONE):
                pairs.append(PreferencePair(chosen={}, rejected=feats,
                                            source="action", context=kind))
    return pairs


def _known_users() -> list:
    """Users that have an action-outcome log (best-effort)."""
    try:
        from . import store
        return list(store.backend().keys(outcomes._NS))
    except Exception:
        return []


def collect_pairs(*, users: list | None = None) -> list[PreferencePair]:
    """Every offline preference pair from the real, human-labeled logs."""
    return collect_routing_pairs() + collect_action_pairs(users)


# --- the reward model: a transparent linear scorer, fit OFFLINE ---------------

class RewardModel:
    """A small, interpretable linear reward model: `score(x) = Σ w[f]·x[f]`.

    Fit OFFLINE by deterministic gradient descent on the Bradley–Terry pairwise
    logistic objective — maximize `σ(score(chosen) − score(rejected))`. There is
    no randomness, no ML dependency, and no model/API call; the whole thing is a
    dict of feature weights a human can read (`top_features`)."""

    def __init__(self, weights: dict | None = None):
        self.weights: dict = dict(weights or {})

    def score(self, features: dict) -> float:
        return sum(self.weights.get(f, 0.0) * v for f, v in features.items())

    def fit(self, pairs: list, *, epochs: int = _EPOCHS, lr: float = _LR,
            l2: float = _L2) -> dict:
        """Learn weights from preference pairs. Deterministic: zero init, sorted
        feature order, no shuffling. Returns a fit summary (loss/accuracy trace
        endpoints, feature count) — NOT applied anywhere."""
        # Seed every seen feature at 0 so iteration order is stable & complete.
        for p in pairs:
            for f in p.chosen:
                self.weights.setdefault(f, 0.0)
            for f in p.rejected:
                self.weights.setdefault(f, 0.0)
        if not pairs:
            return {"pairs": 0, "features": 0, "epochs": 0,
                    "final_loss": 0.0, "accuracy": 0.0, "fitted": False}

        for _ in range(epochs):
            grad: dict = {}
            for p in pairs:
                margin = self.score(p.chosen) - self.score(p.rejected)
                # dL/dmargin for BT logistic loss = -(1 - σ(margin)) · weight
                coeff = (1.0 - _sigmoid(margin)) * p.weight
                for f, v in p.chosen.items():
                    grad[f] = grad.get(f, 0.0) + coeff * v
                for f, v in p.rejected.items():
                    grad[f] = grad.get(f, 0.0) - coeff * v
            # Gradient ASCENT on the objective, with L2 shrinkage.
            for f in sorted(self.weights):
                g = grad.get(f, 0.0) - l2 * self.weights[f]
                self.weights[f] += lr * g

        return {"pairs": len(pairs), "features": len(self.weights),
                "epochs": epochs, "final_loss": self._loss(pairs),
                "accuracy": self._accuracy(pairs), "fitted": True}

    def _loss(self, pairs: list) -> float:
        if not pairs:
            return 0.0
        tot = wsum = 0.0
        for p in pairs:
            margin = self.score(p.chosen) - self.score(p.rejected)
            tot += p.weight * -math.log(max(_sigmoid(margin), 1e-12))
            wsum += p.weight
        return round(tot / (wsum or 1.0), 6)

    def _accuracy(self, pairs: list) -> float:
        """Share of pairs the model ranks correctly (chosen strictly above)."""
        if not pairs:
            return 0.0
        right = sum(1 for p in pairs
                    if self.score(p.chosen) > self.score(p.rejected))
        return round(right / len(pairs), 4)

    def top_features(self, k: int = 10) -> dict:
        """The most reward-positive and most reward-negative learned features —
        the model's interpretable summary."""
        ordered = sorted(self.weights.items(), key=lambda kv: kv[1])
        return {"most_positive": [(f, round(w, 4)) for f, w in ordered[::-1][:k]],
                "most_negative": [(f, round(w, 4)) for f, w in ordered[:k]]}

    def to_dict(self) -> dict:
        return {"weights": {f: round(w, 6) for f, w in self.weights.items()}}

    @classmethod
    def from_dict(cls, d) -> "RewardModel":
        w = d.get("weights", {}) if isinstance(d, dict) else {}
        return cls({str(f): float(v) for f, v in w.items()})


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


# --- the data gate -----------------------------------------------------------

def gate_status(pairs: list | None = None) -> dict:
    """Whether enough REAL preference data exists for a fitted model to mean
    anything — else it stays an inert scaffold. Synthetic rows never reach here
    (they are dropped at collection)."""
    pairs = collect_pairs() if pairs is None else pairs
    contexts = {p.context for p in pairs}
    reasons = []
    if len(pairs) < GATE_MIN_PAIRS:
        reasons.append(f"need ≥{GATE_MIN_PAIRS} real preference pairs "
                       f"(have {len(pairs)})")
    if len(contexts) < GATE_MIN_CONTEXTS:
        reasons.append(f"need ≥{GATE_MIN_CONTEXTS} distinct contexts "
                       f"(have {len(contexts)})")
    return {"met": not reasons, "reasons": reasons, "pairs": len(pairs),
            "contexts": len(contexts),
            "thresholds": {"pairs": GATE_MIN_PAIRS,
                           "contexts": GATE_MIN_CONTEXTS}}


# --- dataset summary + signed export -----------------------------------------

def _by_source(pairs: list) -> dict:
    out: dict = {}
    for p in pairs:
        out[p.source] = out.get(p.source, 0) + 1
    return out


def _scoreboard_health() -> dict:
    """CLEAN/DIRTY counts from the signed supervision scoreboard — dataset
    provenance, reported alongside (not fed into the model)."""
    try:
        from . import supervise
        clean = dirty = 0
        for s in supervise.scoreboard():
            g = (s.get("state") or {}).get("grade")
            if g == "CLEAN":
                clean += 1
            elif g == "DIRTY":
                dirty += 1
        return {"clean_cycles": clean, "dirty_cycles": dirty}
    except Exception:
        return {"clean_cycles": 0, "dirty_cycles": 0}


def collect_report(*, fit: bool = False) -> dict:
    """The read-only dataset report: pair counts, sources, the gate, scoreboard
    health, and — if `fit` and the gate is met — an offline model fit summary
    with its top features. Never mutates anything but its own (optional) audit."""
    pairs = collect_pairs()
    gate = gate_status(pairs)
    report = {
        "pairs": len(pairs),
        "by_source": _by_source(pairs),
        "contexts": sorted({p.context for p in pairs}),
        "gate": gate,
        "scoreboard_health": _scoreboard_health(),
        "model": None,
    }
    if fit:
        if not enabled():
            raise RLScaffoldError(
                "RL scaffold is disabled — set OLYMPUS_RL_SCAFFOLD to fit the "
                "offline reward model. It changes no behavior; it is evidence.")
        if not gate["met"]:
            report["model"] = {"fitted": False, "gate_reasons": gate["reasons"]}
        else:
            model = RewardModel()
            summary = model.fit(pairs)
            report["model"] = {**summary, "top_features": model.top_features(),
                               "weights": model.to_dict()["weights"]}
    return report


def export(*, fit: bool = True) -> dict:
    """Sign a content-safe snapshot of the dataset report on the delta substrate
    (tamper-evident, head-anchored). GATED: requires the operator opt-in."""
    if not enabled():
        raise RLScaffoldError(
            "RL scaffold is disabled — set OLYMPUS_RL_SCAFFOLD to export.")
    report = collect_report(fit=fit)
    snap = deltas.record_snapshot(
        RL_DATASET, kind="rl-scaffold", state=report,
        provenance=deltas.Provenance(source="rlscaffold", trust="operator",
                                     detail=f"offline preference export "
                                            f"({report['pairs']} pairs)"))
    report["snapshot_hash"] = snap["snapshot_hash"]
    return report


def history() -> list[dict]:
    """Every signed RL-scaffold export (verify with deltas.verify_history)."""
    return deltas.snapshots(RL_DATASET)


def render_report(report: dict) -> str:
    """Operator-facing rendering of a dataset report."""
    g = report["gate"]
    lines = [
        "# Offline RL preference-data & reward-model scaffold",
        f"pairs: {report['pairs']}  "
        f"sources: {report['by_source']}  "
        f"contexts: {len(report['contexts'])}",
        f"data gate: {'MET' if g['met'] else 'NOT MET'}"
        + ("" if g["met"] else "  — " + "; ".join(g["reasons"])),
        f"supervision health: {report['scoreboard_health']}",
        "",
        "This is an OFFLINE, advisory scaffold. It updates no model, changes no",
        "routing, and runs no training loop. Scores are evidence for a human.",
    ]
    m = report.get("model")
    if m is None:
        lines += ["", "(reward model not fit — pass --fit with OLYMPUS_RL_SCAFFOLD)"]
    elif not m.get("fitted"):
        lines += ["", "reward model NOT fit — below the data gate:"]
        lines += [f"  - {r}" for r in m.get("gate_reasons", [])]
    else:
        lines += ["", f"## Offline reward model — {m['pairs']} pairs, "
                  f"{m['features']} features, accuracy {m['accuracy']}, "
                  f"loss {m['final_loss']}",
                  "most reward-positive decisions:"]
        lines += [f"  + {f}: {w}" for f, w in m["top_features"]["most_positive"]]
        lines.append("most reward-negative decisions:")
        lines += [f"  - {f}: {w}" for f, w in m["top_features"]["most_negative"]]
    return "\n".join(lines)
