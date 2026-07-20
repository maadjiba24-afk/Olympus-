"""Tests for near-duplicate merging: Jaro-Winkler, MinHash blocking,
union-find groups, and the merge rewrite."""

import pytest

from olympus import codegraph, codegraph_dedup, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "mem")
    store.reset()
    yield
    store.reset()


def test_jaro_winkler_basics():
    jw = codegraph_dedup.jaro_winkler
    assert jw("same", "same") == 1.0
    assert jw("", "x") == 0.0
    assert jw("martha", "marhta") > 0.9
    assert jw("apple", "orange") < 0.6


def test_minhash_similar_labels_share_bands():
    sig_a = codegraph_dedup._minhash(codegraph_dedup._shingles(
        "the threat model of the sandbox layer"))
    sig_b = codegraph_dedup._minhash(codegraph_dedup._shingles(
        "the threat model of the sandbox layers"))
    shared = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    assert shared > len(sig_a) // 2


def test_only_prose_kinds_participate():
    codegraph.add_node("p", "a.py", "a.handler", codegraph.FUNCTION)
    codegraph.add_node("p", "b.py", "b.handler", codegraph.FUNCTION)
    assert codegraph_dedup.find_duplicates("p") == []


def test_docs_with_near_identical_titles_group_and_merge():
    d1 = codegraph.add_node("p", "d1.md", "release process notes",
                            codegraph.DOCUMENT)
    codegraph.add_node("p", "d2.md", "release process note",
                       codegraph.DOCUMENT)
    codegraph.add_node("p", "d3.md", "incident runbook", codegraph.DOCUMENT)
    f = codegraph.add_node("p", "m.py", "m.fn", codegraph.FUNCTION)
    codegraph.add_edge("p", f["id"], "references", d1["id"])

    groups = codegraph_dedup.find_duplicates("p")
    assert len(groups) == 1 and len(groups[0]) == 2

    r = codegraph_dedup.merge_duplicates("p")
    assert r["merged"] == 1
    labels = [n["label"] for n in codegraph.nodes("p")]
    assert labels.count("incident runbook") == 1
    assert sum("release process" in l for l in labels) == 1
    # the edge now points at the surviving doc
    survivor = next(n for n in codegraph.nodes("p")
                    if "release process" in n["label"])
    assert any(e["dst"] == survivor["id"] for e in codegraph.edges("p"))


def test_different_kinds_never_merge():
    codegraph.add_node("p", "d.md", "sandbox design", codegraph.DOCUMENT)
    owner = codegraph.add_node("p", "m.py", "m.fn", codegraph.FUNCTION)
    codegraph.add_rationale("p", "m.py", owner["id"], "sandbox design")
    assert codegraph_dedup.find_duplicates("p") == []


def test_merge_is_idempotent():
    codegraph.add_node("p", "d1.md", "threat model", codegraph.DOCUMENT)
    codegraph.add_node("p", "d2.md", "threat  model", codegraph.DOCUMENT)
    codegraph_dedup.merge_duplicates("p")
    assert codegraph_dedup.merge_duplicates("p") == {"groups": 0, "merged": 0}
