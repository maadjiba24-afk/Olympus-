"""Milestone 2.1 — checkpointed execution ledger (Plane 2).

Every step is a signed, content-addressed checkpoint node; a linear run is a
hash chain (branching factor 1). The Done bar:

  * a run killed mid-flight RESUMES and completes;
  * its replay hash is BYTE-IDENTICAL to an uninterrupted control run;
  * checkpoints VERIFY under the witness (0.1) key — and any tamper is rejected.
"""

import json

import pytest

from olympus import config, ledger, witness

_signing = pytest.mark.skipif(not witness.available(),
                              reason="cryptography backend unavailable")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("OLYMPUS_SIGNING_SEED", "m2.1-ledger-seed")
    # A fresh per-run lock table so nothing leaks across tests.
    monkeypatch.setattr(ledger, "_locks", {})


# The work under checkpoint: a deterministic accumulator.
_PLAN = [{"action": "add", "n": i} for i in range(1, 6)]


def _executor(prev_state, step, index):
    total = (prev_state or {}).get("total", 0) + step["n"]
    return {"total": total, "at": index}


# --- content-addressed, signed linear chain -------------------------------

@_signing
def test_chain_is_content_addressed_signed_and_linked():
    rid = "run-chain"
    led = ledger.open_run(rid)
    for i, step in enumerate(_PLAN):
        led.append(step, {"i": i})
    nodes = led.nodes
    assert [n["seq"] for n in nodes] == list(range(len(_PLAN)))
    assert nodes[0]["parent"] is None
    for prev, node in zip(nodes, nodes[1:]):
        assert node["parent"] == prev["node_hash"]        # hash chain
    v = ledger.verify_ledger(rid)
    assert v["ok"] and v["verified"] == len(_PLAN) and v["attested"] is True


# --- kill mid-flight → resume → byte-identical replay hash ----------------

@_signing
def test_killed_run_resumes_and_completes_byte_identical():
    # Control: an uninterrupted run of the whole plan.
    ctrl = ledger.drive("run-control", _PLAN, _executor)
    control_hash = ledger.replay_hash("run-control")
    assert ctrl["total"] == 15

    # A run killed after 2 committed steps (stop_before=2).
    ledger.drive("run-killed", _PLAN, _executor, stop_before=2)
    assert ledger.open_run("run-killed").next_seq() == 2
    assert ledger.replay_hash("run-killed") != control_hash    # incomplete

    # Re-drive the SAME plan: it skips the 2 committed steps and finishes.
    resumed = ledger.drive("run-killed", _PLAN, _executor)
    assert resumed["total"] == 15
    assert ledger.open_run("run-killed").next_seq() == len(_PLAN)
    # Byte-identical to the uninterrupted control.
    assert ledger.replay_hash("run-killed") == control_hash
    assert ledger.verify_ledger("run-killed")["ok"]


@_signing
def test_resume_executes_only_the_remaining_steps():
    calls = []

    def counting(prev, step, index):
        calls.append(index)
        return _executor(prev, step, index)

    ledger.drive("run-r", _PLAN, counting, stop_before=3)
    assert calls == [0, 1, 2]
    calls.clear()
    ledger.drive("run-r", _PLAN, counting)             # resume
    assert calls == [3, 4]                             # committed steps skipped


# --- verification / tamper rejection --------------------------------------

def _rewrite(rid, nodes):
    ledger._path(rid).write_text(
        "\n".join(json.dumps(n) for n in nodes) + "\n", encoding="utf-8")


@_signing
def test_tampered_step_fails_verify():
    rid = "run-tamper"
    ledger.drive(rid, _PLAN, _executor)
    nodes = ledger._read_nodes(rid)
    nodes[2]["step"] = {"action": "add", "n": 999}     # forge a step post-sign
    _rewrite(rid, nodes)
    v = ledger.verify_ledger(rid)
    assert not v["ok"] and v["verified"] == 2
    assert any("seq 2" in p for p in v["problems"])


@_signing
def test_broken_parent_link_fails_verify():
    rid = "run-link"
    ledger.drive(rid, _PLAN, _executor)
    nodes = ledger._read_nodes(rid)
    nodes[3]["parent"] = "00" * 32                     # sever the chain
    _rewrite(rid, nodes)
    v = ledger.verify_ledger(rid)
    assert not v["ok"] and v["verified"] == 3


@_signing
def test_forged_signature_rejected():
    rid = "run-forge"
    ledger.drive(rid, _PLAN, _executor)
    nodes = ledger._read_nodes(rid)
    nodes[1]["signature"] = "ab" * 32                  # bogus signature
    _rewrite(rid, nodes)
    v = ledger.verify_ledger(rid)
    assert not v["ok"] and v["verified"] == 1


@_signing
def test_reordered_nodes_fail_verify():
    rid = "run-reorder"
    ledger.drive(rid, _PLAN, _executor)
    nodes = ledger._read_nodes(rid)
    nodes[1], nodes[2] = nodes[2], nodes[1]            # swap two committed nodes
    _rewrite(rid, nodes)
    assert not ledger.verify_ledger(rid)["ok"]


# --- resume safety ---------------------------------------------------------

@_signing
def test_resume_with_divergent_plan_is_refused():
    rid = "run-diverge"
    ledger.drive(rid, _PLAN, _executor, stop_before=2)
    other = [{"action": "add", "n": 42}] + _PLAN[1:]   # step 0 differs
    with pytest.raises(ledger.LedgerError):
        ledger.drive(rid, other, _executor)


@_signing
def test_torn_final_line_is_tolerated_on_resume():
    rid = "run-torn"
    ledger.drive(rid, _PLAN, _executor, stop_before=3)
    # Simulate a write interrupted by a kill: append a partial, unparseable line.
    with ledger._path(rid).open("a", encoding="utf-8") as f:
        f.write('{"seq": 3, "step": {"action": "ad')      # torn
    assert ledger.open_run(rid).next_seq() == 3            # torn line dropped
    resumed = ledger.drive(rid, _PLAN, _executor)          # resumes cleanly
    assert resumed["total"] == 15 and ledger.verify_ledger(rid)["ok"]


@_signing
def test_complete_final_line_without_newline_is_preserved_on_resume():
    # A kill can flush a complete node's JSON but drop the trailing newline.
    # That node IS committed; resume must keep it (not truncate it) and continue.
    rid = "run-nonl"
    ledger.drive(rid, _PLAN, _executor, stop_before=3)
    raw = ledger._path(rid).read_bytes().rstrip(b"\n")     # strip the separator
    ledger._path(rid).write_bytes(raw)                     # 3rd node, no newline
    assert ledger.open_run(rid).next_seq() == 3            # still committed
    resumed = ledger.drive(rid, _PLAN, _executor)          # resumes, keeps node 2
    assert resumed["total"] == 15
    # No lost node, no seq gap: contiguous chain 0..4 that verifies.
    assert [n["seq"] for n in ledger.open_run(rid).nodes] == list(range(5))
    assert ledger.verify_ledger(rid)["ok"]
    # And byte-identical to an uninterrupted control of the same plan.
    ledger.drive("run-nonl-ctrl", _PLAN, _executor)
    assert ledger.replay_hash(rid) == ledger.replay_hash("run-nonl-ctrl")


def test_default_seed_chain_is_unattested(monkeypatch):
    monkeypatch.delenv("OLYMPUS_SIGNING_SEED", raising=False)
    if not witness.available():
        pytest.skip("crypto unavailable")
    rid = "run-devseed"
    ledger.drive(rid, _PLAN, _executor)
    v = ledger.verify_ledger(rid)
    assert v["ok"] and v["attested"] is False             # signed, not attested


def test_invalid_run_id_rejected():
    with pytest.raises(ledger.LedgerError):
        ledger.open_run("../escape").append({"a": 1}, {})


def test_empty_run_verifies_false():
    v = ledger.verify_ledger("never-existed")
    assert not v["ok"] and not v["found"]
