"""Phase 3 — personal-document RAG. Retrieval over the user's own documents,
lexical by default and semantic when an embeddings endpoint is configured.
Content is first-party (trusted), grounded into the synthesis prompt.
"""

import pytest

from olympus import config, documents, docrag, embed


@pytest.fixture()
def user(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(config, "MEMORY_ENABLED", True)
    # default: no embeddings endpoint -> lexical path
    monkeypatch.setattr(embed, "available", lambda: False)
    return "raguser"


def _seed(u):
    documents.save(u, "Trip Plan",
                   "# Trip Plan\n\nWe fly to Lisbon on Tuesday. Hotel is the "
                   "Baixa. Budget is 2000 euros for the week.")
    documents.save(u, "Recipes",
                   "# Recipes\n\nCarbonara: guanciale, eggs, pecorino, black "
                   "pepper. No cream, ever.")


# --- chunking --------------------------------------------------------------

def test_chunk_short_text_single():
    assert docrag.chunk("hello world") == ["hello world"]


def test_chunk_long_text_overlaps():
    text = ("para one.\n\n" * 200)
    chunks = docrag.chunk(text, size=400, overlap=80)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


# --- lexical retrieval -----------------------------------------------------

def test_retrieve_finds_relevant_document(user):
    _seed(user)
    hits = docrag.retrieve(user, "what is my hotel in lisbon", k=3)
    assert hits and "Trip Plan" == hits[0]["document"]
    assert "Baixa" in hits[0]["text"]


def test_retrieve_empty_when_no_documents(user):
    assert docrag.retrieve(user, "anything") == []


def test_context_block_grounds_relevant_doc(user):
    _seed(user)
    block = docrag.context_block(user, "carbonara ingredients")
    assert "From your documents" in block and "Recipes" in block
    assert "guanciale" in block


def test_context_block_empty_for_irrelevant_query(user):
    _seed(user)
    assert docrag.context_block(user, "quantum chromodynamics lecture") == ""


def test_context_block_respects_char_budget(user):
    documents.save(user, "Big", "lisbon " * 5000)
    block = docrag.context_block(user, "lisbon", char_budget=500)
    assert 0 < len(block) < 1500


# --- index caching + mtime invalidation ------------------------------------

def test_index_reembeds_only_changed_docs(user, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(embed, "available", lambda: True)

    def fake_embed(texts):
        calls["n"] += 1
        return [[float(len(t)), 1.0] for t in texts]

    monkeypatch.setattr(embed, "embed", fake_embed)
    documents.save(user, "A", "alpha content here")
    docrag.build_index(user)
    first = calls["n"]
    docrag.build_index(user)                    # unchanged -> no re-embed
    assert calls["n"] == first
    documents.save(user, "A", "alpha content changed")
    docrag.build_index(user)                    # changed -> re-embed
    assert calls["n"] > first


# --- semantic path ---------------------------------------------------------

def test_semantic_ranks_by_cosine(user, monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: True)
    # doc chunk vector and query vector: make "Recipes" the semantic match
    vecs = {"carbonara guanciale": [1.0, 0.0], "trip lisbon hotel": [0.0, 1.0]}

    def fake_embed(texts):
        out = []
        for t in texts:
            out.append([1.0, 0.0] if "carbonara" in t.lower() else [0.0, 1.0])
        return out

    monkeypatch.setattr(embed, "embed", fake_embed)
    monkeypatch.setattr(embed, "embed_one",
                        lambda t: [1.0, 0.0])   # query ~ carbonara
    _seed(user)
    hits = docrag.retrieve(user, "pasta", k=2)
    assert hits[0]["document"] == "Recipes"


def test_falls_back_to_lexical_when_embed_fails(user, monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed", lambda texts: None)   # failure
    monkeypatch.setattr(embed, "embed_one", lambda t: None)
    _seed(user)
    hits = docrag.retrieve(user, "lisbon hotel budget")
    assert hits and hits[0]["document"] == "Trip Plan"


# --- tool + pipeline wiring ------------------------------------------------

def test_search_documents_tool(user, monkeypatch):
    from olympus import tools, memory
    monkeypatch.setattr(memory, "current_user", lambda: user)
    _seed(user)
    out = tools.HANDLERS["search_documents"]("lisbon hotel")
    assert "Trip Plan" in out
    assert "No relevant" in tools.HANDLERS["search_documents"]("xyzzy plugh")


def test_tool_registered_and_on_peitho():
    from olympus import tools, specialists
    assert "search_documents" in tools.EXTRA_TOOLS
    assert "search_documents" in tools.HANDLERS
    assert "search_documents" in specialists.SPECIALISTS["peitho"].extra_tools


def test_synthesis_injects_document_grounding(user, monkeypatch):
    """The synthesis prompt must carry relevant document context."""
    from olympus import orchestrator
    _seed(user)
    bot = orchestrator.Olympus(user=user)
    captured = {}
    monkeypatch.setattr(orchestrator.backend, "complete_text",
                        lambda pool, system, msgs, effort="high":
                        captured.setdefault("system", system) or "answer")
    bot._synthesize("where is my hotel in lisbon?", "brief", "verified")
    assert "From your documents" in captured["system"]
    assert "Baixa" in captured["system"]
