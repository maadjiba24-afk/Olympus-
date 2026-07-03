"""Tests for the relationship graph — entities, edges, traversal, injection."""

import pytest

from olympus import relgraph, config, store


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    store.reset()
    yield
    store.reset()


def test_add_node_dedupes_case_insensitively():
    a = relgraph.add_node("u", "Acme", relgraph.COMPANY)
    b = relgraph.add_node("u", "acme")
    assert a["id"] == b["id"] and len(relgraph.nodes("u")) == 1


def test_add_edge_creates_nodes_and_dedupes():
    relgraph.add_edge("u", "Sarah", "cofounder_of", "Acme",
                      src_kind="person", dst_kind="company")
    relgraph.add_edge("u", "Sarah", "cofounder_of", "Acme")  # dup
    assert len(relgraph.nodes("u")) == 2
    assert len(relgraph.edges("u")) == 1


def test_add_node_refuses_new_at_cap(monkeypatch):
    monkeypatch.setattr(relgraph, "_MAX_NODES", 2)
    relgraph.add_node("u", "A"); relgraph.add_node("u", "B")
    assert relgraph.add_node("u", "C") is None      # refused, not aliased to A
    assert len(relgraph.nodes("u")) == 2


def test_add_edge_at_cap_does_not_fabricate_false_edge(monkeypatch):
    # With the graph full, a new (src, dst) pair must NOT be aliased onto an
    # existing node and joined by a bogus edge.
    monkeypatch.setattr(relgraph, "_MAX_NODES", 2)
    relgraph.add_node("u", "A"); relgraph.add_node("u", "B")
    assert relgraph.add_edge("u", "New1", "knows", "New2") is None
    assert relgraph.edges("u") == []                 # no false relationship
    assert len(relgraph.nodes("u")) == 2


def test_relation_normalized():
    e = relgraph.add_edge("u", "A", "Works At", "B")
    assert e["rel"] == "works_at"


def test_find_entities_requires_all_label_words():
    relgraph.add_node("u", "Sarah Chen", relgraph.PERSON)
    assert relgraph.find_entities("u", "how is Sarah Chen doing")
    assert relgraph.find_entities("u", "how is Sarah doing") == []  # missing 'chen'
    assert relgraph.find_entities("u", "totally unrelated") == []


def test_neighbors_traverses_both_directions():
    # "who works at Acme" must reach Sarah via the INCOMING works_at edge
    relgraph.add_edge("u", "Sarah", "works_at", "Acme",
                      src_kind="person", dst_kind="company")
    acme = relgraph.find_entities("u", "Acme")[0]
    labels = [n["label"] for _, n in relgraph.neighbors("u", acme["id"])]
    assert "Sarah" in labels


def test_context_block_who_do_i_know_at():
    relgraph.add_edge("u", "Sarah", "works_at", "Acme",
                      src_kind="person", dst_kind="company")
    relgraph.add_edge("u", "Tom", "works_at", "Acme",
                      src_kind="person", dst_kind="company")
    block = relgraph.context_block("u", "who do I know at Acme?")
    assert "Sarah works at Acme" in block and "Tom works at Acme" in block


def test_context_block_empty_when_no_entity():
    relgraph.add_edge("u", "Sarah", "works_at", "Acme")
    assert relgraph.context_block("u", "what's the weather like today") == ""


def test_describe():
    relgraph.add_edge("u", "Sarah", "cofounder_of", "Acme",
                      src_kind="person", dst_kind="company")
    desc = relgraph.describe("u", "Sarah")
    assert "Sarah" in desc and "cofounder of Acme" in desc


def test_forget_removes_node_and_edges():
    relgraph.add_edge("u", "Sarah", "works_at", "Acme")
    assert relgraph.forget("u", "Acme")
    assert relgraph.context_block("u", "who is at Acme") == ""
    # the edge is gone too
    assert relgraph.edges("u") == []


def test_ingest_triples():
    n = relgraph.ingest("u", [
        {"subject": "Sarah", "relation": "cofounder_of", "object": "Acme",
         "subject_kind": "person", "object_kind": "company"},
        {"subject": "", "relation": "x", "object": "y"},          # skipped
        {"subject": "Acme", "relation": "competitor_of", "object": "Globex"},
    ])
    assert n == 2
    assert "competitor of Globex" in relgraph.describe("u", "Acme")


def test_per_user():
    relgraph.add_node("alice", "X")
    assert relgraph.nodes("bob") == []


# --- extractor populates the graph + pipeline injection ------------------

def test_extractor_ingests_relationships(monkeypatch):
    from olympus import recall, usermem
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    monkeypatch.setattr(recall.backend, "complete_json", lambda *a, **k: {
        "memories": [],
        "relationships": [{"subject": "Sarah", "relation": "cofounder_of",
                           "object": "Acme", "subject_kind": "person",
                           "object_kind": "company"}]})

    class _S:
        provider = "anthropic"; model = "x"; api_key = None; base_url = None

    summary = recall.extract(
        "gu", "Sarah and I cofounded the company Acme together last year.",
        "Nice.", _S())
    assert summary.get("relationships") == 1
    assert "cofounder of Acme" in relgraph.describe("gu", "Sarah")


def test_graph_injected_into_router(monkeypatch):
    from olympus import orchestrator, backend
    relgraph.add_edge("ru", "Sarah", "works_at", "Acme",
                      src_kind="person", dst_kind="company")
    captured = {}
    monkeypatch.setattr(backend, "complete_json",
                        lambda settings, system, messages, schema, effort="high":
                        captured.update(system=system) or
                        {"mode": "direct", "direct_reply": "ok", "specialists": [],
                         "brief": None, "needs_verification": False})
    bot = orchestrator.Olympus(user="ru")
    bot._route("who do I know at Acme?")
    assert "Sarah works at Acme" in captured["system"]
