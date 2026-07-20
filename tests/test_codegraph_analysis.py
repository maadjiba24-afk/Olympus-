"""Tests for graph analysis: Louvain communities, god nodes, surprises,
questions, the report, and the token benchmark."""

import pytest

from olympus import codegraph, codegraph_analysis, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    store.reset()
    yield
    store.reset()


def _two_cliques(project="p"):
    """Two tight 4-cliques joined by a single bridge edge."""
    ids = {}
    for side, names in (("a", "abcd"), ("b", "wxyz")):
        for ch in names:
            n = codegraph.add_node(project, f"{side}/{side}.py",
                                   f"{side}.fn_{ch}", codegraph.FUNCTION)
            ids[f"{side}{ch}"] = n["id"]
        for i, ci in enumerate(names):
            for cj in names[i + 1:]:
                codegraph.add_edge(project, ids[f"{side}{ci}"], "calls",
                                   ids[f"{side}{cj}"])
    codegraph.add_edge(project, ids["aa"], "calls", ids["bw"])
    return ids


def test_louvain_separates_two_cliques():
    _two_cliques()
    comm = codegraph_analysis.compute_communities("p")
    assign = comm["assign"]
    a_comms = {assign[i] for lbl, i in _ids_by_prefix("p", "fn_").items()
               if _path_of("p", i).startswith("a/")}
    b_comms = {assign[i] for lbl, i in _ids_by_prefix("p", "fn_").items()
               if _path_of("p", i).startswith("b/")}
    assert len(a_comms) == 1 and len(b_comms) == 1
    assert a_comms != b_comms


def _ids_by_prefix(project, prefix):
    return {n["label"]: n["id"] for n in codegraph.nodes(project)
            if n["label"].startswith(prefix)}


def _path_of(project, nid):
    return next(n["path"] for n in codegraph.nodes(project)
                if n["id"] == nid)


def test_louvain_is_deterministic():
    _two_cliques()
    a = codegraph_analysis.compute_communities("p")["assign"]
    b = codegraph_analysis.compute_communities("p")["assign"]
    assert a == b


def test_communities_cached_until_recompute():
    _two_cliques()
    first = codegraph_analysis.communities("p")
    codegraph_analysis.set_label("p", 0, "the left side")
    again = codegraph_analysis.communities("p")
    assert again["labels"]["0"] == "the left side"


def test_god_nodes_ranked_by_degree_and_skip_rationale():
    ids = _two_cliques()
    hub = codegraph.add_node("p", "a/a.py", "a.hub", codegraph.FUNCTION)
    for k in ("aa", "ab", "ac", "ad", "bw", "bx"):
        codegraph.add_edge("p", ids[k], "calls", hub["id"])
    codegraph.add_rationale("p", "a/a.py", hub["id"],
                            "the hub everything uses")
    gods = codegraph_analysis.god_nodes("p")
    assert gods[0]["label"] == "hub"
    assert all(g["kind"] != codegraph.RATIONALE for g in gods)


def test_surprises_finds_the_bridge():
    _two_cliques()
    codegraph_analysis.compute_communities("p")
    surp = codegraph_analysis.surprises("p")
    assert surp, "the single cross-clique edge should surface"
    assert any("communities" in s["why"] for s in surp)


def test_report_renders_all_sections():
    _two_cliques()
    rep = codegraph_analysis.render_report("p")
    for heading in ("God nodes", "Communities", "confidence"):
        assert heading in rep


def test_report_on_empty_graph_is_helpful():
    assert "codegraph build" in codegraph_analysis.render_report("empty")


def test_token_benchmark_measures_reduction(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "big.py").write_text(
        "def target():\n    pass\n" + ("# padding\n" * 3000))
    from olympus import codegraph_build
    codegraph_build.build("p2", root)
    b = codegraph_analysis.token_benchmark("p2", "target")
    assert b["answerable"] and b["corpus_tokens"] > b["subgraph_tokens"]
    assert 0 < b["reduction"] <= 1
