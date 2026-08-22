"""Cross-session FTS5 search: indexing, querying, reindex, live-on-save."""

import json
import sqlite3

import pytest

from olympus import config, emem, memory, search, tools


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


def test_search_sessions_never_crosses_the_authenticated_user_boundary():
    memory.set_user("alice")
    memory.save_conversation(
        "alice-session",
        [{"role": "user", "content": "synthetic canary marigold-alice"}],
    )
    memory.set_user("bob")
    memory.save_conversation(
        "bob-session",
        [{"role": "user", "content": "synthetic canary marigold-bob"}],
    )

    bob = tools._search_sessions("marigold")
    assert "marigold-bob" in bob
    assert "marigold-alice" not in bob

    memory.set_user("alice")
    alice = tools._search_sessions("marigold")
    assert "marigold-alice" in alice
    assert "marigold-bob" not in alice


def test_malformed_fts_fallback_keeps_owner_and_conversation_filters():
    memory.set_user("alice")
    search.index_conversation(
        "wanted", [{"role": "user", "content": 'synthetic "needle'}])
    search.index_conversation(
        "other", [{"role": "user", "content": 'synthetic "needle'}])
    memory.set_user("bob")
    search.index_conversation(
        "wanted", [{"role": "user", "content": 'bob synthetic "needle'}])

    memory.set_user("alice")
    hits = search.search('"needle', conversation="wanted")
    assert [(hit.conversation, hit.content) for hit in hits] == [
        ("wanted", 'synthetic "needle')]


def test_non_fts_substring_fallback_is_owner_scoped(monkeypatch):
    monkeypatch.setattr(search, "_fts5_available", lambda _conn: False)
    memory.set_user("alice")
    search.index_conversation(
        "alice-session",
        [{"role": "user", "content": "harmless fallback juniper-alice"}],
    )
    memory.set_user("bob")
    search.index_conversation(
        "bob-session",
        [{"role": "user", "content": "harmless fallback juniper-bob"}],
    )

    hits = search.search("juniper")
    assert [hit.content for hit in hits] == [
        "harmless fallback juniper-bob"]


def test_emem_uses_its_explicit_user_not_ambient_context():
    memory.set_user("alice")
    search.index_conversation(
        "alice-session",
        [{"role": "user", "content": "harmless saffron-alice"}],
    )
    memory.set_user("bob")
    search.index_conversation(
        "bob-session",
        [{"role": "user", "content": "harmless saffron-bob"}],
    )

    memory.set_user("alice")  # deliberately wrong ambient principal
    fragments = emem.gather("bob", "saffron", limit=10)
    text = "\n".join(fragment.text for fragment in fragments)
    assert "saffron-bob" in text
    assert "saffron-alice" not in text


def test_old_ownerless_index_is_invalidated_instead_of_exposed():
    path = search._db_path()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE turns "
                 "(conversation TEXT, role TEXT, content TEXT, turn INT)")
    conn.execute("INSERT INTO turns VALUES (?,?,?,?)",
                 ("legacy", "user", "legacy private canary", 0))
    conn.commit()
    conn.close()

    memory.set_user("bob")
    assert search.search("legacy private canary") == []
    conn, _ = search._connect()
    try:
        columns = tuple(row[1] for row in conn.execute(
            "PRAGMA table_info(turns)").fetchall())
    finally:
        conn.close()
    assert columns == search._TURN_COLUMNS


def test_reindex_skips_ownerless_legacy_snapshots():
    conversations = config.MEMORY_DIR / "conversations"
    conversations.mkdir(parents=True)
    (conversations / "legacy.json").write_text(
        json.dumps([{"role": "user", "content": "legacy blue canary"}]),
        encoding="utf-8",
    )

    memory.set_user("bob")
    assert search.reindex() == 0
    assert search.search("legacy blue canary") == []


def test_reindex_preserves_durable_owners():
    memory.set_user("alice")
    memory.save_conversation(
        "alice-session", [{"role": "user", "content": "violet alice"}])
    memory.set_user("bob")
    memory.save_conversation(
        "bob-session", [{"role": "user", "content": "violet bob"}])

    assert search.reindex() == 2
    assert [hit.content for hit in search.search("violet")] == ["violet bob"]
    memory.set_user("alice")
    assert [hit.content for hit in search.search("violet")] == ["violet alice"]


def test_conversation_owner_binding_cannot_be_reassigned():
    memory.set_user("alice")
    memory.save_conversation(
        "fixed-owner", [{"role": "user", "content": "alice text"}])
    assert memory.conversation_owner("fixed-owner") == "alice"

    memory.set_user("bob")
    with pytest.raises(PermissionError, match="different memory principal"):
        memory.save_conversation(
            "fixed-owner", [{"role": "user", "content": "bob overwrite"}])
    assert memory.load_conversation("fixed-owner") == [
        {"role": "user", "content": "alice text"}]
