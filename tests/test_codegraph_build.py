"""Tests for the multi-language build pipeline: extraction across languages,
confidence tiers, ignore files, documents, and incremental update."""

import pytest

from olympus import codegraph, codegraph_build, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    store.reset()
    yield
    store.reset()


def _tree(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text(
        "def entry():\n    helper()\n\ndef helper():\n    pass\n")
    (root / "svc.go").write_text(
        "package main\n// NOTE: go-side hub\nfunc Serve() int { return 1 }\n"
        "func Boot() int { return Serve() }\n")
    (root / "README.md").write_text(
        "# Demo\nSee [[svc]] and [the app](./app.py).\n")
    return root


def _labels(project):
    return {n["label"] for n in codegraph.nodes(project)}


def test_build_extracts_python_go_and_docs(tmp_path):
    r = codegraph_build.build("p", _tree(tmp_path))
    assert r["files"] == 3
    labels = _labels("p")
    assert {"entry", "helper", "Serve", "Boot", "README"} <= labels


def test_python_call_edges_are_extracted_tier(tmp_path):
    codegraph_build.build("p", _tree(tmp_path))
    edges = codegraph.edges("p")
    by = {(e["src"], e["rel"], e["dst"]): e for e in edges}
    ids = {n["label"]: n["id"] for n in codegraph.nodes("p")}
    e = by.get((ids["entry"], "calls", ids["helper"]))
    assert e and e["tier"] == codegraph.EXTRACTED


def test_regex_language_call_edges_are_inferred_not_extracted(tmp_path):
    codegraph_build.build("p", _tree(tmp_path))
    ids = {n["label"]: n["id"] for n in codegraph.nodes("p")}
    call = [e for e in codegraph.edges("p")
            if e["src"] == ids["Boot"] and e["dst"] == ids["Serve"]]
    assert call and call[0]["tier"] == codegraph.INFERRED


def test_doc_links_become_references_edges(tmp_path):
    codegraph_build.build("p", _tree(tmp_path))
    ids = {n["label"]: n["id"] for n in codegraph.nodes("p")}
    rels = {(e["src"], e["rel"], e["dst"]) for e in codegraph.edges("p")}
    assert (ids["README"], "references", ids["svc"]) in rels
    assert (ids["README"], "references", ids["app"]) in rels


def test_code_import_never_resolves_to_a_same_stem_doc(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "config.py").write_text("X = 1\n")
    (root / "CONFIG.md").write_text("# Config doc\n")
    (root / "user.py").write_text("import config\n")
    codegraph_build.build("p", root)
    nodes = {n["id"]: n for n in codegraph.nodes("p")}
    for e in codegraph.edges("p"):
        if e["rel"] == "imports":
            assert nodes[e["dst"]]["path"].endswith(".py")


def test_shadowed_names_do_not_resolve_across_files(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def append():\n    pass\n")
    (root / "b.py").write_text("def use():\n    x = []\n    x.append(1)\n")
    codegraph_build.build("p", root)
    ids = {n["label"]: n["id"] for n in codegraph.nodes("p")}
    bad = [e for e in codegraph.edges("p")
           if e["dst"] == ids["append"] and e["src"] == ids["use"]]
    assert not bad


def test_gitignore_and_olympusignore_are_honored(tmp_path):
    root = tmp_path / "proj"
    (root / "generated").mkdir(parents=True)
    (root / "kept").mkdir()
    (root / ".gitignore").write_text("generated/\n")
    (root / ".olympusignore").write_text("secret.py\n")
    (root / "generated" / "gen.py").write_text("def gen():\n    pass\n")
    (root / "kept" / "ok.py").write_text("def ok():\n    pass\n")
    (root / "secret.py").write_text("def secret():\n    pass\n")
    codegraph_build.build("p", root)
    labels = _labels("p")
    assert "ok" in labels
    assert "gen" not in labels and "secret" not in labels


def test_ignore_negation_reincludes(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".gitignore").write_text("*.py\n!keep.py\n")
    (root / "keep.py").write_text("def kept():\n    pass\n")
    (root / "drop.py").write_text("def dropped():\n    pass\n")
    codegraph_build.build("p", root)
    labels = _labels("p")
    assert "kept" in labels and "dropped" not in labels


def test_incremental_update_only_touches_changed_files(tmp_path):
    root = _tree(tmp_path)
    codegraph_build.build("p", root)
    before = {n["id"] for n in codegraph.nodes("p")}
    (root / "app.py").write_text(
        "def entry():\n    helper()\n\ndef helper():\n    pass\n\n"
        "def extra():\n    entry()\n")
    r = codegraph_build.update("p", root)
    assert r["mode"] == "incremental"
    assert r["files_changed"] == 1 and r["files_deleted"] == 0
    labels = _labels("p")
    assert "extra" in labels
    # unchanged files kept their node identity
    ids_after = {n["id"] for n in codegraph.nodes("p")}
    unchanged = {n["id"] for n in codegraph.nodes("p")
                 if n["path"] in ("svc.go", "README.md")}
    assert unchanged <= before and unchanged <= ids_after


def test_incremental_update_drops_deleted_files(tmp_path):
    root = _tree(tmp_path)
    codegraph_build.build("p", root)
    (root / "svc.go").unlink()
    r = codegraph_build.update("p", root)
    assert r["files_deleted"] == 1
    assert "Serve" not in _labels("p")


def test_update_without_manifest_falls_back_to_full_build(tmp_path):
    root = _tree(tmp_path)
    r = codegraph_build.update("p", root)
    assert r["mode"] == "full" and codegraph.stats("p")["nodes"] > 0


def test_update_restores_cross_file_edges_into_changed_files(tmp_path):
    root = _tree(tmp_path)
    codegraph_build.build("p", root)
    (root / "app.py").write_text(
        "def entry():\n    helper()\n\ndef helper():\n    pass\n")
    codegraph_build.update("p", root)
    ids = {n["label"]: n["id"] for n in codegraph.nodes("p")}
    rels = {(e["src"], e["rel"], e["dst"]) for e in codegraph.edges("p")}
    # the doc edge into app.py survives the file's re-extraction
    assert (ids["README"], "references", ids["app"]) in rels


# --- oracle soundness across languages ------------------------------------

def test_oracle_never_refutes_on_regex_only_evidence(tmp_path):
    codegraph_build.build("p", _tree(tmp_path))
    # Boot -> Serve exists only as INFERRED (go, regex): must not refute,
    # must not confirm.
    v = codegraph.verify_claim("p", "Boot calls Serve")
    assert v["verdict"] == "UNKNOWN"
    # unused-claims about regex-extracted symbols are also unknowable
    v2 = codegraph.verify_claim("p", "Serve is unused")
    assert v2["verdict"] in ("UNKNOWN", "REFUTED")   # has an incoming INFERRED
    (tmp_path / "proj" / "lone.go").write_text(
        "package main\nfunc Lonely() int { return 1 }\n")
    codegraph_build.update("p", tmp_path / "proj")
    v3 = codegraph.verify_claim("p", "Lonely is unused")
    assert v3["verdict"] == "UNKNOWN"


def test_oracle_still_confirms_and_refutes_python(tmp_path):
    codegraph_build.build("p", _tree(tmp_path))
    assert codegraph.verify_claim("p", "entry calls helper")["verdict"] == \
        "CONFIRMED"
    assert codegraph.verify_claim("p", "helper calls entry")["verdict"] == \
        "REFUTED"


# --- #15: qualified-call precision ---------------------------------------

def _node(project, label, path_suffix):
    for n in codegraph.nodes(project):
        if n["label"] == label and str(n["path"]).endswith(path_suffix):
            return n
    return None


def test_qualified_call_pins_to_the_named_module(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "memory.py").write_text("def save():\n    pass\n")
    (root / "store.py").write_text("def save():\n    pass\n")
    (root / "caller.py").write_text(
        "import memory\nimport store\n\ndef run():\n    memory.save()\n")
    codegraph_build.build("p", root)

    run = _node("p", "run", "caller.py")
    mem_save = _node("p", "save", "memory.py")
    store_save = _node("p", "save", "store.py")
    edges = codegraph.edges("p")
    to_mem = [e for e in edges if e["src"] == run["id"]
              and e["dst"] == mem_save["id"] and e["rel"] == "calls"]
    to_store = [e for e in edges if e["src"] == run["id"]
                and e["dst"] == store_save["id"] and e["rel"] == "calls"]
    # pinned to the module the qualifier names, as ground truth
    assert to_mem and to_mem[0]["tier"] == codegraph.EXTRACTED
    assert not to_store                       # the other save() is not linked


def test_qualified_call_resolves_a_name_defined_in_many_files(tmp_path):
    # `save` defined in 5 modules (> _MAX_AMBIGUOUS) → previously skipped
    # entirely (no edge). With the qualifier it now resolves precisely.
    root = tmp_path / "proj"
    root.mkdir()
    for m in ("memory", "store", "cache", "disk", "vault"):
        (root / f"{m}.py").write_text("def save():\n    pass\n")
    (root / "caller.py").write_text(
        "import vault\n\ndef run():\n    vault.save()\n")
    codegraph_build.build("p", root)

    run = _node("p", "run", "caller.py")
    vault_save = _node("p", "save", "vault.py")
    to_vault = [e for e in codegraph.edges("p") if e["src"] == run["id"]
                and e["dst"] == vault_save["id"] and e["rel"] == "calls"]
    assert to_vault and to_vault[0]["tier"] == codegraph.EXTRACTED


def test_oracle_confirms_a_qualified_call(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "memory.py").write_text("def save():\n    pass\n")
    (root / "store.py").write_text("def save():\n    pass\n")
    (root / "caller.py").write_text(
        "import memory\n\ndef run():\n    memory.save()\n")
    codegraph_build.build("p", root)
    # Was UNKNOWN before (ambiguous save); now CONFIRMED via the precise edge.
    assert codegraph.verify_claim("p", "run calls save")["verdict"] == "CONFIRMED"


def test_from_import_alias_narrows(tmp_path):
    # `from . import memory` style (package-relative) also feeds the alias map.
    root = tmp_path / "proj"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "memory.py").write_text("def save():\n    pass\n")
    (pkg / "store.py").write_text("def save():\n    pass\n")
    (pkg / "caller.py").write_text(
        "from pkg import memory\n\ndef run():\n    memory.save()\n")
    codegraph_build.build("p", root)
    run = _node("p", "run", "caller.py")
    mem_save = _node("p", "save", "memory.py")
    to_mem = [e for e in codegraph.edges("p") if e["src"] == run["id"]
              and e["dst"] == mem_save["id"] and e["rel"] == "calls"]
    assert to_mem and to_mem[0]["tier"] == codegraph.EXTRACTED
