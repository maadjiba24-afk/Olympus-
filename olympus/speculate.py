"""Speculative branches (Plane 2.2) — best-first tree search as UNCOMMITTED
checkpoint branches forked off the committed execution ledger.

This fuses two things that already exist rather than building a third engine:

  * `treesearch` — the bounded best-first planner. It already explores ONLY
    read-only/reversible steps, withholds every side-effectful step for the
    approval gate, and enforces hard caps (nodes / tokens / wall-clock / depth).
  * `ledger` — the signed, content-addressed checkpoint chain (Plane 2.1).

`explore()` runs the search with a ledger-recording observer, so every branch it
generates is a signed, uncommitted speculation record and every pruned branch
keeps a signed ledger record (with the reason it was cut). `commit()` then folds
a chosen branch into the committed chain — appending its safe steps as real
checkpoints and HALTING at the first side-effectful step for the approval gate.
Speculation never mutates the world; committing a side-effectful branch is a
halt, not an auto-take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import ledger, treesearch


@dataclass
class Speculation:
    """The outcome of a bounded exploration — the best safe path found, the
    side-effectful steps withheld for approval, and the signed audit records."""
    status: str
    best_path: list
    best_score: float
    pending_approval: list = field(default_factory=list)
    branches: list = field(default_factory=list)   # signed 'branch' records
    prunes: list = field(default_factory=list)      # signed 'prune' records
    stats: dict = field(default_factory=dict)

    @property
    def needs_approval(self) -> bool:
        return self.status == treesearch.HALTED or bool(self.pending_approval)


class _LedgerRecorder:
    """A treesearch observer that persists exploration as signed ledger records."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.branches: list[dict] = []
        self.prunes: list[dict] = []

    def branch(self, info: dict) -> None:
        self.branches.append(
            ledger.record_speculation(self.run_id, "branch", info))

    def prune(self, info: dict) -> None:
        self.prunes.append(
            ledger.record_speculation(self.run_id, "prune", info))


def explore(run_id: str, problem: Any, initial: Any,
            caps: "treesearch.SearchCaps | None" = None, *,
            classify: Callable[[Any], str] | None = None,
            clock: Callable[[], float] | None = None) -> Speculation:
    """Run a bounded best-first search off the run's committed state, recording
    each explored branch and each pruned branch as a signed ledger record. Does
    NOT commit anything — exploration is side-effect-free by construction."""
    rec = _LedgerRecorder(run_id)
    result = treesearch.search(problem, initial, caps, clock=clock,
                               classify=classify, observer=rec)
    return Speculation(
        status=result.status, best_path=list(result.path),
        best_score=result.best_score, pending_approval=result.pending_approval,
        branches=rec.branches, prunes=rec.prunes, stats=result.stats)


def commit(run_id: str, path, *,
           classify: Callable[[Any], str] | None = None,
           executor: Callable[[Any, Any, int], Any] | None = None) -> dict:
    """Fold a chosen speculative branch into the committed checkpoint chain.

    Safe (read-only/reversible) steps are appended as real, signed checkpoints.
    The FIRST side-effectful step HALTS: it is recorded as a signed 'halt'
    speculation record and handed back for the approval gate — commit stops
    there, and nothing side-effectful is ever auto-taken. A branch that is
    entirely safe commits in full.

    Returns {status, committed, halted_step?, index?, effect?}; `status` is
    'committed' or treesearch.HALTED."""
    steps = list(path)
    committed: list[dict] = []
    led = ledger.open_run(run_id)
    state = led.state()
    for i, step in enumerate(steps):
        sd = _step_dict(step)
        effect = treesearch.effect_of(step, classify)
        if effect not in treesearch.SAFE:
            ledger.record_speculation(run_id, "halt", {
                "step": sd, "index": i, "effect": effect,
                "reason": "commit of a side-effectful step requires approval"})
            return {"status": treesearch.HALTED, "committed": committed,
                    "halted_step": sd, "index": i, "effect": effect}
        state = (executor(state, step, i) if executor is not None
                 else {"committed_step": sd, "index": i})
        committed.append(ledger.checkpoint(run_id, sd, state))
    return {"status": "committed", "committed": committed}


def _step_dict(step: Any) -> dict:
    if hasattr(step, "to_dict"):
        return step.to_dict()
    if isinstance(step, dict):
        return step
    return {"action": str(getattr(step, "action", step))}
