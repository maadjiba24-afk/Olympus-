from olympus import memory


def test_save_and_search_shared():
    memory.save("lessons", "Verify market numbers", "Always check the source.")
    assert "Verify market numbers" in memory.search("market numbers")
    assert "Verify market numbers" in memory.recent("lessons")


def test_user_namespaces_are_isolated():
    memory.set_user("alice")
    memory.save("lessons", "Alice secret goal", "Save 500 per month.")
    memory.set_user("bob")
    assert "Alice secret goal" not in memory.search("alice secret goal")
    memory.set_user("alice")
    assert "Alice secret goal" in memory.search("alice secret goal")


def test_shared_lessons_visible_to_all_users():
    memory.set_user("shared")
    memory.save("lessons", "Council wisdom", "Web claims need sources.")
    memory.set_user("carol")
    assert "Council wisdom" in memory.search("council wisdom sources")


def test_system_categories_always_shared():
    memory.set_user("dave")
    path = memory.save("reports", "Scan", "World pulse...")
    assert "users" not in str(path)


def test_conversation_persistence():
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "hello"}]
    memory.save_conversation("web-abc", history)
    assert memory.load_conversation("web-abc") == history
    assert memory.load_conversation("never-seen") == []


def test_watchlist_fifo():
    memory.watchlist_add("https://youtu.be/AAAAAAAAAAA")
    memory.watchlist_add("https://youtu.be/BBBBBBBBBBB")
    assert memory.watchlist_pop().endswith("AAAAAAAAAAA")
    assert memory.watchlist_pop().endswith("BBBBBBBBBBB")
    assert memory.watchlist_pop() is None


def test_safe_id():
    assert memory.safe_id("tg-12345") == "tg-12345"
    assert memory.safe_id("../../etc") == "etc"
    assert memory.safe_id("") == "shared"
