"""Tests for the code graph wired into the self-evolution loop: claim scanning,
impact reports, the propose_upgrade stamp, Aletheia's ground-truth block, and
the gate CLI action."""

import pytest

from olympus import codegraph, codegraph_build, codegraph_langs, config, store


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


# --- iter-3 hardening: review findings, each with a regression -------------

def test_scan_claims_ignores_english_uses_prose(tmp_path):
    """HIGH review finding: 'The parser uses config' must NOT become a false
    REFUTED verdict fed to Aletheia as ground truth."""
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("def parser():\n    pass\n\ndef config():\n    pass\n")
    codegraph_build.build("self", root)
    assert codegraph.scan_claims(
        "self", "The parser uses config values to decide formatting") == []


def test_scan_claims_requires_distinctive_operands(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("def get():\n    pass\n\ndef set():\n    pass\n")
    codegraph_build.build("self", root)
    # short common-word symbols: a 'get calls set' English phrase is not a claim
    assert codegraph.scan_claims("self", "get calls set") == []


def test_scan_claims_still_verifies_distinctive_claims(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text(
        "def resolve_handler():\n    dispatch_event()\n\n"
        "def dispatch_event():\n    pass\n")
    codegraph_build.build("self", root)
    verds = {c["claim"]: c["verdict"]
             for c in codegraph.scan_claims(
                 "self", "resolve_handler calls dispatch_event")}
    assert verds.get("resolve_handler calls dispatch_event") == "CONFIRMED"


def test_scan_claims_is_bounded_on_hostile_bundle(tmp_path):
    import time as _t
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text(
        "\n".join(f"def sym{i}():\n    pass" for i in range(200)))
    codegraph_build.build("self", root)
    hostile = " ".join(f"zzAlpha{i} calls zzBeta{i}" for i in range(4000))
    start = _t.perf_counter()
    codegraph.scan_claims("self", hostile)
    assert (_t.perf_counter() - start) < 2.0    # bounded by _SCAN_MATCH_CAP


@pytest.mark.parametrize("kw", ["with", "using", "lock", "foreach"])
def test_control_keywords_not_extracted_as_methods(tmp_path, kw):
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.ts").write_text(f"{kw} (obj) {{\n  doThing();\n}}\n"
                               "class K {\n  real() {}\n}\n")
    codegraph_langs.extract_file("p", root / "a.ts", root)
    labels = {n["label"] for n in codegraph.nodes("p")}
    assert kw not in labels and "real" in labels


def test_method_shorthand_is_linear_without_the_length_cap():
    """F2: the pattern itself must be near-linear, not merely saved by the
    per-line cap — a 40k-char space-padded near-miss must not take seconds."""
    import time as _t
    spec = next(x for x in codegraph_langs.LANGS if x.name == "typescript")
    for line in (" " * 40000 + "x(", "\t" * 40000 + "async foo("):
        start = _t.perf_counter()
        spec.fn.search(line)
        assert (_t.perf_counter() - start) < 1.0
