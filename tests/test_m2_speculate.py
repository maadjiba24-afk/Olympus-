"""Milestone 2.2 — speculative branches as uncommitted checkpoint branches.

Best-first tree search forks the committed ledger into UNCOMMITTED branches:
exploration touches only read-only/reversible steps, hard caps bound it, every
pruned branch keeps a signed ledger record, and committing a side-effectful
branch HALTS for the approval gate rather than auto-taking it.

Done bar: caps, halt-on-commit, and prune-with-record — all proven here.
"""

import json

import pytest

from olympus import config, ledger, speculate, treesearch, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m2.2-speculate-seed")
    monkeypatch.setattr(ledger, "_locks", {})


class _Adder:
    """Reach `target` from 0. Each expand offers a few read-only increments plus
    one side-effectful 'submit' — so exploration is safe and branchy, and any
    plan that would *act* must pass through the approval gate."""

    def __init__(self, target: int, branch: int = 3):
        self.target = target
        self.branch = branch

    def expand(self, state):
        steps = [treesearch.Step(f"add{k}", {"k": k}, treesearch.READ_ONLY,
                                 cost_tokens=1)
                 for k in range(1, self.branch + 1)]
        steps.append(treesearch.Step("submit", {}, treesearch.SIDE_EFFECTFUL))
        return steps

    def apply(self, state, step):
        return state + int(step.args.get("k", 0))

    def evaluate(self, state):
        return -abs(self.target - state)

    def is_goal(self, state):
        return state == self.target


# --- exploration records signed branches, never touches the committed chain --

@_signing
def test_explore_records_signed_branches_and_leaves_chain_untouched():
    spec = speculate.explore("run-x", _Adder(3), 0,
                             treesearch.SearchCaps(max_nodes=8, max_depth=4))
    assert spec.branches, "exploration should record branch nodes"
    # A side-effectful 'submit' is withheld, not explored.
    assert any(b["info"]["step"]["action"] == "submit" and
               not b["info"]["explored"] for b in spec.branches)
    v = ledger.verify_speculation("run-x")
    assert v["ok"] and v["attested"] is True
    # Speculation is UNCOMMITTED: the committed checkpoint chain stays empty.
    assert ledger.open_run("run-x").next_seq() == 0


# --- hard caps trip and prune records are written -------------------------

@_signing
def test_node_cap_trips_and_prunes_are_recorded():
    spec = speculate.explore("run-cap", _Adder(50, branch=4), 0,
                             treesearch.SearchCaps(max_nodes=3))
    assert spec.status == treesearch.NODE_CAP
    assert spec.prunes, "a tripped cap must record the pruned frontier"
    assert all(p["info"]["reason"] == treesearch.NODE_CAP for p in spec.prunes)
    assert ledger.verify_speculation("run-cap")["ok"]


@_signing
def test_depth_cap_prunes_are_recorded():
    spec = speculate.explore("run-depth", _Adder(50, branch=2), 0,
                             treesearch.SearchCaps(max_nodes=50, max_depth=1))
    assert any(p["info"]["reason"] == treesearch.DEPTH_CAP for p in spec.prunes)


@_signing
def test_pruned_records_carry_a_reason_and_verify():
    speculate.explore("run-r", _Adder(99, branch=3), 0,
                      treesearch.SearchCaps(max_nodes=4))
    recs = ledger.speculation_records("run-r")
    prunes = [r for r in recs if r["kind"] == "prune"]
    assert prunes and all(r["info"].get("reason") for r in prunes)
    assert ledger.verify_speculation("run-r")["ok"]


# --- committing halts on a side-effectful step ----------------------------

@_signing
def test_commit_safe_branch_appends_real_checkpoints():
    path = [treesearch.Step("add1", {"k": 1}, treesearch.READ_ONLY),
            treesearch.Step("navigate", {"url": "/x"}, treesearch.REVERSIBLE)]
    out = speculate.commit("run-c", path)
    assert out["status"] == "committed" and len(out["committed"]) == 2
    assert ledger.open_run("run-c").next_seq() == 2
    assert ledger.verify_ledger("run-c")["ok"]


@_signing
def test_commit_side_effectful_branch_halts_for_approval():
    path = [treesearch.Step("add1", {"k": 1}, treesearch.READ_ONLY),
            treesearch.Step("submit", {}, treesearch.SIDE_EFFECTFUL),
            treesearch.Step("add2", {"k": 2}, treesearch.READ_ONLY)]
    out = speculate.commit("run-h", path)
    assert out["status"] == treesearch.HALTED
    assert out["halted_step"]["action"] == "submit" and out["index"] == 1
    # Only the safe prefix committed; the side-effectful step was NOT taken.
    assert len(out["committed"]) == 1
    assert ledger.open_run("run-h").next_seq() == 1
    # A signed halt record documents the gate.
    halts = [r for r in ledger.speculation_records("run-h") if r["kind"] == "halt"]
    assert len(halts) == 1 and halts[0]["info"]["step"]["action"] == "submit"
    assert ledger.verify_speculation("run-h")["ok"]


@_signing
def test_commit_classify_override_forces_halt():
    # A step the caller classifies side-effectful halts even if its own label
    # claims read-only (fail-closed via the classifier).
    path = [treesearch.Step("act", {}, treesearch.READ_ONLY)]
    out = speculate.commit("run-cl", path, classify=lambda s: treesearch.SIDE_EFFECTFUL)
    assert out["status"] == treesearch.HALTED and out["committed"] == []


# --- tamper rejection ------------------------------------------------------

@_signing
def test_tampered_speculation_record_rejected():
    speculate.explore("run-t", _Adder(5), 0,
                      treesearch.SearchCaps(max_nodes=5))
    path = ledger._spec_path("run-t")
    recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    recs[0]["info"] = {"step": {"action": "forged"}}       # tamper post-sign
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    v = ledger.verify_speculation("run-t")
    assert not v["ok"] and v["verified"] == 0


# --- backward compatibility ------------------------------------------------

def test_search_without_observer_still_works():
    # The observer param is optional; the core planner is unchanged without it.
    res = treesearch.search(_Adder(2), 0, treesearch.SearchCaps(max_nodes=10))
    assert res.status in (treesearch.GOAL, treesearch.EXHAUSTED,
                          treesearch.HALTED, treesearch.NODE_CAP)


def test_default_seed_speculation_is_unattested(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    if not witness.available():
        pytest.skip("crypto unavailable")
    speculate.explore("run-dev", _Adder(3), 0,
                      treesearch.SearchCaps(max_nodes=5))
    v = ledger.verify_speculation("run-dev")
    assert v["ok"] and v["attested"] is False
