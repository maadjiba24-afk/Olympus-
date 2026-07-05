"""Learned model routing — SPEC-04 Phase B. Evidence-gated, heuristic-fallback.

Phase A (routing_outcomes.py) records which model each specialist actually ran
on and whether the run succeeded. Phase B closes the loop: when choosing a model
for a specialist, prefer the pool member with the best RECORDED success rate —
but only when the evidence clears every gate below. Anything short of full
evidence falls back to the existing keyword heuristic (config.capability_score),
byte-for-byte. The selector can therefore never be worse than the heuristic on a
deployment without data: with no data it simply never engages.

Activation requires ALL of:
  1. The operator opt-in:  OLYMPUS_LEARNED_ROUTING=1  (default OFF).
  2. NOT replaying (OLYMPUS_REPLAY) — replay must reproduce the recorded
     decisions, never re-decide them against a ledger that has since grown.
  3. The SPEC-04 Phase B data gate (routing_outcomes.gate_status): enough
     labeled, non-synthetic outcomes across enough task-types and real sources.
  4. Per-decision evidence: the challenger cell (specialist, model) has at least
     MIN_CELL_SAMPLES labeled real outcomes, AND the incumbent (the heuristic's
     pick) is ALSO known with a strictly lower Wilson lower bound. No evidence
     about the incumbent ⇒ no override — we never displace the heuristic on
     one-sided data.

Statistics, deliberately boring (stdlib only, no ML dependency): outcomes are
weighted (positive = 1.0, approved_after_edit = 0.5, negative = 0.0) and ranked
by the Wilson score lower bound at 95% — a small-sample-conservative estimate
that a high rate on 3 runs never beats a decent rate on 80.

Selection precedence in ModelPool.for_specialist:
  explicit pin (OLYMPUS_SPECIALIST_MODELS)  >  learned (this module)  >
  role heuristic (capability_score).
Sovereign mode (SPEC-02) filters the pool BEFORE any of this runs, so the
selector can only ever choose among already-eligible members.
"""

from __future__ import annotations

import math
import os
import threading
import time

# Minimum labeled, non-synthetic outcomes a (specialist, model) cell needs
# before the selector treats its success rate as known.
MIN_CELL_SAMPLES = 25

# Success weights per outcome signal (approved_after_edit is a half-win: the
# model produced something usable, but not as prepared).
_WEIGHT = {"positive": 1.0, "approved_after_edit": 0.5, "negative": 0.0}

_Z = 1.96                              # 95% Wilson interval

# The ledger is re-read at most once per TTL (it's a few thousand rows of JSON;
# for_specialist runs several times per request).
_CACHE_TTL = 60.0
_cache: dict = {"ts": 0.0, "cells": None, "gate_met": None}
_CACHE_LOCK = threading.Lock()


def enabled() -> bool:
    """The operator opt-in, forced OFF during replay (a replayed run must
    reproduce its recorded decisions, not re-decide them)."""
    if os.environ.get("OLYMPUS_REPLAY"):
        return False
    return os.environ.get("OLYMPUS_LEARNED_ROUTING", "").strip().lower() in (
        "1", "true", "yes", "on")


def wilson_lower_bound(successes: float, n: int, z: float = _Z) -> float:
    """Lower bound of the Wilson score interval — a pessimistic success-rate
    estimate that shrinks toward 0 on small samples, so sparse cells can't win
    on luck. (Weighted successes make this an approximation of the binomial
    case; it stays monotone and conservative, which is all ranking needs.)"""
    if n <= 0:
        return 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def clear_cache() -> None:
    """Forget the aggregated ledger (tests, and after bulk imports)."""
    with _CACHE_LOCK:
        _cache["ts"] = 0.0
        _cache["cells"] = None
        _cache["gate_met"] = None


def _aggregate() -> tuple[dict, bool]:
    """(cells, gate_met) from the Phase A ledger — labeled, non-synthetic rows
    only, aggregated across this instance's users. cells maps
    (specialist, model) -> {n, successes, rate, lb, known}."""
    from . import routing_outcomes
    rows = routing_outcomes._all_rows()
    labeled = routing_outcomes._labeled(rows)
    cells: dict[tuple[str, str], dict] = {}
    for r in labeled:
        keyc = (r.get("specialist", ""), r.get("model", ""))
        c = cells.setdefault(keyc, {"n": 0, "successes": 0.0})
        c["n"] += 1
        c["successes"] += _WEIGHT.get(r.get("outcome_signal"), 0.0)
    for c in cells.values():
        c["rate"] = round(c["successes"] / c["n"], 4) if c["n"] else 0.0
        c["lb"] = round(wilson_lower_bound(c["successes"], c["n"]), 4)
        c["known"] = c["n"] >= MIN_CELL_SAMPLES
    gate_met = bool(routing_outcomes.gate_status(rows)["met"])
    return cells, gate_met


def _cells() -> tuple[dict, bool]:
    now = time.time()
    with _CACHE_LOCK:
        if _cache["cells"] is not None and now - _cache["ts"] < _CACHE_TTL:
            return _cache["cells"], _cache["gate_met"]
    cells, gate_met = _aggregate()
    with _CACHE_LOCK:
        _cache.update(ts=now, cells=cells, gate_met=gate_met)
    return cells, gate_met


def choose(members, specialist: str, heuristic_pick):
    """The learned pick among `members` for `specialist`, or None to defer to
    the heuristic. Never raises into the caller; any failure means None.

    Override rule (conservative by construction): the best known cell by Wilson
    lower bound wins ONLY if the incumbent's cell is also known and strictly
    worse. One-sided evidence never displaces the heuristic."""
    try:
        if not enabled() or len(members) <= 1:
            return None
        cells, gate_met = _cells()
        if not gate_met or not cells:
            return None
        scored = []
        for m in members:
            c = cells.get((specialist, m.model or ""))
            if c and c["known"]:
                scored.append((c["lb"], m))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0], reverse=True)
        best_lb, best = scored[0]
        if best is heuristic_pick or best.model == heuristic_pick.model:
            return best                 # agreement — same model, learned basis
        inc = cells.get((specialist, heuristic_pick.model or ""))
        if not inc or not inc["known"]:
            return None                 # incumbent unknown ⇒ never override
        if best_lb > inc["lb"]:
            return best
        return None
    except Exception:
        return None                     # telemetry must never break routing


def status() -> dict:
    """Operator/auditor view for `olympus routing-stats`: activation state and
    the per-cell evidence table the selector would decide from."""
    try:
        cells, gate_met = _aggregate()   # fresh, not cached — it's a CLI view
    except Exception:
        cells, gate_met = {}, False
    flag = os.environ.get("OLYMPUS_LEARNED_ROUTING", "").strip().lower() in (
        "1", "true", "yes", "on")
    return {
        "flag_enabled": flag,
        "gate_met": gate_met,
        "active": flag and gate_met and not os.environ.get("OLYMPUS_REPLAY"),
        "min_cell_samples": MIN_CELL_SAMPLES,
        "cells": [
            {"specialist": k[0], "model": k[1], "n": c["n"],
             "rate": c["rate"], "wilson_lb": c["lb"], "known": c["known"]}
            for k, c in sorted(cells.items(),
                               key=lambda kv: -kv[1]["lb"])
        ],
    }
