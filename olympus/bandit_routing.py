"""Bandit model routing — UCB1 exploration over the same outcome ledger.

`learned_routing.py` is a CONSERVATIVE exploiter: it only ever moves off the
keyword heuristic when a challenger model has decisively out-performed the
incumbent (Wilson lower bound, both cells well-sampled). That is the right
default, but it never deliberately *tries* an under-sampled model, so a genuinely
better model that simply hasn't been picked yet stays invisible.

This module is the EXPLORER alternative: a UCB1 multi-armed bandit over the same
`routing_outcomes` ledger. Each pool model is an arm; the score is its mean
reward plus an exploration bonus that shrinks as the arm is pulled, so an
under-tried model gets a bounded number of shots before the bandit settles on the
empirical best. It is a drop-in for the `learned_routing.choose` seam (same
signature) and is a MUTUALLY-EXCLUSIVE alternative to it — an operator runs one
or the other.

Deliberately deterministic: UCB1 is argmax over counts, NOT Thompson sampling, so
there is no rng and the same ledger always yields the same pick — replay-safe by
construction. It is nonetheless forced OFF during replay (like the learned
selector), because the ledger grows between record and replay and a replayed run
must reproduce its RECORDED decision, not re-decide against newer data.

Activation requires the opt-in `OLYMPUS_BANDIT_ROUTING=1` (default OFF) and a
short warmup: until a specialist has `MIN_WARMUP` recorded outcomes the heuristic
drives (and seeds the ledger), so the bandit never makes a cold-start pick on no
data.
"""

from __future__ import annotations

import math
import os

_WEIGHT = {"positive": 1.0, "approved_after_edit": 0.5, "negative": 0.0}
_C = math.sqrt(2.0)          # UCB1 exploration constant
MIN_WARMUP = 8               # per-specialist outcomes before the bandit engages


def enabled() -> bool:
    """Opt-in, forced OFF during replay (reproduce recorded decisions)."""
    if os.environ.get("OLYMPUS_REPLAY"):
        return False
    return os.environ.get("OLYMPUS_BANDIT_ROUTING", "").strip().lower() in (
        "1", "true", "yes", "on")


def _arms(specialist: str) -> tuple[dict, int]:
    """Per-model `{model: {n, reward}}` for this specialist and the total pulls,
    from labeled non-synthetic outcomes. Best-effort: ({}, 0) on any error."""
    try:
        from . import routing_outcomes
        rows = routing_outcomes._labeled(routing_outcomes._all_rows())
    except Exception:
        return {}, 0
    arms: dict[str, dict] = {}
    total = 0
    for r in rows:
        if r.get("specialist") != specialist:
            continue
        model = r.get("model", "")
        a = arms.setdefault(model, {"n": 0, "reward": 0.0})
        a["n"] += 1
        a["reward"] += _WEIGHT.get(r.get("outcome_signal"), 0.0)
        total += 1
    return arms, total


def _exploration() -> float:
    """The self-tuned exploration constant (defaults to sqrt(2)). `evolve` lowers
    it when routing outcomes degrade under the bandit, so it explores less and
    trusts the empirical best more. Falls back to the constant on any read
    error."""
    try:
        from . import evolve
        return float(evolve.current("bandit", "exploration"))
    except Exception:
        return _C


def ucb_score(reward: float, n: int, total: int, c: float | None = None) -> float:
    """UCB1 score: mean reward + exploration bonus. An unpulled arm (n=0) scores
    +inf so it is tried before any exploitation. `c` defaults to the self-tuned
    exploration constant."""
    if n <= 0:
        return float("inf")
    if c is None:
        c = _C
    mean = reward / n
    return mean + c * math.sqrt(math.log(max(total, 1)) / n)


def choose(members, specialist: str, heuristic_pick):
    """The bandit's pick among `members` for `specialist`, or None to defer to
    the heuristic. Same contract as `learned_routing.choose`: never raises, and
    None means "no opinion, use the heuristic".

    Deterministic: ties in UCB score break by model name ascending. Below the
    per-specialist warmup the bandit abstains so the heuristic can seed the
    ledger first."""
    try:
        if not enabled() or len(members) <= 1:
            return None
        arms, _ = _arms(specialist)
        # UCB horizon = pulls of the CURRENT arms only. Counting pulls of
        # since-removed models would inflate the sqrt(log(total)/n) exploration
        # term and skew the ranking between surviving arms (textbook UCB1 sums
        # over the active arm set).
        total = sum(arms.get(m.model or "", {"n": 0})["n"] for m in members)
        if total < MIN_WARMUP:
            return None                    # warm up on the heuristic first
        c = _exploration()                 # self-tuned exploration constant
        scored = []
        for m in members:
            a = arms.get(m.model or "", {"n": 0, "reward": 0.0})
            score = ucb_score(a["reward"], a["n"], total, c)
            scored.append((score, m.model or "", m))
        # Highest UCB wins; ties → model name ascending (fully deterministic).
        scored.sort(key=lambda t: (-t[0], t[1]))
        return scored[0][2]
    except Exception:
        return None                        # routing must never break on telemetry


def status() -> dict:
    """Operator view: activation state and the per-arm UCB table the bandit would
    decide from, per specialist."""
    flag = os.environ.get("OLYMPUS_BANDIT_ROUTING", "").strip().lower() in (
        "1", "true", "yes", "on")
    try:
        from . import routing_outcomes
        rows = routing_outcomes._labeled(routing_outcomes._all_rows())
        specialists = sorted({r.get("specialist", "") for r in rows})
    except Exception:
        specialists = []
    table = []
    for spec in specialists:
        arms, total = _arms(spec)
        for model, a in sorted(arms.items()):
            table.append({
                "specialist": spec, "model": model, "n": a["n"],
                "mean_reward": round(a["reward"] / a["n"], 4) if a["n"] else 0.0,
                "ucb": round(ucb_score(a["reward"], a["n"], total), 4),
                "warm": total >= MIN_WARMUP,
            })
    return {
        "flag_enabled": flag,
        "active": flag and not os.environ.get("OLYMPUS_REPLAY"),
        "min_warmup": MIN_WARMUP,
        "arms": table,
    }
