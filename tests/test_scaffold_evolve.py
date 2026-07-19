"""D4 — governed scaffold evolution (propose-only, ADR 0003). The tests are the
safety proof: security modules are unreachable, nothing is ever written to the
real source tree, no apply path exists, and failing candidates are excluded."""

import pytest

from olympus import scaffold_evolve as se, behavioral_contracts as abc


# --- fail-closed module allowlist (the core guarantee) --------------------

def test_security_modules_are_never_evolvable():
    for m in ("security", "cmdguard", "actions", "behavioral_contracts",
              "mandate", "witness", "vault", "egress", "capprofile",
              "secretref", "sandbox", "approvals", "config", "builtin_actions"):
        assert not se.is_evolvable(m), m
        assert not se.is_evolvable(f"olympus/{m}.py"), m


def test_unknown_module_is_not_evolvable():
    assert not se.is_evolvable("does_not_exist")
    assert not se.is_evolvable("orchestrator")   # not on the allowlist → refused


def test_allowlisted_module_is_evolvable():
    assert se.is_evolvable("transcript")
    assert se.is_evolvable("i18n.py")


def test_propose_refuses_a_security_module():
    with pytest.raises(ValueError):
        se.propose("security", lambda before: before)
    with pytest.raises(ValueError):
        se.propose("behavioral_contracts", lambda before: before + "\n")


def test_no_apply_function_exists():
    # propose-only: there must be no way to apply a candidate to the source tree
    assert not hasattr(se, "apply")
    assert not hasattr(se, "apply_proposal")
    assert not hasattr(se, "write_module")


# --- propose never touches the real source tree ---------------------------

def test_propose_does_not_modify_the_real_module():
    original = se._module_path("transcript").read_text(encoding="utf-8")

    def generate(before):
        return before + "\n# a proposed comment\n"

    se.propose("transcript", generate)
    after = se._module_path("transcript").read_text(encoding="utf-8")
    assert after == original            # the real file is untouched


# --- benchmark gate: only compiling + passing candidates are surfaced -----

def test_compiling_candidate_is_valid_and_surfaced():
    cand = se.propose("transcript", lambda b: b + "\n# ok\n")
    assert cand.compiles and cand.benchmark_passed and cand.valid
    surfaced = se.proposals("transcript", valid_only=True)
    assert any(c["id"] == cand.id for c in surfaced)


def test_noncompiling_candidate_is_archived_but_not_surfaced():
    cand = se.propose("transcript", lambda b: b + "\nthis is not python!!!\n")
    assert not cand.compiles and not cand.valid
    assert cand.id in {c["id"] for c in se.archive()}          # archived
    assert cand.id not in {c["id"] for c in se.proposals(valid_only=True)}  # hidden


def test_benchmark_failure_excludes_candidate():
    def failing_benchmark(tmp_path, module):
        return False, 0.0
    cand = se.propose("transcript", lambda b: b + "\n# x\n",
                      benchmark=failing_benchmark)
    assert cand.compiles and not cand.benchmark_passed and not cand.valid


def test_empty_candidate_is_rejected():
    cand = se.propose("transcript", lambda b: "   ")
    assert not cand.compiles and not cand.valid


# --- the scaffold.propose contract ----------------------------------------

def test_contract_present_and_blocks_bad_candidates():
    reg = abc.load()
    assert abc.contracts_for("scaffold.propose", reg)
    with pytest.raises(abc.ContractViolation):
        abc.enforce("scaffold.propose", {"target_evolvable": False,
                                         "candidate_compiles": True,
                                         "benchmark_passed": True})
    with pytest.raises(abc.ContractViolation):
        abc.enforce("scaffold.propose", {"target_evolvable": True,
                                         "candidate_compiles": False,
                                         "benchmark_passed": True})
    with pytest.raises(abc.ContractViolation):
        abc.enforce("scaffold.propose", {"target_evolvable": True,
                                         "candidate_compiles": True,
                                         "benchmark_passed": False})


def test_yaml_and_embedded_still_in_sync():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(abc._yaml_path().read_text(encoding="utf-8"))
    assert data["contracts"] == abc._DEFAULT_CONTRACTS


# --- archive + diff surfacing ---------------------------------------------

def test_render_diff_shows_change_and_no_autoapply_note():
    cand = se.propose("transcript", lambda b: b + "\n# proposed line\n")
    diff = se.render_diff(cand)
    assert "olympus/transcript.py" in diff
    assert "+# proposed line" in diff
    assert "nothing auto-applies" in diff


def test_archive_is_bounded(monkeypatch):
    monkeypatch.setattr(se, "_MAX_ARCHIVE", 5)
    for i in range(10):
        se.propose("transcript", lambda b, i=i: b + f"\n# {i}\n")
    assert len(se.archive()) <= 5


def test_off_by_default():
    import os
    os.environ.pop("OLYMPUS_SCAFFOLD_EVOLVE", None)
    assert se.enabled() is False
