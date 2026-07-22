"""Native quorum consensus — agreement primitives + multi-verifier aggregation.

Ruflo ships Raft/Byzantine consensus that, by default, runs over an in-process
event emitter — distributed-systems machinery with no distributed system beneath
it. Olympus takes the honest slice of that idea: agreement among AGENTS. A single
verifier can be confidently wrong, so where accuracy matters a QUORUM of
independent verifiers — each given a different lens — must agree before a verdict
stands. This module is the pure tally core (majority, Byzantine ⌊2n/3⌋+1
supermajority, weighted vote) plus `safest_verdict`, the safety-biased
aggregator the orchestrator uses to fold N parallel Aletheia verdicts into one.

Pure and deterministic (mirroring `dytopo`/`emem`): tallies break ties by
`(count desc, value asc)`, so the same votes always yield the same winner. No
clock, no rng, no I/O — replay-safe by construction. Opt-in via
`OLYMPUS_CONSENSUS` (default OFF: a single verifier stands, exactly as today).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_MAX_VERIFIERS = 7          # hard cap on the verifier fan-out
_DEFAULT_VERIFIERS = 3


def enabled() -> bool:
    """Whether verification runs as a multi-verifier quorum. Default OFF — the
    single-verifier path is unchanged until an operator opts in."""
    return os.environ.get("OLYMPUS_CONSENSUS", "").strip().lower() in (
        "1", "on", "true", "yes")


def verifier_count() -> int:
    """How many verifiers to fan out to (`OLYMPUS_CONSENSUS_VERIFIERS`, default
    3), clamped to an odd count in [1, _MAX_VERIFIERS] so a majority always
    exists."""
    try:
        n = int(os.environ.get("OLYMPUS_CONSENSUS_VERIFIERS",
                               str(_DEFAULT_VERIFIERS)))
    except (TypeError, ValueError):
        n = _DEFAULT_VERIFIERS
    n = max(1, min(n, _MAX_VERIFIERS))
    return n if n % 2 == 1 else n + 1        # keep it odd (no split decisions)


# --- pure tally primitives -----------------------------------------------

def tally(votes) -> dict:
    """Count occurrences of each vote value. Deterministic dict (insertion order
    is irrelevant; callers that need order use `decide`)."""
    counts: dict = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    return counts


def majority_threshold(n: int) -> int:
    """Strict majority: more than half."""
    return n // 2 + 1


def byzantine_threshold(n: int) -> int:
    """Byzantine-fault-tolerant supermajority ⌊2n/3⌋+1 — for n = 3f+1 this is
    the classic 2f+1 agreement that tolerates f faulty/adversarial voters."""
    return (2 * n) // 3 + 1


@dataclass
class ConsensusResult:
    winner: object
    count: int
    n: int
    threshold: int
    met: bool
    counts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"winner": self.winner, "count": self.count, "n": self.n,
                "threshold": self.threshold, "met": self.met,
                "counts": dict(self.counts)}


def decide(votes, *, byzantine: bool = False) -> ConsensusResult:
    """The winning vote and whether it clears the quorum threshold. The winner
    is the highest-count value, ties broken by `str(value)` ascending for full
    determinism. `met` is False when no value reaches quorum (a genuine
    no-agreement outcome the caller must handle, never a silent pick)."""
    votes = list(votes)
    n = len(votes)
    counts = tally(votes)
    if n == 0:
        return ConsensusResult(None, 0, 0, 0, False, counts)
    thr = byzantine_threshold(n) if byzantine else majority_threshold(n)
    # Winner = highest count; ties broken by str(value) ascending (determinism).
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    winner, count = ranked[0]
    return ConsensusResult(winner, count, n, thr, count >= thr, counts)


def decide_weighted(weighted_votes, *, byzantine: bool = False
                    ) -> ConsensusResult:
    """Weighted variant: `weighted_votes` is an iterable of `(value, weight)`.
    The winner carries the greatest summed weight; the threshold is a majority
    (or ⌊2/3⌋+1) of TOTAL weight. Ties broken by `str(value)` ascending."""
    weights: dict = {}
    total = 0.0
    for value, w in weighted_votes:
        w = max(0.0, float(w))
        weights[value] = weights.get(value, 0.0) + w
        total += w
    if total <= 0:
        return ConsensusResult(None, 0, 0, 0, False, weights)
    ranked = sorted(weights.items(), key=lambda kv: (-kv[1], str(kv[0])))
    winner, wsum = ranked[0]
    thr = (2 * total) / 3 if byzantine else total / 2
    return ConsensusResult(winner, wsum, total, thr, wsum > thr, weights)


# --- verdict aggregation (the orchestrator's use) ------------------------

_SEVERITY = {"reject": 2, "warn": 1, "pass": 0}


def safest_verdict(verdicts, *, byzantine: bool = False) -> dict:
    """Fold N parsed Aletheia verdicts (`{status, unsupported_claims,
    confidence}`) into one, SAFETY-biased so a real problem is never voted away:

      * reject if `reject` reaches quorum — a quorum of verifiers seeing a
        fabrication outweighs the rest.
      * pass only if `pass` reaches quorum AND no verifier rejected — unanimous-
        enough clean bill of health.
      * warn otherwise — any unresolved dissent lands on the cautious verdict
        rather than silently passing.

    `unsupported_claims` is the de-duped union from every non-pass verdict (a
    concern one verifier raised is kept even if outvoted). `confidence` is the
    mean. Returns the standard verdict dict plus an `agreement` fraction and the
    per-status `votes` tally for the audit log."""
    verdicts = [v for v in verdicts if isinstance(v, dict) and v.get("status")]
    if not verdicts:
        return {"status": "warn", "unsupported_claims": [], "confidence": 0.0,
                "agreement": 0.0, "votes": {}}
    statuses = [v["status"] for v in verdicts]
    n = len(statuses)
    thr = byzantine_threshold(n) if byzantine else majority_threshold(n)
    counts = tally(statuses)
    rejects = counts.get("reject", 0)
    passes = counts.get("pass", 0)
    if rejects >= thr:
        status = "reject"
    elif passes >= thr and rejects == 0:
        status = "pass"
    else:
        status = "warn"

    # A `pass` verdict must not carry unsupported claims — that would be an
    # internally-inconsistent verdict dict (clean bill of health + listed
    # concerns). Claims are only meaningful on warn/reject.
    claims: list[str] = []
    if status != "pass":
        seen: set[str] = set()
        for v in verdicts:
            if v["status"] == "pass":
                continue
            for c in v.get("unsupported_claims", []) or []:
                if c not in seen:
                    seen.add(c)
                    claims.append(c)
    confs = [float(v.get("confidence", 0.0)) for v in verdicts]
    mean_conf = round(sum(confs) / n, 4) if n else 0.0
    agreement = round(counts.get(status, 0) / n, 4)
    return {"status": status, "unsupported_claims": claims[:20],
            "confidence": mean_conf, "agreement": agreement,
            "votes": dict(counts)}


# The lenses each parallel verifier is given, so the quorum catches distinct
# failure modes (perspective-diverse verification) rather than N identical runs.
LENSES = (
    "FACTUAL ACCURACY: are the concrete claims true, current, and sourced?",
    "LOGICAL SOUNDNESS: do the conclusions actually follow from the evidence?",
    "COMPLETENESS: is anything material missing, overstated, or unsupported?",
    "ADVERSARIAL: actively try to find one fabricated or unsupported claim.",
    "CONSISTENCY: do the specialist outputs contradict each other or themselves?",
    "GROUNDING: is every consequential claim tied to a verifiable source?",
    "SCOPE: does the answer address the actual brief, not a nearby question?",
)


def lens_for(index: int) -> str:
    return LENSES[index % len(LENSES)]
