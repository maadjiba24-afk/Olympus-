"""Tests for the exporters: every format renders, parses, and stays
self-contained; database pushes fail with a clear message sans drivers."""

import json
import xml.etree.ElementTree as ET

import pytest

from olympus import codegraph, codegraph_export, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    store.reset()
    a = codegraph.add_node("p", "m.py", "m.alpha", codegraph.FUNCTION)
    b = codegraph.add_node("p", "m.py", "m.beta", codegraph.FUNCTION)
    codegraph.add_node("p", "docs/design.md", "design", codegraph.DOCUMENT)
    codegraph.add_edge("p", a["id"], "calls", b["id"])
    yield
    store.reset()


def test_node_link_shape():
    d = codegraph_export.to_node_link("p")
    assert d["directed"] and len(d["nodes"]) == 3 and len(d["edges"]) == 1
    assert all("community" in n for n in d["nodes"])


def test_graphml_parses():
    xml = codegraph_export.to_graphml("p")
    root = ET.fromstring(xml)
    assert root.tag.endswith("graphml")


def test_graphml_escapes_hostile_labels():
    codegraph.add_node("p", "m.py", 'm.<script>"x"', codegraph.FUNCTION)
    ET.fromstring(codegraph_export.to_graphml("p"))   # must not raise


def test_mermaid_has_subgraphs_and_edges():
    mmd = codegraph_export.to_mermaid("p")
    assert mmd.startswith("flowchart") and "subgraph" in mmd
    assert "-->" in mmd or "-.->" in mmd


def test_html_is_self_contained():
    html = codegraph_export.to_html("p")
    assert "<script" in html
    assert "src=" not in html and "href=" not in html and "@import" not in html
    assert "alpha" in html


def test_html_payload_cannot_close_its_script_tag():
    codegraph.add_node("p", "m.py", "m.x</script><script>alert(1)",
                       codegraph.FUNCTION)
    html = codegraph_export.to_html("p")
    body = html.split('type="application/json">', 1)[1]
    payload = body.split("</script>", 1)[0]
    assert "alert(1)" not in payload or "<\\/" in payload


def test_cypher_escapes_quotes():
    codegraph.add_node("p", "m.py", "m.o'brien", codegraph.FUNCTION)
    cy = codegraph_export.to_cypher("p")
    assert "\\'" in cy and "MERGE" in cy


def test_obsidian_vault_written_under_own_dir(tmp_path):
    r = codegraph_export.to_obsidian("p", tmp_path / "vault")
    assert r["notes"] >= 3
    files = list((tmp_path / "vault").rglob("*.md"))
    assert any(f.name == "index.md" for f in files)
    # everything stays under the codegraph-<project> subdirectory
    assert all("codegraph-p" in str(f) for f in files)


def test_wiki_pages_crosslink(tmp_path):
    r = codegraph_export.to_wiki("p", tmp_path / "wiki")
    index = (tmp_path / "wiki" / "index.md").read_text()
    assert "Subsystems" in index
    assert r["pages"] >= 2


def test_export_dispatch_and_unknown_format(tmp_path):
    written = codegraph_export.export("p", tmp_path / "out",
                                      ["json", "mermaid", "report"])
    assert set(written) == {"json", "mermaid", "report"}
    data = json.loads((tmp_path / "out" / "graph.json").read_text())
    assert data["nodes"]
    with pytest.raises(ValueError):
        codegraph_export.export("p", tmp_path / "out", ["nope"])


def test_db_pushes_need_their_drivers():
    with pytest.raises(RuntimeError, match="neo4j"):
        codegraph_export.push_neo4j("p", "bolt://localhost:7687")
    with pytest.raises(RuntimeError, match="falkordb"):
        codegraph_export.push_falkordb("p")
