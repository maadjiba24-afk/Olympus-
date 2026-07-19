"""Gap 1 — notes / todos / reminders. A small per-user checklist store: notes
(kept scraps), todos (tickable), and reminders (todos with a due time surfaced
when due). First-party content, no side effect beyond the user's own file.
"""

import pytest

from olympus import config, todos


@pytest.fixture()
def user(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    return "u"


# --- add / list ------------------------------------------------------------

def test_add_and_list(user):
    todos.add(user, "buy milk")
    items = todos.listing(user)
    assert len(items) == 1 and items[0]["text"] == "buy milk"
    assert items[0]["kind"] == "todo" and items[0]["done"] is False


def test_add_empty_rejected(user):
    with pytest.raises(ValueError):
        todos.add(user, "   ")


def test_note_kind(user):
    it = todos.add(user, "idea: ship it", kind="note")
    assert it["kind"] == "note"


def test_due_parsed_from_string(user):
    it = todos.add(user, "call bank", due="2026-07-10 09:00")
    assert it["due"] is not None


def test_bad_due_is_undated_not_rejected(user):
    it = todos.add(user, "someday", due="not a date")
    assert it["due"] is None and it["text"] == "someday"


def test_bool_due_is_not_an_epoch(user):
    # JSON `true` arrives as Python bool (an int subclass) — must NOT become
    # epoch 1.0 (1970). Treat it as undated.
    assert todos.add(user, "x", due=True)["due"] is None
    assert todos.add(user, "y", due=False)["due"] is None


# --- complete / delete / clear ---------------------------------------------

def test_complete(user):
    it = todos.add(user, "task")
    assert todos.complete(user, it["id"]) is True
    assert todos.listing(user)[0]["done"] is True


def test_complete_unknown_id(user):
    assert todos.complete(user, "nope") is False


def test_delete(user):
    it = todos.add(user, "task")
    assert todos.delete(user, it["id"]) is True
    assert todos.listing(user) == []


def test_clear_done(user):
    a = todos.add(user, "a")
    todos.add(user, "b")
    todos.complete(user, a["id"])
    assert todos.clear_done(user) == 1
    assert [i["text"] for i in todos.listing(user)] == ["b"]


# --- ordering + reminders --------------------------------------------------

def test_open_items_sort_before_done(user):
    a = todos.add(user, "done-one")
    todos.add(user, "open-one")
    todos.complete(user, a["id"])
    order = [i["text"] for i in todos.listing(user)]
    assert order == ["open-one", "done-one"]


def test_due_items_returns_only_ready_open_reminders(user):
    todos.add(user, "past", due=100.0)          # long past -> due
    todos.add(user, "future", due=10 ** 12)     # far future -> not due
    todos.add(user, "no-due")                   # not a reminder
    due = todos.due_items(user, now=1000.0)
    assert [d["text"] for d in due] == ["past"]


def test_done_reminder_is_not_due(user):
    it = todos.add(user, "past", due=100.0)
    todos.complete(user, it["id"])
    assert todos.due_items(user, now=1000.0) == []


def test_malformed_store_is_normalized_not_crashing(user):
    # a hand-edited/corrupt file: a non-dict, an item with no id, and one
    # missing text/kind — must not crash listing/complete/delete.
    import json
    todos._path(user).write_text(json.dumps([
        "garbage",
        {"text": "no id here"},
        {"id": "ok1"},                       # missing text/kind/done
    ]), encoding="utf-8")
    items = todos.listing(user)
    assert [it["id"] for it in items] == ["ok1"]
    assert items[0]["text"] == "" and items[0]["kind"] == "todo"
    assert todos.complete(user, "ok1") is True     # addressable, no KeyError


def test_render_list_empty(user):
    assert "Nothing on your list" in todos.render_list(user)


def test_render_list_shows_items(user):
    todos.add(user, "buy milk")
    out = todos.render_list(user)
    assert "buy milk" in out and "[ ]" in out


# --- tool + specialist wiring ----------------------------------------------

def test_tools_registered_and_on_chronos(user, monkeypatch):
    from olympus import tools, specialists, memory
    for name in ("list_todos", "add_todo", "complete_todo"):
        assert name in tools.EXTRA_TOOLS and name in tools.HANDLERS
        assert name in specialists.SPECIALISTS["chronos"].extra_tools
    monkeypatch.setattr(memory, "current_user", lambda: user)
    out = tools.HANDLERS["add_todo"]("water plants")
    assert "Todo added" in out
    assert "water plants" in tools.HANDLERS["list_todos"]()
