"""Tests for the code graph wired into the self-evolution loop: claim scanning,
impact reports, the propose_upgrade stamp, Aletheia's ground-truth block, and
the gate CLI action."""

import pytest

from olympus import codegraph, codegraph_build, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    store.reset()
    codegraph.set_enabled(True)
    yield
    codegraph.set_enabled(True)
    store.reset()


def _self_graph(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_text(
        "def caller():\n    helper()\n\ndef helper():\n    pass\n"
        "\ndef orphan():\n    pass\n")
    codegraph_build.build("self", root)


# --- scan_claims ----------------------------------------------------------

def test_scan_claims_finds_all_and_returns_only_decisive(tmp_path):
    _self_graph(tmp_path)
    text = ("caller calls helper, and caller calls orphan; "
            "orphan is unused; nonexistent calls whatever.")
    verds = {c["claim"]: c["verdict"]
             for c in codegraph.scan_claims("self", text)}
    assert verds.get("caller calls helper") == "CONFIRMED"
    assert verds.get("caller calls orphan") == "REFUTED"
    assert verds.get("orphan is unused") == "CONFIRMED"
    # a claim about unknown symbols is UNKNOWN -> omitted, never a false verdict
    assert "nonexistent calls whatever" not in verds


def test_scan_claims_is_noop_when_disabled_or_empty(tmp_path):
    _self_graph(tmp_path)
    codegraph.set_enabled(False)
    assert codegraph.scan_claims("self", "caller calls helper") == []
    codegraph.set_enabled(True)
    assert codegraph.scan_claims("empty-project", "caller calls helper") == []


def test_scan_claims_respects_limit(tmp_path):
    _self_graph(tmp_path)
    text = " ".join(["caller calls helper."] * 20)
    # deduped to one distinct claim regardless of repetition
    assert len(codegraph.scan_claims("self", text, limit=3)) == 1


# --- impact_report --------------------------------------------------------

def test_impact_report_lists_dependents(tmp_path):
    _self_graph(tmp_path)
    note = codegraph.impact_report("self", "refactor helper please")
    assert "helper" in note and "caller" in note and "depend" in note.lower()


def test_impact_report_empty_when_nothing_named(tmp_path):
    _self_graph(tmp_path)
    assert codegraph.impact_report("self", "just some prose with no symbols") == ""
    assert codegraph.impact_report("empty", "helper") == ""


# --- propose_upgrade stamp ------------------------------------------------

def test_propose_upgrade_stamps_graph_impact(tmp_path, monkeypatch):
    _self_graph(tmp_path)
    from olympus import tools, github, config as cfg
    monkeypatch.setattr(github, "create_issue", lambda *a, **k: None)
    monkeypatch.setattr(cfg, "egress_guard_enabled", lambda: False)
    saved = {}
    from olympus import memory
    monkeypatch.setattr(memory, "save",
                        lambda cat, title, details: saved.update(
                            {"details": details}) or "path/x")
    tools.HANDLERS["propose_upgrade"]("refactor helper",
                                      "we want to change helper's signature")
    assert "Graph impact" in saved["details"]
    assert "caller" in saved["details"]        # the recorded blast radius


def test_propose_upgrade_no_stamp_when_graph_disabled(tmp_path, monkeypatch):
    _self_graph(tmp_path)
    codegraph.set_enabled(False)
    from olympus import tools, github, config as cfg, memory
    monkeypatch.setattr(github, "create_issue", lambda *a, **k: None)
    monkeypatch.setattr(cfg, "egress_guard_enabled", lambda: False)
    saved = {}
    monkeypatch.setattr(memory, "save",
                        lambda cat, title, details: saved.update(
                            {"details": details}) or "path/x")
    tools.HANDLERS["propose_upgrade"]("x", "change helper")
    assert "Graph impact" not in saved["details"]


# --- gate CLI action ------------------------------------------------------

def test_gate_action_registered_and_dispatches(monkeypatch):
    from olympus import codegraph_cli, codegraph_gate
    assert "gate" in codegraph_cli.ACTIONS
    called = {}

    def fake_gate(root="."):
        called["root"] = root
        return {"passed": True}

    monkeypatch.setattr(codegraph_gate, "run_gate", fake_gate)

    class Args:
        action = "gate"
        project = "self"
        root = "."
        arg = []
    assert codegraph_cli.run(Args()) == 0
    assert called == {"root": "."}
