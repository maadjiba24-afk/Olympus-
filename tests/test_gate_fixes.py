"""Skill-gate correctness fixes from the audit:
  #2 a scoped skill whose specialist has NO benchmark items must be SKIPPED,
     not silently scored against the whole 50-item benchmark (empty `only=[]`).
  #4 a skill must STRICTLY raise the score to be promoted (ties are reverted).
  #1 prompt upgrades must be code-gated: gate_prompt applies a change only on
     non-regression and rolls it back otherwise (the enforced counterpart to the
     honor-system update_prompt).
"""

import pytest

from olympus import config, evals, orchestrator, skills

_S = config.Settings(provider="anthropic", model="x", api_key="k")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    yield


def test_scoped_skill_without_coverage_is_skipped_not_scored(monkeypatch):
    monkeypatch.setattr(skills, "list_provisional", lambda: [("s1", "ghostspec")])
    monkeypatch.setattr(evals, "ids_for", lambda specs: [])          # no coverage
    monkeypatch.setattr(evals, "generate_item",
                        lambda sp, s: (_ for _ in ()).throw(RuntimeError("nope")))
    ran = {"n": 0}
    monkeypatch.setattr(evals, "run",
                        lambda *a, **k: (ran.__setitem__("n", ran["n"] + 1),
                                         {"avg": 5.0})[1])
    out = orchestrator.gate_skills(_S)
    assert "no benchmark coverage" in out.lower()
    assert ran["n"] == 0            # the bug scored against all 50; fixed = never


def test_tie_is_reverted_not_promoted(monkeypatch):
    monkeypatch.setattr(skills, "list_provisional", lambda: [("s1", "plutus")])
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    monkeypatch.setattr(evals, "run", lambda *a, **k: {"avg": 7.0})  # after==before
    calls = {"promote": [], "revert": [], "hidden": []}
    monkeypatch.setattr(skills, "set_hidden",
                        lambda n, h: calls["hidden"].append((n, h)))
    monkeypatch.setattr(skills, "promote", lambda n: calls["promote"].append(n))
    monkeypatch.setattr(skills, "revert", lambda n: calls["revert"].append(n) or "reverted")
    orchestrator.gate_skills(_S)
    assert calls["revert"] == ["s1"] and calls["promote"] == []   # tie -> revert


# --- #1 gate_prompt: code-enforced measure-and-rollback ------------------

def _prompt_dir(tmp_path, monkeypatch, body="OLD PROMPT"):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "plutus.md").write_text(body + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROMPTS_DIR", d)
    return d


def test_gate_prompt_keeps_change_on_non_regression(tmp_path, monkeypatch):
    d = _prompt_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    scores = iter([7.0, 8.0])              # before, after — improvement
    monkeypatch.setattr(evals, "run", lambda *a, **k: {"avg": next(scores)})
    out = orchestrator.gate_prompt("plutus", "NEW PROMPT", "sharper", settings=_S)
    assert "kept" in out.lower()
    assert (d / "plutus.md").read_text(encoding="utf-8").strip() == "NEW PROMPT"


def test_gate_prompt_reverts_change_on_regression(tmp_path, monkeypatch):
    d = _prompt_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    scores = iter([8.0, 6.0])              # before, after — regression
    monkeypatch.setattr(evals, "run", lambda *a, **k: {"avg": next(scores)})
    out = orchestrator.gate_prompt("plutus", "NEW PROMPT", "risky", settings=_S)
    assert "revert" in out.lower()
    # rolled back to the original prompt, not left in the regressed state
    assert (d / "plutus.md").read_text(encoding="utf-8").strip() == "OLD PROMPT"


def test_gate_prompt_refuses_without_coverage(tmp_path, monkeypatch):
    d = _prompt_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(evals, "ids_for", lambda specs: [])   # no coverage
    ran = {"n": 0}
    monkeypatch.setattr(evals, "run",
                        lambda *a, **k: ran.__setitem__("n", ran["n"] + 1))
    out = orchestrator.gate_prompt("plutus", "NEW PROMPT", "x", settings=_S)
    assert "cannot benchmark-gate" in out.lower()
    assert ran["n"] == 0        # never benchmarked
    # and crucially never applied the unmeasurable change
    assert (d / "plutus.md").read_text(encoding="utf-8").strip() == "OLD PROMPT"
