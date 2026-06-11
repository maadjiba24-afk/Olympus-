"""Tests for benchmark-gated skill creation and auto-generated eval items."""

from olympus import evals, orchestrator, skills


# --- provisional skill lifecycle ----------------------------------------

def test_provisional_flag_and_listing():
    skills.create("Permanent skill", "always on", "do X", provisional=False)
    skills.create("New trick", "maybe", "do Y", specialist="plutus",
                  provisional=True)
    prov = dict(skills.list_provisional())
    assert "New trick" in prov and prov["New trick"] == "plutus"
    assert "Permanent skill" not in prov
    assert "(provisional)" in skills.index()


def test_promote_clears_flag():
    skills.create("Trick", "x", "y", specialist="peitho", provisional=True)
    skills.promote("Trick")
    assert skills.list_provisional() == []
    assert "<!-- provisional -->" not in skills.read("Trick")


def test_revert_deletes_new_skill():
    skills.create("Brand new", "x", "y", provisional=True)
    assert skills.count() == 1
    msg = skills.revert("Brand new")
    assert "removed" in msg
    assert skills.count() == 0


def test_revert_restores_previous_version():
    skills.create("Evolving", "x", "original method", provisional=False)
    skills.create("Evolving", "x", "risky new method", specialist="plutus",
                  provisional=True)
    assert "risky new method" in skills.read("Evolving")
    msg = skills.revert("Evolving")
    assert "reverted" in msg
    assert "original method" in skills.read("Evolving")
    assert "risky new method" not in skills.read("Evolving")


def test_hide_unhide_excludes_from_index():
    skills.create("Hideable", "x", "y", provisional=True)
    skills.set_hidden("Hideable", True)
    assert "Hideable" not in skills.index()
    skills.set_hidden("Hideable", False)
    assert "Hideable" in skills.index()


# --- the gate routine ----------------------------------------------------

def test_gate_promotes_when_score_holds(monkeypatch):
    skills.create("Helpful", "x", "y", specialist="plutus", provisional=True)
    # with-provisional run scores higher than without
    seq = iter([{"avg": 8.0, "items": []}, {"avg": 7.0, "items": []}])
    monkeypatch.setattr(evals, "run", lambda settings, only=None: next(seq))
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    msg = orchestrator.gate_skills()
    assert "Promoted" in msg
    assert skills.list_provisional() == []          # promoted, flag cleared
    assert skills.count() == 1


def test_gate_reverts_when_score_regresses(monkeypatch):
    skills.create("Harmful", "x", "y", specialist="plutus", provisional=True)
    # with-provisional scores LOWER than without -> revert
    seq = iter([{"avg": 6.0, "items": []}, {"avg": 8.0, "items": []}])
    monkeypatch.setattr(evals, "run", lambda settings, only=None: next(seq))
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    msg = orchestrator.gate_skills()
    assert "Reverted" in msg
    assert skills.count() == 0                       # harmful skill removed


def test_gate_generates_eval_for_uncovered_domain(monkeypatch):
    skills.create("Niche", "x", "y", specialist="chiron", provisional=True)
    calls = {"gen": 0}

    def fake_ids(specs):
        # chiron has no items until generate_item is called
        return [] if calls["gen"] == 0 else ["chiron-gen-1"]

    def fake_gen(specialist, settings=None):
        calls["gen"] += 1
        return "generated"

    monkeypatch.setattr(evals, "ids_for", fake_ids)
    monkeypatch.setattr(evals, "generate_item", fake_gen)
    monkeypatch.setattr(evals, "run", lambda settings, only=None: {"avg": 7, "items": []})
    orchestrator.gate_skills()
    assert calls["gen"] == 1                          # auto-generated an eval


def test_gate_noop_without_provisional():
    skills.create("Stable", "x", "y", provisional=False)
    assert "No provisional" in orchestrator.gate_skills()


def test_gate_unhides_on_benchmark_failure(monkeypatch):
    skills.create("Skill", "x", "y", specialist="plutus", provisional=True)

    def boom(settings, only=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(evals, "run", boom)
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["plutus-budget"])
    msg = orchestrator.gate_skills()
    assert "Skipped" in msg                          # benchmark failed for it
    assert "Skill" in skills.index()                 # not left hidden
    assert ("Skill", "plutus") in skills.list_provisional()  # stays provisional


def test_gate_isolates_each_skill_no_riding_along(monkeypatch):
    """Regression: a harmful skill must NOT be promoted by riding along with a
    helpful one. Each skill is gated on its own marginal effect."""
    skills.create("Helpful", "x", "y", specialist="plutus", provisional=True)
    skills.create("Harmful", "x", "y", specialist="peitho", provisional=True)

    def scorer(settings, only=None):
        idx = skills.index()
        if "Harmful" not in idx:    # removing Harmful scores BETTER -> revert it
            return {"avg": 8.0, "items": []}
        if "Helpful" not in idx:    # removing Helpful scores WORSE -> keep it
            return {"avg": 6.0, "items": []}
        return {"avg": 7.0, "items": []}   # both visible

    monkeypatch.setattr(evals, "run", scorer)
    monkeypatch.setattr(evals, "ids_for", lambda specs: ["x"])
    orchestrator.gate_skills()
    names = skills.index()
    assert "Harmful" not in names, "harmful skill rode along and survived!"
    assert "Helpful" in names
    assert skills.list_provisional() == []   # decided: none left provisional


# --- auto-generated benchmark items merge --------------------------------

def test_add_item_merges_into_benchmarks(monkeypatch):
    base = len(evals.load_benchmarks())
    evals.add_item({"id": "plutus-extra-1", "specialist": "plutus",
                    "task": "t", "criteria": "c"})
    items = evals.load_benchmarks()
    assert len(items) == base + 1
    assert any(i["id"] == "plutus-extra-1" for i in items)
    assert "plutus-extra-1" in evals.ids_for(["plutus"])
    # adding same id replaces, not duplicates
    evals.add_item({"id": "plutus-extra-1", "specialist": "plutus",
                    "task": "t2", "criteria": "c2"})
    assert len(evals.load_benchmarks()) == base + 1
