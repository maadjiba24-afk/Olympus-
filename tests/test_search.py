"""Cross-session FTS5 search: indexing, querying, reindex, live-on-save."""

from olympus import memory, search


def test_index_and_search():
    history = [
        {"role": "user", "content": "what price for the premium tier?"},
        {"role": "assistant", "content": "Set the premium tier at $49/month."},
    ]
    n = search.index_conversation("conv1", history)
    assert n == 2
    hits = search.search("premium")
    assert any("premium" in h.content.lower() for h in hits)
    assert hits[0].conversation == "conv1"


def test_search_empty_query_returns_nothing():
    assert search.search("") == []


def test_skips_empty_turns():
    n = search.index_conversation("c", [{"role": "user", "content": "  "},
                                        {"role": "user", "content": "real"}])
    assert n == 1


def test_reindex_from_files():
    memory.save_conversation("alpha", [{"role": "user", "content": "alpha topic"}])
    memory.save_conversation("beta", [{"role": "user", "content": "beta topic"}])
    total = search.reindex()
    assert total == 2
    assert search.search("beta")[0].conversation == "beta"


def test_live_index_on_save_conversation():
    memory.save_conversation("live", [{"role": "user",
                                       "content": "remember the mango plan"}])
    hits = search.search("mango")
    assert hits and hits[0].conversation == "live"


def test_reindex_replaces_stale_turns():
    search.index_conversation("c", [{"role": "user", "content": "first version"}])
    search.index_conversation("c", [{"role": "user", "content": "second version"}])
    hits = search.search("version")
    assert len(hits) == 1 and "second" in hits[0].content


def test_conversation_filter():
    search.index_conversation("x", [{"role": "user", "content": "shared word here"}])
    search.index_conversation("y", [{"role": "user", "content": "shared word too"}])
    hits = search.search("shared", conversation="x")
    assert all(h.conversation == "x" for h in hits) and hits


def test_malformed_query_falls_back():
    search.index_conversation("c", [{"role": "user", "content": 'has "quotes" in it'}])
    # an unbalanced quote is invalid FTS syntax; must not raise
    hits = search.search('"quotes')
    assert isinstance(hits, list)
