"""Semantic layer for skills — embedding-based dedup at write time and
cosine retrieval. Runs without a real embeddings endpoint by monkeypatching the
embed seam with a deterministic bag-of-words vectorizer, so cosine reflects text
overlap. Mirrors the best-effort contract: no endpoint → no-op, lexical only."""

import pytest

from olympus import embed, skills


_VOCAB = ["python", "code", "execute", "run", "email", "send", "inbox",
          "chart", "plot", "data", "visualize"]


def _bow(text: str):
    t = text.lower()
    v = [float(t.count(w)) for w in _VOCAB]
    return v if any(v) else [1.0] + [0.0] * (len(_VOCAB) - 1)


@pytest.fixture()
def semantic(monkeypatch):
    """Turn the embedding seam on with a deterministic vectorizer."""
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed_one", lambda text: _bow(text))
    # cosine stays real
    yield


def test_search_ranks_by_semantic_similarity(semantic):
    skills.create("Python runner", "Execute python code",
                  "Run python code snippets and print results.")
    skills.create("Email sender", "Send an email",
                  "Compose and send email to an inbox.")
    skills.create("Chart maker", "Visualize data",
                  "Plot data into a chart to visualize it.")

    hits = skills.search("run python code", limit=3)
    assert hits, "expected semantic hits"
    assert hits[0]["name"] == "Python runner"          # best match ranks first
    names = [h["name"] for h in hits]
    # unrelated skills (zero overlap) are correctly excluded, never outranking
    assert "Email sender" not in names
    # a query that overlaps the chart skill surfaces it above the email skill
    chart_hits = [h["name"] for h in skills.search("visualize data chart", 3)]
    assert chart_hits and chart_hits[0] == "Chart maker"
    assert "Email sender" not in chart_hits


def test_search_is_empty_without_an_endpoint(monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)
    skills.create("Python runner", "Execute python code", "Run python code.")
    assert skills.search("python") == []          # best-effort → lexical fallback


def test_create_flags_a_near_duplicate(semantic):
    skills.create("Run Python", "Execute python code",
                  "Run python code snippets and print the results.")
    msg = skills.create("Python executor", "Execute python code",
                        "Run python code snippets and print results.")
    assert "Near-duplicate" in msg


def test_create_does_not_flag_a_distinct_skill(semantic):
    skills.create("Run Python", "Execute python code", "Run python code.")
    msg = skills.create("Email sender", "Send email", "Send email to an inbox.")
    assert "Near-duplicate" not in msg


def test_near_duplicates_noop_without_endpoint(monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)
    skills.create("Run Python", "Execute python code", "Run python code.")
    assert skills.near_duplicates("Python exec", "Execute python code",
                                  "Run python code.") == []


def test_embedding_cache_avoids_recompute(semantic, monkeypatch):
    skills.create("Python runner", "Execute python code", "Run python code.")
    calls = {"n": 0}
    real = embed.embed_one

    def counting(text):
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(embed, "embed_one", counting)
    skills.search("python")            # embeds the query + each skill (cache miss? no — cached at create)
    first = calls["n"]
    skills.search("python")            # second search: skill vecs served from cache
    # the only new embed calls on the 2nd search are for the query itself (1),
    # not re-embedding the unchanged skill.
    assert calls["n"] - first <= 1


def test_embedding_cache_invalidates_on_model_change(monkeypatch):
    """Changing the embed model must invalidate cached vectors (they live in a
    different space) — otherwise search silently returns nothing until every
    skill is re-saved. The cache re-embeds under the new model."""
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "_model", lambda: "model-a")
    monkeypatch.setattr(embed, "embed_one", lambda t: [1.0, 0.0, 0.0])
    skills.create("Runner", "run", "run code")
    v1 = skills._skill_embedding("runner", "run code")
    assert v1 == [1.0, 0.0, 0.0]

    # switch to a different model (different-dimension vectors)
    monkeypatch.setattr(embed, "_model", lambda: "model-b")
    monkeypatch.setattr(embed, "embed_one", lambda t: [0.0, 1.0, 0.0, 0.0])
    v2 = skills._skill_embedding("runner", "run code")
    assert v2 == [0.0, 1.0, 0.0, 0.0], "stale model vec served after model change"
    # and the cache now records the new model
    assert skills._load_emb_cache()["runner"]["model"] == "model-b"
